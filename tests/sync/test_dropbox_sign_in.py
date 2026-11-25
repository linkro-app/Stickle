"""Signing in to Dropbox in the browser (dropbox.sign_in, over
stickle.sync.targets.sign_in), against Dropbox in the test (dropbox_server). The
test plays the browser: it opens the page to allow Stickle, which sends it back
to the listener with a code, or shows the code for the user to enter."""

import contextlib
import http.client
import logging
import re
import socket
import sys
from collections.abc import Generator
from urllib.parse import parse_qs, urlsplit

import pytest
from dropbox_server import (
    ACCOUNT,
    API,
    APP_KEY,
    AUTHORIZE,
    CONTENT,
    HOST,
    NOTIFY,
    TOKEN_PATH,
    WWW,
    Answer,
    DropboxState,
    dropbox_server,
)

from stickle.cloud_apps import BUILT_IN, DROPBOX_VARIABLE
from stickle.sync.targets.base import NoAccessError, UnavailableError
from stickle.sync.targets.dropbox import (
    DROPBOX,
    REDIRECT_PORTS,
    SCOPE,
    App,
    Dropbox,
    registered,
    sign_in,
)
from stickle.sync.targets.sign_in import Pages, SignIn, SignInFailedError

PAGES = Pages("ko", "로그인했습니다.", "로그인하지 못했습니다.")
SOON = 5.0  # seconds a sign-in is given to end, in the test


@pytest.fixture
def box() -> Generator[DropboxState]:
    with dropbox_server() as serving:
        yield serving


@pytest.fixture
def signing(box: DropboxState) -> Generator[SignIn]:
    with sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made:
        yield made


def query_of(address: str) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(urlsplit(address).query).items()}


def to_allow_page(box: DropboxState, address: str) -> tuple[int, str, str]:
    """As a browser at the page to allow Stickle, the user allowing it: its
    status, where it sends the browser back to, and the page."""
    parts = urlsplit(address)
    assert parts.path == AUTHORIZE
    connection = http.client.HTTPSConnection(HOST, parts.port, context=box.context(), timeout=10)
    try:
        connection.request("GET", f"{parts.path}?{parts.query}")
        response = connection.getresponse()
        body = response.read().decode()
        return response.status, response.getheader("Location") or "", body
    finally:
        connection.close()


def shown_code(box: DropboxState, address: str) -> str:
    status, location, body = to_allow_page(box, address)
    assert status == 200 and not location
    found = re.search(r"<code>([^<]+)</code>", body)
    assert found is not None
    return found.group(1)


def back(location: str, *, host: str | None = None) -> tuple[int, bytes]:
    """As a browser sent back to the listener: its status and page."""
    parts = urlsplit(location)
    assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port
    connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=10)
    try:
        connection.request(
            "GET", f"{parts.path or '/'}?{parts.query}", headers={"Host": host or parts.netloc}
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def signed_in_by_browser(box: DropboxState, signing: SignIn) -> tuple[int, bytes]:
    status, location, _ = to_allow_page(box, signing.start())
    assert status == 302
    return back(location)


def port_of(address: str) -> int:
    port = urlsplit(query_of(address)["redirect_uri"]).port
    assert port is not None
    return port


def listening(port: int, via: str = "127.0.0.1") -> bool:
    family = socket.AF_INET6 if ":" in via else socket.AF_INET
    with socket.socket(family) as probe:
        probe.settimeout(3)
        try:
            probe.connect((via, port))
        except OSError:
            return False
    return True


def exchanges(box: DropboxState) -> list[dict[str, str]]:
    """The forms sent to the token address to exchange a code."""
    forms = [
        {name: values[0] for name, values in parse_qs(each.body.decode()).items()}
        for each in box.requests(API, TOKEN_PATH)
    ]
    return [form for form in forms if form.get("grant_type") == "authorization_code"]


@contextlib.contextmanager
def taken(ports: tuple[int, ...]) -> Generator[None]:
    """The ports held on 127.0.0.1 by another program, as far as they can be."""
    held: list[socket.socket] = []
    try:
        for port in ports:
            each = socket.socket()
            try:
                each.bind(("127.0.0.1", port))
                each.listen()
            except OSError:
                each.close()  # another has it already
                continue
            held.append(each)
        yield
    finally:
        for each in held:
            each.close()


# The address for the browser


def test_the_address_asks_dropbox_for_a_code_and_a_refresh_token(signing: SignIn) -> None:
    address = signing.start()
    query = query_of(address)

    assert query["client_id"] == APP_KEY
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == f"http://127.0.0.1:{port_of(address)}"
    assert port_of(address) in REDIRECT_PORTS
    assert query["scope"] == SCOPE
    assert set(SCOPE.split()) == {
        "files.metadata.read",
        "files.content.read",
        "files.content.write",
        "account_info.read",
    }
    assert query["token_access_type"] == "offline"
    assert query["force_reapprove"] == "true"  # the account shown, every time
    assert query["locale"] == "ko"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    assert len(query["state"]) >= 43
    assert "client_secret" not in query and "force_reauthentication" not in query


def test_the_sign_in_page_is_dropboxs_own_site() -> None:
    assert DROPBOX.authorize == "https://www.dropbox.com/oauth2/authorize"
    assert DROPBOX.www not in DROPBOX.hosts  # the browser's: the app sends it nothing


def test_it_listens_on_127_0_0_1_on_a_registered_port(signing: SignIn) -> None:
    port = port_of(signing.start())

    assert not signing.pasting
    assert listening(port)
    assert not listening(port, "::1")  # the browser goes to the address, not to localhost


def test_a_port_taken_the_next_registered_one(box: DropboxState) -> None:
    with (
        taken(REDIRECT_PORTS[:2]),
        sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made,
    ):
        port = port_of(made.start())
    assert port in REDIRECT_PORTS[2:]


# Signing in


def test_a_sign_in_gives_the_refresh_token_and_the_account(
    box: DropboxState, signing: SignIn
) -> None:
    status, body = signed_in_by_browser(box, signing)
    signed = signing.wait(SOON)

    assert status == 200 and "로그인했습니다." in body.decode()
    assert signed is not None
    assert signed.refresh_token in box.refresh_tokens
    assert signed.account == ACCOUNT
    [form] = exchanges(box)
    assert form["client_id"] == APP_KEY
    assert form["redirect_uri"].startswith("http://127.0.0.1:")
    assert form["code_verifier"] and "client_secret" not in form


def test_the_account_is_asked_with_the_access_token_before_the_page(
    box: DropboxState, signing: SignIn
) -> None:
    signed_in_by_browser(box, signing)  # the page is in: so is all before it

    [asked] = box.requests(API, "/2/users/get_current_account")
    assert asked.authorization == f"Bearer {box.issued[-1]}"
    order = [each.path for each in box.asked if each.host == API]
    assert order == [TOKEN_PATH, "/2/users/get_current_account"]


def test_the_listener_is_closed_once_signed_in(box: DropboxState, signing: SignIn) -> None:
    address = signing.start()
    _, location, _ = to_allow_page(box, address)
    back(location)
    signing.wait(SOON)

    assert not listening(port_of(address))


def test_a_refresh_token_from_the_sign_in_makes_a_target(
    box: DropboxState, signing: SignIn
) -> None:
    signed_in_by_browser(box, signing)
    signed = signing.wait(SOON)
    assert signed is not None
    app = App(APP_KEY, box.place, box.context)
    target = app.target(signed.refresh_token, lambda _: None)
    try:
        assert list(target.list("")) == []
    finally:
        target.close()


def test_saying_no_to_the_app(box: DropboxState, signing: SignIn) -> None:
    box.declining = True
    status, body = signed_in_by_browser(box, signing)

    assert status == 400 and "로그인하지 못했습니다." in body.decode()
    with pytest.raises(SignInFailedError) as failed:
        signing.wait(SOON)
    assert failed.value.declined
    assert exchanges(box) == []


def test_an_answer_from_another_host_name_is_not_taken(box: DropboxState, signing: SignIn) -> None:
    _, location, _ = to_allow_page(box, signing.start())
    port = urlsplit(location).port

    assert back(location, host=f"localhost:{port}")[0] == 404
    assert exchanges(box) == []
    assert back(location)[0] == 200
    assert signing.wait(SOON) is not None


def test_no_refresh_token_is_no_sign_in(box: DropboxState, signing: SignIn) -> None:
    box.token_answers.append(
        Answer(200, body={"access_token": "sl.u.only", "token_type": "bearer", "expires_in": 14400})
    )
    status, _ = signed_in_by_browser(box, signing)

    assert status == 400
    with pytest.raises(SignInFailedError):
        signing.wait(SOON)


def test_the_app_turned_away_at_the_exchange(box: DropboxState, signing: SignIn) -> None:
    _, location, _ = to_allow_page(box, signing.start())
    box.app_turned_away = True
    back(location)

    with pytest.raises(NoAccessError):
        signing.wait(SOON)


# The account's name


@pytest.mark.parametrize("email", [None, "", "   ", "a" * 300, "line\nbreak", 42])
def test_an_account_not_told_or_odd_is_left_out(
    box: DropboxState, signing: SignIn, email: object
) -> None:
    box.email = email
    status, _ = signed_in_by_browser(box, signing)
    signed = signing.wait(SOON)

    assert status == 200 and signed is not None and signed.account is None


@pytest.mark.parametrize("answer", [Answer(500, body=b"down"), Answer(401, body=b"{}")])
def test_the_account_not_had_leaves_the_sign_in_done(
    box: DropboxState, signing: SignIn, answer: Answer
) -> None:
    box.answers.append(answer)  # for the next request but a token's: the account's
    status, _ = signed_in_by_browser(box, signing)
    signed = signing.wait(SOON)

    assert status == 200 and signed is not None
    assert signed.refresh_token and signed.account is None


def test_the_account_unreachable_leaves_the_sign_in_done(box: DropboxState) -> None:
    def unreachable(_tokens: object) -> str | None:
        raise UnavailableError()

    place = box.place
    with SignIn(
        place.authorize,
        place.tokens,
        APP_KEY,
        SCOPE,
        PAGES,
        extra={"token_access_type": "offline"},
        redirect_host="127.0.0.1",
        ports=place.redirect_ports,
        account=unreachable,
        public=box.context,
    ) as made:
        status, _ = signed_in_by_browser(box, made)
        signed = made.wait(SOON)

    assert status == 200 and signed is not None and signed.account is None


# Every port taken: the code entered


def test_every_port_taken_the_user_enters_the_code_shown(box: DropboxState) -> None:
    with (
        taken(REDIRECT_PORTS),
        sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made,
    ):
        address = made.start()
        assert made.pasting
        assert "redirect_uri" not in query_of(address)
        code = shown_code(box, address)

        signed = made.enter(f"  {code}\n")

        assert signed is not None and signed.account == ACCOUNT
        assert made.wait(SOON) == signed
    [form] = exchanges(box)
    assert "redirect_uri" not in form and form["code_verifier"]


def test_a_wrong_code_may_be_entered_again(box: DropboxState) -> None:
    with (
        taken(REDIRECT_PORTS),
        sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made,
    ):
        code = shown_code(box, made.start())

        with pytest.raises(SignInFailedError):
            made.enter("not a code at all")  # not one: sent nowhere
        assert exchanges(box) == []
        with pytest.raises(SignInFailedError):
            made.enter("abcdefghijklmnop")  # Dropbox's to refuse
        assert len(exchanges(box)) == 1

        assert made.enter(code) is not None
        with pytest.raises(RuntimeError):
            made.enter(code)


def test_a_code_from_another_sign_in_is_no_good(box: DropboxState) -> None:
    with taken(REDIRECT_PORTS):
        with sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as other:
            code = shown_code(box, other.start())
        with sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made:
            made.start()
            with pytest.raises(SignInFailedError):
                made.enter(code)  # its verifier is another's


def test_cancelled_while_the_code_is_awaited(box: DropboxState) -> None:
    with (
        taken(REDIRECT_PORTS),
        sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made,
    ):
        code = shown_code(box, made.start())
        made.cancel()

        assert made.wait(SOON) is None
        assert made.enter(code) is None
    assert exchanges(box) == []


def test_a_code_is_entered_only_when_pasting(signing: SignIn) -> None:
    signing.start()
    with pytest.raises(RuntimeError):
        signing.enter("abcdefghijklmnop")


def test_no_port_and_no_pasting_is_a_sign_in_not_to_be_had(box: DropboxState) -> None:
    place = box.place
    with taken(place.redirect_ports):
        made = SignIn(
            place.authorize,
            place.tokens,
            APP_KEY,
            SCOPE,
            PAGES,
            redirect_host="127.0.0.1",
            ports=place.redirect_ports,
            public=box.context,
        )
        with pytest.raises(UnavailableError):
            made.start()


# Who is sent what


def test_the_tokens_go_only_to_the_api_host(box: DropboxState, signing: SignIn) -> None:
    signed_in_by_browser(box, signing)
    signing.wait(SOON)

    assert all(each.authorization is None for each in box.requests(WWW))
    assert all(each.authorization is None for each in box.requests(API, TOKEN_PATH))
    assert box.requests(CONTENT) == [] and box.requests(NOTIFY) == []
    assert [each.path for each in box.requests(WWW)] == [AUTHORIZE]  # the browser's


def test_nothing_says_the_code_the_state_the_tokens_or_the_account(
    box: DropboxState, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    told: list[str] = []
    with sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as made:
        address = made.start()
        _, location, _ = to_allow_page(box, address)
        back(f"http://127.0.0.1:{port_of(address)}/?code=x&state=y")  # turned away
        status, body = back(location)
        signed = made.wait(SOON)
        told += [repr(made), str(made)]
    assert signed is not None and status == 200
    told += [repr(signed), str(signed), body.decode()]
    with (
        taken(REDIRECT_PORTS),
        sign_in(APP_KEY, PAGES, place=box.place, public=box.context) as pasting,
    ):
        pasting.start()
        try:
            pasting.enter("abcdefghijklmnop")
        except SignInFailedError as failure:
            told += [repr(failure), str(failure)]
    told += [repr(App(APP_KEY)), str(App(APP_KEY))]
    captured = capfd.readouterr()
    told += [caplog.text, captured.out, captured.err]
    query = query_of(location)
    secrets = [
        query["code"],
        query["state"],
        signed.refresh_token,
        ACCOUNT,
        APP_KEY,
        made._verifier,  # pyright: ignore[reportPrivateUsage]
        *box.issued,
    ]
    for each in secrets:
        assert not any(each in text for text in told)


# The app as registered


@pytest.fixture
def from_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DROPBOX_VARIABLE, raising=False)
    monkeypatch.delitem(sys.modules, BUILT_IN, raising=False)


@pytest.mark.usefixtures("from_the_source")
def test_without_an_app_key_dropbox_is_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    assert registered() is None
    monkeypatch.setenv(DROPBOX_VARIABLE, "not one")
    assert registered() is None


@pytest.mark.usefixtures("from_the_source")
def test_the_app_as_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DROPBOX_VARIABLE, APP_KEY)

    assert registered() == App(APP_KEY)
    made = App(APP_KEY).target("refresh", lambda _: None)
    try:
        assert isinstance(made, Dropbox)
    finally:
        made.close()
