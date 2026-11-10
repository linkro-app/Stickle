"""A WebDAV server as a target: the address it may be at, who is talked to, what
its answers come up as, and a listing read as it arrives. What every target
answers alike is in test_target_contract, on the real server too."""

import datetime
import hashlib
import http.server
import re
import socketserver
import ssl
import threading
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path

import pytest
import trustme
from webdav_server import HOST, Server, self_signed, webdav

from stickle.sync.outcome import Stopped, after
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    CertificateError,
    Entry,
    FullError,
    NoAccessError,
    NotThereError,
    RefusedError,
    SignInNeededError,
    TargetError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.webdav import (
    Address,
    AddressError,
    NoWebDavFolderError,
    OutsideHomeError,
    WebDav,
    _OnlyThisCertificate,  # pyright: ignore[reportPrivateUsage]
    certificate_of,
    fingerprint,
    parse_address,
)

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"
PASSWORD = "correct horse battery staple"
SOURCES = Path(__file__).parents[2] / "src" / "stickle"


# The address


@pytest.mark.parametrize(
    ("typed", "address"),
    [
        ("nas.example.com", Address(True, "nas.example.com", None, "")),
        ("https://nas.example.com:5006/", Address(True, "nas.example.com", 5006, "")),
        ("  https://nas.example.com/a/b/  ", Address(True, "nas.example.com", None, "/a/b")),
        ("HTTPS://NAS.example.com/A", Address(True, "nas.example.com", None, "/A")),
        ("192.168.0.10:5006/share", Address(True, "192.168.0.10", 5006, "/share")),
        ("https://nas.example.com/my notes", Address(True, "nas.example.com", None, "/my%20notes")),
        (
            "https://nas.example.com/my%20notes",
            Address(True, "nas.example.com", None, "/my%20notes"),
        ),
        ("https://[2001:db8::1]:5006/a", Address(True, "2001:db8::1", 5006, "/a")),
        ("http://192.168.0.10:5005/a", Address(False, "192.168.0.10", 5005, "/a")),
    ],
)
def test_an_address_is_read_as_typed_and_https_unless_it_says_http(
    typed: str, address: Address
) -> None:
    assert parse_address(typed) == address


def test_a_folder_in_any_language_is_encoded_once() -> None:
    assert parse_address("https://nas.example.com/메모").path == "/%EB%A9%94%EB%AA%A8"
    assert parse_address("https://nas.example.com/%EB%A9%94%EB%AA%A8").path == "/%EB%A9%94%EB%AA%A8"


@pytest.mark.parametrize(
    "typed",
    [
        "https://nas.example.com:5006/a%20b",
        "https://[2001:db8::1]:5006/a",
        "http://192.168.0.10/a",
        "https://nas.example.com",
    ],
)
def test_an_address_kept_reads_back_the_same(typed: str) -> None:
    address = parse_address(typed)
    assert parse_address(address.text) == address


@pytest.mark.parametrize(
    "host",
    [
        "192.168.0.10",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.254",
        "169.254.1.1",
        "[fd00::1]",
        "[fe80::1]",
        "nas.local",
        "NAS.LOCAL",
        "nas.local.",
    ],
)
def test_http_is_taken_at_home(host: str) -> None:
    assert not parse_address(f"http://{host}/a").secure


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "8.8.8.8",
        "172.32.0.1",
        "100.64.0.1",
        "[2001:4860:4860::8888]",
        "[::ffff:8.8.8.8]",
        "nas.local.example.com",
        "local",
        "nas",
        "2130706433",
    ],
)
def test_http_is_refused_away_from_home(host: str) -> None:
    with pytest.raises(OutsideHomeError):
        parse_address(f"http://{host}/a")
    assert parse_address(f"https://{host}/a").secure  # and https is fine


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "   ",
        "https://",
        "ftp://nas.example.com",
        "file:///c:/notes",
        "https://nas.example.com:99999",
        "https://user:secret@nas.example.com",
        "https://user@nas.example.com",
        "https://nas.example.com/a?b=1",
        "https://nas.example.com/a#b",
        "https://nas.example.com/a/../b",
        "https://nas.example.com/a%2Fb",
    ],
)
def test_what_is_not_an_address_is_refused(typed: str) -> None:
    with pytest.raises(AddressError):
        parse_address(typed)


# Who is talked to


@pytest.fixture
def share(tmp_path: Path) -> Path:
    root = tmp_path / "dav"
    (root / "share").mkdir(parents=True)
    return root


def over_tls(server: Server) -> Address:
    return parse_address(f"https://{HOST}:{server.port}/share")


def plain(server: Server) -> Address:
    return parse_address(f"http://{HOST}:{server.port}/share")


def untrusting() -> ssl.SSLContext:
    """No public authority issued the certificates of these tests."""
    return ssl.create_default_context()


def test_a_public_authoritys_certificate_is_connected_to_at_once(share: Path) -> None:
    with webdav(share, PASSWORD, trustme.CA()) as server:
        assert server.context is not None
        context = server.context
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=lambda: context)
        assert list(target.list("")) == []
        target.close()


def test_one_nobody_issued_stops_and_shows_itself_and_nothing_is_sent(share: Path) -> None:
    with webdav(share, PASSWORD, own=self_signed()) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=untrusting)
        with pytest.raises(CertificateError) as caught:
            list(target.list(""))
        assert caught.value.certificate == server.certificate
        assert server.asked == []


def test_the_systems_own_check_does_not_know_a_test_authority(share: Path) -> None:
    with webdav(share, PASSWORD, trustme.CA()) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD)
        with pytest.raises(CertificateError):
            list(target.list(""))
        assert server.asked == []


def test_a_public_certificate_for_another_address_is_not_taken(share: Path) -> None:
    with webdav(share, PASSWORD, trustme.CA(), name="another.example.com") as server:
        assert server.context is not None
        context = server.context
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=lambda: context)
        with pytest.raises(CertificateError) as caught:
            list(target.list(""))
        assert caught.value.certificate == server.certificate
        assert server.asked == []


def test_a_look_at_a_certificate_sends_nothing(share: Path) -> None:
    with webdav(share, PASSWORD, own=self_signed()) as server:
        shown = certificate_of(over_tls(server))
        assert shown == server.certificate
        assert server.asked == []
    assert fingerprint(shown) == hashlib.sha256(shown).hexdigest()


def test_a_look_where_nothing_answers_is_unavailable() -> None:
    with pytest.raises(UnavailableError):
        certificate_of(parse_address(f"https://{HOST}:1"))


def signed_by_itself(root: Path) -> AbstractContextManager[Server]:
    return webdav(root, PASSWORD, own=self_signed())


def issued_by_an_authority_of_its_own(root: Path) -> AbstractContextManager[Server]:
    return webdav(root, PASSWORD, trustme.CA(), name="my-nas.example.com")


@pytest.mark.parametrize("serving", [signed_by_itself, issued_by_an_authority_of_its_own])
def test_the_certificate_trusted_is_connected_to_whatever_it_is_named(
    share: Path, serving: Callable[[Path], AbstractContextManager[Server]]
) -> None:
    with serving(share) as server:
        target = WebDav(
            over_tls(server), "stickle", PASSWORD, server.certificate, public=untrusting
        )
        target.put(NOTE, b"text", Absent())
        assert target.get(NOTE).data == b"text"
        assert all(each.sign_in is not None for each in server.asked)
        target.close()


def test_the_certificate_trusted_goes_on_past_its_date(share: Path) -> None:
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    with webdav(share, PASSWORD, trustme.CA(), not_after=past) as server:
        target = WebDav(
            over_tls(server), "stickle", PASSWORD, server.certificate, public=untrusting
        )
        assert list(target.list("")) == []
        target.close()


def test_a_public_certificate_past_its_date_is_not_taken(share: Path) -> None:
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    with webdav(share, PASSWORD, trustme.CA(), not_after=past) as server:
        assert server.context is not None
        context = server.context
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=lambda: context)
        with pytest.raises(CertificateError):
            list(target.list(""))
        assert server.asked == []


def test_another_certificate_stops_and_nothing_is_sent(share: Path) -> None:
    trusted = webdav_certificate(share.parent / "first")
    with webdav(share, PASSWORD, own=self_signed()) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD, trusted, public=untrusting)
        with pytest.raises(CertificateError) as caught:
            list(target.list(""))
        assert caught.value.certificate == server.certificate != trusted
        assert server.asked == []


def webdav_certificate(root: Path) -> bytes:
    """A certificate of another server."""
    root.mkdir()
    with webdav(root, own=self_signed()) as other:
        assert other.certificate is not None
        return other.certificate


def test_a_certificate_the_trusted_one_issued_is_another_certificate(share: Path) -> None:
    authority = trustme.CA()
    trusted = ssl.PEM_cert_to_DER_cert(authority.cert_pem.bytes().decode("ascii"))
    with webdav(share, PASSWORD, authority) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD, trusted, public=untrusting)
        with pytest.raises(CertificateError):
            list(target.list(""))
        assert server.asked == []


def test_a_public_authoritys_in_place_of_the_trusted_one_goes_on(share: Path) -> None:
    trusted = webdav_certificate(share.parent / "first")
    dropped: list[bool] = []
    with webdav(share, PASSWORD, trustme.CA()) as server:
        assert server.context is not None
        context = server.context
        target = WebDav(
            over_tls(server),
            "stickle",
            PASSWORD,
            trusted,
            public=lambda: context,
            no_longer_trusted=lambda: dropped.append(True),
        )
        assert list(target.list("")) == []
        assert dropped == [True]
        target.close()
        assert list(target.list("")) == []  # a new connection: told once, not each time
        assert dropped == [True]
        target.close()


def test_the_certificate_is_compared_however_a_connection_comes_to_be_made(share: Path) -> None:
    authority = trustme.CA()  # a handshake it issued passes; only the comparing stops it
    trusted = ssl.PEM_cert_to_DER_cert(authority.cert_pem.bytes().decode("ascii"))
    with webdav(share, PASSWORD, authority) as server:
        connection = _OnlyThisCertificate(over_tls(server), trusted)  # never told to connect
        with pytest.raises(ssl.SSLCertVerificationError):
            connection.request("GET", "/share/", headers={"Authorization": "Basic secret"})
        connection.close()
        assert server.asked == []


def test_a_request_takes_the_time_it_was_given_over_http(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD, timeout=1.5)
        assert target._open().timeout == 1.5  # pyright: ignore[reportPrivateUsage]
        target.close()


def test_a_request_takes_the_time_it_was_given_with_a_trusted_certificate(share: Path) -> None:
    shown, key = self_signed()
    trusted = ssl.PEM_cert_to_DER_cert(shown.decode("ascii"))
    with webdav(share, PASSWORD, own=(shown, key)) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD, trusted, timeout=1.5)
        connection = target._open()  # pyright: ignore[reportPrivateUsage]
        assert connection.timeout == 1.5
        connection.close()


def test_a_request_takes_the_time_it_was_given_with_a_public_certificate(share: Path) -> None:
    authority = trustme.CA()
    with webdav(share, PASSWORD, authority) as server:
        context = server.context
        assert context is not None
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=lambda: context, timeout=1.5)
        connection = target._open()  # pyright: ignore[reportPrivateUsage]
        assert connection.timeout == 1.5
        connection.close()


def test_a_server_failing_has_answered_and_one_not_reached_has_not(scripted: Scripted) -> None:
    scripted.answers.append((500, {}, b""))
    target = to(scripted)
    with pytest.raises(UnavailableError) as failing:
        target.get(NOTE)
    target.close()
    assert failing.value.answered
    nobody = WebDav(Address(False, HOST, 1, "/share"), "stickle", PASSWORD, timeout=1.0)
    with pytest.raises(UnavailableError) as not_reached:
        nobody.get(NOTE)
    assert not not_reached.value.answered


def test_a_folder_the_server_will_not_make_has_answered(scripted: Scripted) -> None:
    scripted.answers.append((409, {}, b""))
    target = to(scripted)
    with pytest.raises(UnavailableError) as failing:
        target.put(NOTE, b"x", Absent())
    target.close()
    assert failing.value.answered


def test_certificate_checks_are_never_turned_off() -> None:
    sources = list(SOURCES.rglob("*.py"))
    assert len(sources) > 50
    text = {path: path.read_text(encoding="utf-8") for path in sources}
    for path, source in text.items():
        assert "CERT_NONE" not in source, path
        assert "_create_unverified_context" not in source, path
        assert "verify_mode" not in source, path
    # Only for the certificate the user trusted, where the certificate itself is compared.
    turned_off = [path.name for path, each in text.items() if "check_hostname = False" in each]
    assert turned_off == ["webdav.py"]
    assert len(re.findall("check_hostname = False", text[SOURCES / "sync/targets/webdav.py"])) == 1


def test_over_http_the_server_is_reached_at_home(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        assert list(target.list("")) == []
        target.close()


def test_a_certificate_stops_sync_until_the_user_looks() -> None:
    outcome = after(CertificateError(b"shown"), failures=3)
    assert outcome.stopped is Stopped.CERTIFICATE and outcome.wait is None


# The password


def test_a_wrong_password_is_asked_once_and_never_again(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", "wrong")
        with pytest.raises(SignInNeededError):
            list(target.list(""))
        assert len(server.asked) == 1
        with pytest.raises(SignInNeededError):
            target.put(NOTE, b"text", Absent())
        assert len(server.asked) == 2  # the first folder asked for, and nothing after it
        target.close()


def test_a_password_in_any_language_signs_in(share: Path) -> None:
    with webdav(share, "비밀번호 🔑") as server:
        target = WebDav(plain(server), "stickle", "비밀번호 🔑")
        assert list(target.list("")) == []
        target.close()


# Whether the address is a WebDAV folder


def test_a_webdav_folder_at_the_address_passes_and_nothing_is_made(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        target.check()
        target.close()
        assert len(server.asked) == 1
    assert list((share / "share").iterdir()) == []


def test_a_wrong_password_fails_the_check_and_is_asked_once(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", "wrong")
        with pytest.raises(SignInNeededError):
            target.check()
        target.close()
        assert len(server.asked) == 1


def test_no_folder_at_the_address_fails_the_check(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        address = parse_address(f"http://{HOST}:{server.port}/nowhere")
        target = WebDav(address, "stickle", PASSWORD)
        with pytest.raises(NoWebDavFolderError):
            target.check()
        target.close()
    assert not (share / "nowhere").exists()


def test_the_password_is_not_sent_to_a_server_not_trusted(share: Path) -> None:
    with signed_by_itself(share) as server:
        target = WebDav(over_tls(server), "stickle", PASSWORD, public=untrusting)
        with pytest.raises(CertificateError):
            target.check()
        target.close()
        assert server.asked == []


def failures(call: Callable[[], object]) -> list[TargetError]:
    with pytest.raises(TargetError) as caught:
        call()
    return [caught.value]


def said(error: BaseException) -> str:
    """All an error says of itself when written down, and what it came from."""
    parts: list[str] = []
    at: BaseException | None = error
    while at is not None:
        parts += [repr(at), str(at)]
        at = at.__cause__ or (None if at.__suppress_context__ else at.__context__)
    return "\n".join(parts)


def test_no_error_says_where_or_who(share: Path) -> None:
    address = parse_address("https://secret-nas.invalid:5006/private-folder")
    caught = failures(lambda: list(WebDav(address, "secret-user", PASSWORD).list("")))
    refused = parse_address(f"https://{HOST}:1/private-folder")
    caught += failures(lambda: list(WebDav(refused, "secret-user", PASSWORD).list("")))
    with webdav(share, PASSWORD, own=self_signed()) as server:
        shown = over_tls(server)
        caught += failures(
            lambda: list(WebDav(shown, "secret-user", PASSWORD, public=untrusting).list(""))
        )
    assert len(caught) == 3
    for error in caught:
        written = said(error)
        for what in ("secret-nas", "private-folder", "secret-user", PASSWORD, HOST):
            assert what not in written, written


# Files and folders, on the real server


def test_the_stickle_folder_is_made_under_the_address_when_first_written(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        target.put(NOTE, b"text", Absent())
        target.put("vault.json", b"{}", Absent())
        target.close()
    assert (share / "share" / "Stickle" / NOTE).read_bytes() == b"text"
    assert (share / "share" / "Stickle" / "vault.json").read_bytes() == b"{}"


def test_folders_are_made_once_each_before_the_first_write_into_them(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        target.put("notes/a.json", b"1", Absent())
        target.put("notes/b.json", b"2", Absent())
        target.put("records/c.json", b"3", Absent())
        target.put("vault.json", b"4", Absent())
        target.close()
        assert [each.method for each in server.asked] == [
            "MKCOL",  # the Stickle folder
            "MKCOL",  # notes
            "PUT",
            "PUT",
            "MKCOL",  # records
            "PUT",
            "PUT",
        ]


def test_the_users_own_folder_is_not_made_for_them(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        elsewhere = parse_address(f"http://{HOST}:{server.port}/no-such-folder")
        target = WebDav(elsewhere, "stickle", PASSWORD)
        with pytest.raises(UnavailableError):
            target.put(NOTE, b"text", Absent())
        target.close()
    assert not (share / "no-such-folder").exists()


def test_a_folder_not_made_yet_lists_nothing(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        assert list(target.list("notes")) == []
        assert list(target.list("")) == []
        target.close()


def test_a_name_with_odd_characters_comes_back_as_it_went(share: Path) -> None:
    name = "notes/사본 (1) #100% a+b.json"
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        target.put(name, b"text", Absent())
        assert [each.path for each in target.list("notes")] == [name]
        assert target.get(name).data == b"text"
        target.delete(name, AsRead(None))
        with pytest.raises(NotThereError):
            target.get(name)
        target.close()


def test_a_listing_and_requests_made_while_reading_it(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        for number in range(4):
            target.put(f"notes/{number}.json", f"{number}".encode(), Absent())
        read = {each.path: target.get(each.path).data for each in target.list("notes")}
        assert read == {f"notes/{n}.json": f"{n}".encode() for n in range(4)}
        target.close()


def test_a_listing_left_half_read_does_not_get_in_the_way(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        for number in range(4):
            target.put(f"notes/{number}.json", b"x", Absent())
        listing = target.list("notes")
        next(listing)
        listing.close()
        assert target.get("notes/1.json").data == b"x"
        target.close()


def test_listed_tags_are_the_tags_a_read_gives(share: Path) -> None:
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        written = target.put(NOTE, b"text", Absent())
        entry = next(iter(target.list("notes")))
        assert entry.tag == target.get(NOTE).tag == written
        target.close()


def test_conditions_are_sent_though_they_cannot_be_counted_on(share: Path) -> None:
    # Told nothing of the server, the target says conditions cannot be counted
    # on, and sends them all the same: this server keeps them and refuses.
    with webdav(share, PASSWORD) as server:
        target = WebDav(plain(server), "stickle", PASSWORD)
        assert not target.keeps_conditions and not target.unique_tags
        target.put(NOTE, b"1", Absent())
        with pytest.raises(RefusedError):
            target.put(NOTE, b"22", Absent())
        with pytest.raises(RefusedError):
            target.put(NOTE, b"22", AsRead('"not its tag"'))
        assert target.get(NOTE).data == b"1"
        target.close()


# Answers, from a server that says what the test tells it to


@dataclass
class Scripted:
    """Answers in turn what it is told to, then 200."""

    answers: list[tuple[int, dict[str, str], bytes]] = field(
        default_factory=list[tuple[int, dict[str, str], bytes]]
    )
    close_after: bool = False  # closes each connection after answering, saying nothing of it
    hang_up: int = 0  # requests still to take and close the connection on, answering nothing
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    depths: list[str | None] = field(default_factory=list[str | None])
    conditions: list[tuple[str | None, str | None]] = field(
        default_factory=list[tuple[str | None, str | None]]
    )
    connections: int = 0
    port: int = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script: Scripted

    def setup(self) -> None:
        super().setup()
        self.script.connections += 1

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.script.requests.append((self.command, self.path))
        self.script.depths.append(self.headers.get("Depth"))
        self.script.conditions.append(
            (self.headers.get("If-Match"), self.headers.get("If-None-Match"))
        )
        if self.script.hang_up > 0:
            self.script.hang_up -= 1
            self.close_connection = True
            return
        status, headers, body = (
            self.script.answers.pop(0) if self.script.answers else (200, {}, b"")
        )
        self.send_response_only(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if "Date" not in headers:
            self.send_header("Date", formatdate(usegmt=True))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if self.script.close_after:
            self.close_connection = True

    do_GET = do_PUT = do_DELETE = do_PROPFIND = do_MKCOL = answer  # noqa: N815

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def scripted() -> Iterator[Scripted]:
    script = Scripted()

    class Handler(_Handler):
        pass

    Handler.script = script

    class Threading(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Threading((HOST, 0), Handler)
    script.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield script
    server.shutdown()
    server.server_close()
    thread.join(10)


def to(script: Scripted, heard: Callable[[int, int], None] | None = None) -> WebDav:
    address = parse_address(f"http://{HOST}:{script.port}/share")
    return WebDav(address, "stickle", PASSWORD, heard=heard)


def test_the_check_asks_of_the_address_itself(scripted: Scripted) -> None:
    scripted.answers = [(207, {}, b"")]
    target = to(scripted)
    target.check()
    target.close()
    assert scripted.requests == [("PROPFIND", "/share/")]
    assert scripted.depths == ["0"]


@pytest.mark.parametrize("status", [200, 301, 302, 400, 404, 405, 501])
def test_what_does_not_answer_as_webdav_fails_the_check(scripted: Scripted, status: int) -> None:
    scripted.answers = [(status, {}, b"")]
    target = to(scripted)
    with pytest.raises(NoWebDavFolderError):
        target.check()
    target.close()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, SignInNeededError),
        (403, NoAccessError),
        (429, TooBusyError),
        (500, UnavailableError),
        (503, UnavailableError),
    ],
)
def test_the_check_says_what_any_request_would(
    scripted: Scripted, status: int, error: type[TargetError]
) -> None:
    scripted.answers = [(status, {}, b"")]
    target = to(scripted)
    with pytest.raises(error):
        target.check()
    target.close()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, SignInNeededError),
        (403, NoAccessError),
        (404, NotThereError),
        (410, NotThereError),
        (412, RefusedError),
        (429, TooBusyError),
        (507, FullError),
        (500, UnavailableError),
        (502, UnavailableError),
        (503, UnavailableError),
        (423, UnavailableError),
        (400, UnavailableError),
        (301, UnavailableError),
        (302, UnavailableError),
    ],
)
def test_what_a_read_is_answered_comes_up_as_the_error_for_it(
    scripted: Scripted, status: int, error: type[Exception]
) -> None:
    scripted.answers.append((status, {"Location": "https://elsewhere.example.com/"}, b""))
    target = to(scripted)
    with pytest.raises(error):
        target.get(NOTE)
    assert len(scripted.requests) == 1  # asked once: a redirect is not followed
    target.close()


@pytest.mark.parametrize(
    ("status", "headers", "wait"),
    [
        (429, {"Retry-After": "120"}, 120.0),
        (429, {}, None),
        (429, {"Retry-After": "soon"}, None),
        (429, {"Retry-After": "-5"}, 0.0),
        (503, {"Retry-After": "30"}, 30.0),
        (
            429,
            {
                "Retry-After": "Wed, 21 Oct 2026 07:29:00 GMT",
                "Date": "Wed, 21 Oct 2026 07:28:00 GMT",
            },
            60.0,
        ),
    ],
)
def test_too_many_requests_waits_as_asked(
    scripted: Scripted, status: int, headers: dict[str, str], wait: float | None
) -> None:
    scripted.answers.append((status, headers, b""))
    target = to(scripted)
    with pytest.raises(TooBusyError) as caught:
        target.get(NOTE)
    assert caught.value.retry_after == wait
    target.close()


def test_no_write_goes_into_a_folder_before_it_is_made(scripted: Scripted) -> None:
    scripted.answers += [(201, {}, b""), (405, {}, b""), (201, {"ETag": '"new"'}, b"")]
    target = to(scripted)
    assert target.put(NOTE, b"text", Absent()) == '"new"'
    assert scripted.requests == [
        ("MKCOL", "/share/Stickle/"),
        ("MKCOL", "/share/Stickle/notes/"),
        ("PUT", f"/share/Stickle/{NOTE}"),
    ]
    target.close()


def test_folders_removed_since_are_made_again(scripted: Scripted) -> None:
    made: tuple[int, dict[str, str], bytes] = (201, {}, b"")
    scripted.answers += [made, made, (409, {}, b""), made, made, (201, {"ETag": '"new"'}, b"")]
    target = to(scripted)
    assert target.put(NOTE, b"text", Absent()) == '"new"'
    assert [method for method, _path in scripted.requests] == [
        "MKCOL",
        "MKCOL",
        "PUT",
        "MKCOL",
        "MKCOL",
        "PUT",
    ]
    target.close()


def test_a_write_still_refused_after_the_folders_are_made_is_refused(scripted: Scripted) -> None:
    there: tuple[int, dict[str, str], bytes] = (405, {}, b"")
    scripted.answers += [there, there, (409, {}, b""), there, there, (409, {}, b"")]
    target = to(scripted)
    with pytest.raises(RefusedError):
        target.put(NOTE, b"text", Absent())
    assert len(scripted.requests) == 6
    target.close()


def test_a_write_with_no_tag_answered_says_none(scripted: Scripted) -> None:
    scripted.answers += [(201, {}, b""), (201, {}, b""), (204, {}, b"")]
    target = to(scripted)
    assert target.put(NOTE, b"text", AsRead('"read"')) is None
    target.close()


def test_a_kept_connection_serves_the_next_request(scripted: Scripted) -> None:
    target = to(scripted)
    for _ in range(5):
        target.get(NOTE)
    assert scripted.connections == 1 and len(scripted.requests) == 5
    target.close()


def test_a_connection_the_server_closed_is_replaced_once(scripted: Scripted) -> None:
    scripted.close_after = True
    target = to(scripted)
    target.get(NOTE)
    target.get(NOTE)
    assert scripted.connections == 2 and len(scripted.requests) == 2
    target.close()


def test_a_new_connection_that_gets_no_answer_is_not_tried_again(scripted: Scripted) -> None:
    scripted.hang_up = 1
    target = to(scripted)
    with pytest.raises(UnavailableError):
        target.get(NOTE)
    assert scripted.connections == 1 and len(scripted.requests) == 1
    target.close()


def test_a_kept_connection_is_replaced_only_once(scripted: Scripted) -> None:
    target = to(scripted)
    target.get(NOTE)
    scripted.hang_up = 5
    with pytest.raises(UnavailableError):
        target.get(NOTE)
    assert scripted.connections == 2 and len(scripted.requests) == 3
    target.close()


def test_a_server_that_does_not_answer_is_unavailable() -> None:
    target = WebDav(parse_address(f"http://{HOST}:1"), "stickle", PASSWORD)
    with pytest.raises(UnavailableError):
        target.get(NOTE)


def test_the_time_a_server_says_is_told(scripted: Scripted) -> None:
    told: list[tuple[int, int]] = []
    scripted.answers.append((200, {"Date": "Wed, 21 Oct 2026 07:28:00 GMT"}, b""))
    target = to(scripted, heard=lambda date, took: told.append((date, took)))
    target.get(NOTE)
    assert len(told) == 1
    date, took = told[0]
    assert date == 1792567680000 and 0 <= took < 10_000
    target.close()


def test_no_date_tells_no_time(scripted: Scripted) -> None:
    told: list[tuple[int, int]] = []
    scripted.answers.append((200, {"Date": "not one"}, b""))
    target = to(scripted, heard=lambda date, took: told.append((date, took)))
    target.get(NOTE)
    assert told == []
    target.close()


def propfind(*responses: str) -> bytes:
    body = "".join(responses)
    return f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">{body}</D:multistatus>'.encode()


def item(href: str, props: str = "", collection: bool = False) -> str:
    kind = "<D:collection/>" if collection else ""
    return (
        f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        f"<D:resourcetype>{kind}</D:resourcetype>{props}"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )


def listed(script: Scripted, body: bytes) -> list[Entry]:
    script.answers.append((207, {"Content-Type": "application/xml"}, body))
    target = to(script)
    try:
        return list(target.list("notes"))
    finally:
        target.close()


def test_a_listing_gives_the_files_right_in_the_folder(scripted: Scripted) -> None:
    entries = listed(
        scripted,
        propfind(
            item("/share/Stickle/notes/", collection=True),
            item(
                "/share/Stickle/notes/a.json",
                '<D:getcontentlength>12</D:getcontentlength><D:getetag>"tag-a"</D:getetag>',
            ),
            item("/share/Stickle/notes/inner/", collection=True),
        ),
    )
    assert entries == [Entry("notes/a.json", '"tag-a"', 12)]


@pytest.mark.parametrize(
    ("href", "name"),
    [
        ("https://nas.example.com:5006/share/Stickle/notes/a.json", "a.json"),
        ("/behind/a/proxy/notes/a.json", "a.json"),
        ("/share/Stickle/notes/a%20b.json", "a b.json"),
        ("/share/Stickle/notes/%EC%82%AC%EB%B3%B8.json", "사본.json"),
        ("/share/Stickle/notes/사본.json", "사본.json"),
    ],
)
def test_a_listing_is_read_however_the_server_writes_its_names(
    scripted: Scripted, href: str, name: str
) -> None:
    assert [each.path for each in listed(scripted, propfind(item(href)))] == [f"notes/{name}"]


@pytest.mark.parametrize(
    "href", ["/share/notes/..", "/share/notes/.", "/share/notes/a%2Fb", "/share/notes/a%5Cb"]
)
def test_a_name_that_could_lead_elsewhere_is_left_out(scripted: Scripted, href: str) -> None:
    assert listed(scripted, propfind(item(href))) == []


@pytest.mark.parametrize(
    ("given", "tag"),
    [
        ('"quoted"', '"quoted"'),
        ("bare", '"bare"'),
        ('W/"weak"', 'W/"weak"'),
        ("  spaced  ", '"spaced"'),
    ],
)
def test_a_tag_listed_is_kept_as_it_is_sent_back(
    scripted: Scripted, given: str, tag: str | None
) -> None:
    props = f"<D:getetag>{given}</D:getetag>"
    assert listed(scripted, propfind(item("/n/a.json", props))) == [Entry("notes/a.json", tag, 0)]


@pytest.mark.parametrize(
    ("given", "tag"),
    [
        ('"quoted"', '"quoted"'),
        ("bare", '"bare"'),
        ('W/"weak"', 'W/"weak"'),
        ("", None),
        ("bad" + chr(7) + "tag", None),
        ("caf" + chr(0xE9), None),
    ],
)
def test_a_tag_read_is_kept_only_if_it_can_be_sent_back(
    scripted: Scripted, given: str, tag: str | None
) -> None:
    scripted.answers.append((200, {"ETag": given}, b"text"))
    target = to(scripted)
    assert target.get(NOTE).tag == tag
    target.close()


def test_a_file_without_size_or_tag_is_listed_all_the_same(scripted: Scripted) -> None:
    entries = listed(
        scripted, propfind(item("/n/a.json", "<D:getcontentlength>x</D:getcontentlength>"))
    )
    assert entries == [Entry("notes/a.json", None, 0)]


def test_a_listing_that_breaks_off_is_unavailable(scripted: Scripted) -> None:
    whole = propfind(item("/n/a.json"), item("/n/b.json"))
    scripted.answers.append((207, {}, whole[: len(whole) - 30]))
    target = to(scripted)
    with pytest.raises(UnavailableError):
        list(target.list("notes"))
    target.get(NOTE)  # and what comes next is not hurt by it
    target.close()


def test_the_condition_is_sent_as_the_tag_was_read(scripted: Scripted) -> None:
    target = to(scripted)
    target.put(NOTE, b"x", AsRead('W/"read"'))
    target.put(NOTE, b"x", Absent())
    target.put(NOTE, b"x", AsRead(None))
    target.put(NOTE, b"x", AsRead("bare"))
    target.delete(NOTE, AsRead('"read"'))
    target.delete(NOTE, AsRead(None))
    asked = zip(scripted.requests, scripted.conditions, strict=True)
    assert [condition for (method, _path), condition in asked if method != "MKCOL"] == [
        ('W/"read"', None),
        (None, "*"),
        (None, None),
        ('"bare"', None),
        ('"read"', None),
        (None, None),
    ]
    target.close()


# Memory


class _Endless(http.server.BaseHTTPRequestHandler):
    """A listing of many files, written as it is read."""

    protocol_version = "HTTP/1.1"
    files = 5000

    def do_PROPFIND(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response_only(207)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.chunk(b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">')
        batch: list[bytes] = []
        for number in range(self.files):
            name = f"/dav/Stickle/notes/{number:08x}-1d9b-4a63-9c38-3b2a4f2f7d10.json"
            props = (
                '<D:getcontentlength>2048</D:getcontentlength><D:getetag>"'
                f"{number:016x}-800-5f3a"
                '"</D:getetag>'
            )
            batch.append(item(name, props).encode())
            if len(batch) == 50:
                self.chunk(b"".join(batch))
                batch.clear()
        self.chunk(b"".join(batch))
        self.chunk(b"</D:multistatus>")
        self.wfile.write(b"0\r\n\r\n")

    def chunk(self, data: bytes) -> None:
        if data:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_a_long_listing_is_not_held_in_memory() -> None:
    class Threading(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Threading((HOST, 0), _Endless)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = WebDav(parse_address(f"http://{HOST}:{server.server_address[1]}"), "stickle", PASSWORD)
    try:
        tracemalloc.start()
        try:
            count = 0
            total = 0
            for entry in target.list("notes"):
                count += 1
                total += entry.size
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        target.close()
        server.shutdown()
        server.server_close()
        thread.join(10)
    assert count == _Endless.files and total == 2048 * count
    # The text alone is over 2 MB, and as a tree ten times that.
    assert peak < 1024 * 1024, f"{peak / 1048576:.1f} MB"
