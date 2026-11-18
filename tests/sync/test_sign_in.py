"""Signing in to OneDrive in the browser (stickle.sync.targets.sign_in, and
onedrive.sign_in), against Microsoft's sign-in in the test (graph_server). The
test plays the browser: it opens the sign-in page, which sends it back to the
listener with a code, and reads the page the listener answers."""

import logging
import socket
import ssl
import threading
import time
from collections.abc import Generator
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from graph_server import (
    ACCOUNT,
    AUTHORIZE_PATH,
    CLIENT_ID,
    HOST,
    TOKEN_PATH,
    Answer,
    Graph,
    back,
    graph_server,
    to_sign_in_page,
)

from stickle.sync.targets import sign_in as module
from stickle.sync.targets.base import (
    Absent,
    NoAccessError,
    SignInNeededError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.oauth import TokenAddress
from stickle.sync.targets.onedrive import SIGN_IN_SCOPE, OneDrive, sign_in
from stickle.sync.targets.sign_in import (
    Pages,
    SignedIn,
    SignIn,
    SignInFailedError,
    _account,  # pyright: ignore[reportPrivateUsage]
    page,
)

PAGES = Pages("en", "All signed in.", "Not signed in, sadly.")


@pytest.fixture
def graph() -> Generator[Graph]:
    with graph_server() as serving:
        yield serving


@pytest.fixture
def signing(graph: Graph) -> Generator[SignIn]:
    with sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as made:
        yield made


def signed_in_by_browser(graph: Graph, signing: SignIn, **how: str) -> tuple[int, bytes]:
    status, _, body = back(to_sign_in_page(graph, signing.start()), **how)
    return status, body


def port_of(address: str) -> int:
    redirect = parse_qs(urlsplit(address).query)["redirect_uri"][0]
    port = urlsplit(redirect).port
    assert port is not None
    return port


def listening(port: int, via: str = "127.0.0.1") -> bool:
    try:
        socket.create_connection((via, port), timeout=3).close()
    except OSError:
        return False
    return True


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


def exchanges(graph: Graph) -> list[dict[str, str]]:
    """The forms sent to the token address to exchange a code."""
    forms = [
        {name: values[0] for name, values in parse_qs(each.body.decode()).items()}
        for each in graph.asked
        if each.path == TOKEN_PATH
    ]
    return [form for form in forms if form.get("grant_type") == "authorization_code"]


# The address for the browser


def test_the_address_asks_microsoft_for_a_code_with_pkce(graph: Graph, signing: SignIn) -> None:
    address = signing.start()
    parts = urlsplit(address)
    assert (parts.scheme, parts.hostname, parts.port) == ("https", HOST, graph.port)
    assert parts.path == AUTHORIZE_PATH
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    assert set(query) == {
        "client_id",
        "response_type",
        "redirect_uri",
        "response_mode",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "prompt",
    }
    assert query["client_id"] == CLIENT_ID
    assert (query["response_type"], query["response_mode"]) == ("code", "query")
    assert query["redirect_uri"] == f"http://localhost:{port_of(address)}"
    assert query["scope"].split() == [
        "openid",
        "profile",
        "Files.ReadWrite.AppFolder",
        "offline_access",
    ]
    assert query["scope"] == SIGN_IN_SCOPE
    assert query["prompt"] == "select_account"  # the user picks the account every time
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43  # SHA-256, base64url without padding
    assert len(query["state"]) >= 43
    assert listening(port_of(address))


def test_each_sign_in_has_its_own_state_and_verifier(graph: Graph) -> None:
    def query_of(made: SignIn) -> dict[str, list[str]]:
        return parse_qs(urlsplit(made.start()).query)

    with (
        sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as one,
        sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as other,
    ):
        first, second = query_of(one), query_of(other)
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]


def test_the_listener_is_on_this_computers_loopback_only(signing: SignIn) -> None:
    signing.start()
    listeners = signing._listeners  # pyright: ignore[reportPrivateUsage]
    addresses = {cast(tuple[str, int], each.server_address)[0] for each in listeners}
    ports = {cast(tuple[str, int], each.server_address)[1] for each in listeners}
    assert addresses == ({"127.0.0.1", "::1"} if has_ipv6_loopback() else {"127.0.0.1"})
    assert len(ports) == 1  # localhost is one port, whichever address it is taken to be


def test_no_other_program_can_listen_on_the_port_too(signing: SignIn) -> None:
    port = port_of(signing.start())
    with socket.socket() as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", port))


# Signed in


def test_a_sign_in_gives_the_refresh_token_and_the_account(graph: Graph, signing: SignIn) -> None:
    status, body = signed_in_by_browser(graph, signing)
    signed = signing.wait(5)
    assert signed is not None
    assert signed.refresh_token in graph.refresh_tokens
    assert signed.account == ACCOUNT
    assert status == 200 and b"All signed in." in body
    (form,) = exchanges(graph)
    assert set(form) == {"client_id", "grant_type", "code", "redirect_uri", "code_verifier"}
    assert form["client_id"] == CLIENT_ID  # and no secret: there is none


def test_the_page_says_signed_in_only_once_the_tokens_are_there(
    graph: Graph, signing: SignIn
) -> None:
    graph.token_answers = [Answer(400, body={"error": "invalid_grant"})]
    status, body = signed_in_by_browser(graph, signing)
    assert status == 400 and b"Not signed in, sadly." in body
    with pytest.raises(SignInFailedError) as failed:
        signing.wait(5)
    assert not failed.value.declined


@pytest.mark.parametrize("via", ["127.0.0.1", "::1"])
def test_the_browser_may_take_localhost_to_either_address(
    graph: Graph, signing: SignIn, via: str
) -> None:
    if via == "::1" and not has_ipv6_loopback():
        pytest.skip("no IPv6 loopback here")
    status, _ = signed_in_by_browser(graph, signing, via=via)
    assert status == 200
    assert signing.wait(5) is not None


def test_the_listener_is_closed_once_signed_in(graph: Graph, signing: SignIn) -> None:
    address = signing.start()
    port = port_of(address)
    location = to_sign_in_page(graph, address)
    assert back(location)[0] == 200
    assert signing.wait(5) is not None
    assert not listening(port)
    if has_ipv6_loopback():
        assert not listening(port, "::1")
    with pytest.raises(OSError):  # the tab reloaded
        back(location)


def test_the_refresh_token_is_onedrives_until_the_user_takes_it_back(
    graph: Graph, signing: SignIn
) -> None:
    signed_in_by_browser(graph, signing)
    signed = signing.wait(5)
    assert signed is not None
    renewed: list[str] = []
    target = OneDrive(
        CLIENT_ID, signed.refresh_token, renewed.append, place=graph.place, public=graph.context
    )
    target.put("a.txt", b"note", Absent())
    assert [entry.path for entry in target.list("")] == ["a.txt"]
    target.close()

    graph.refresh_tokens.clear()  # the user took the app's access away
    graph.expire_access_tokens()
    stopped = OneDrive(
        CLIENT_ID, renewed[-1], renewed.append, place=graph.place, public=graph.context
    )
    with pytest.raises(SignInNeededError):
        list(stopped.list(""))
    asked = len(graph.asked)
    with pytest.raises(SignInNeededError):  # not tried again behind the user's back
        list(stopped.list(""))
    assert len(graph.asked) == asked + 1
    stopped.close()

    with sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as again:
        signed_in_by_browser(graph, again)
        signed_again = again.wait(5)
    assert signed_again is not None
    going = OneDrive(
        CLIENT_ID,
        signed_again.refresh_token,
        renewed.append,
        place=graph.place,
        public=graph.context,
    )
    assert [entry.path for entry in going.list("")] == ["a.txt"]
    going.close()


# What does not end a sign-in


def test_other_requests_are_answered_and_the_sign_in_waits_on(
    graph: Graph, signing: SignIn
) -> None:
    address = signing.start()
    port = port_of(address)
    here = f"http://localhost:{port}"
    location = to_sign_in_page(graph, address)
    state = parse_qs(urlsplit(location).query)["state"][0]
    code = parse_qs(urlsplit(location).query)["code"][0]

    assert back(f"{here}/?{urlencode({'code': code, 'state': 'not-it'})}")[0] == 400
    assert back(f"{here}/?{urlencode({'code': code})}")[0] == 400
    assert back(f"{here}/?code={code}&state={state}&state={state}")[0] == 400  # which one?
    assert back(f"{here}/favicon.ico")[0] == 404
    assert back(f"{here}/elsewhere?{urlencode({'code': code, 'state': state})}")[0] == 404
    assert back(location, host=f"evil.example:{port}")[0] == 404  # not as the browser asks
    assert back(location, method="POST")[0] == 501
    assert exchanges(graph) == []  # none of them was taken for the answer

    status, _, body = back(location)
    assert status == 200 and b"All signed in." in body
    assert signing.wait(5) is not None


def test_a_wrong_answer_is_shown_the_failed_page(graph: Graph, signing: SignIn) -> None:
    port = port_of(signing.start())
    status, headers, body = back(f"http://localhost:{port}/?code=a&state=b")
    assert status == 400 and b"Not signed in, sadly." in body
    assert headers["Content-Type"] == "text/html; charset=utf-8"


def test_only_the_first_answer_counts(graph: Graph, signing: SignIn) -> None:
    location = to_sign_in_page(graph, signing.start())
    first: list[int] = []
    with graph.lock:  # the exchange waits at the token address
        answering = threading.Thread(target=lambda: first.append(back(location)[0]))
        answering.start()
        until = time.monotonic() + 5
        while not signing._taken and time.monotonic() < until:  # pyright: ignore[reportPrivateUsage]
            time.sleep(0.01)
        status, _, body = back(location)
        assert status == 400 and b"Not signed in, sadly." in body
    answering.join(10)
    assert first == [200]
    assert signing.wait(5) is not None
    assert len(exchanges(graph)) == 1


# What ends it without tokens


def test_saying_no_to_the_app(graph: Graph, signing: SignIn) -> None:
    graph.declining = True
    address = signing.start()
    status, _, body = back(to_sign_in_page(graph, address))
    assert status == 400 and b"Not signed in, sadly." in body
    assert b"said no" not in body  # Microsoft's own words are not passed on
    with pytest.raises(SignInFailedError) as failed:
        signing.wait(5)
    assert failed.value.declined
    assert not listening(port_of(address))


def test_the_app_turned_away_at_the_sign_in_page(graph: Graph, signing: SignIn) -> None:
    graph.app_turned_away = True
    status, body = signed_in_by_browser(graph, signing)
    assert status == 400 and b"AADSTS" not in body
    with pytest.raises(NoAccessError):
        signing.wait(5)


@pytest.mark.parametrize("code", ["invalid_client", "unauthorized_client"])
def test_the_app_turned_away_at_the_exchange(graph: Graph, signing: SignIn, code: str) -> None:
    graph.token_answers = [Answer(400, body={"error": code})]
    signed_in_by_browser(graph, signing)
    with pytest.raises(NoAccessError):
        signing.wait(5)


def test_a_code_the_token_address_refuses(graph: Graph, signing: SignIn) -> None:
    graph.token_answers = [
        Answer(400, body={"error": "invalid_grant", "error_description": "<script>x</script>"})
    ]
    status, body = signed_in_by_browser(graph, signing)
    assert status == 400 and b"<script>x" not in body
    with pytest.raises(SignInFailedError):
        signing.wait(5)


def test_the_verifier_is_what_makes_the_code_good(graph: Graph, signing: SignIn) -> None:
    address = signing.start()
    signing._verifier = "another-sign-ins-verifier"  # pyright: ignore[reportPrivateUsage]
    assert back(to_sign_in_page(graph, address))[0] == 400
    with pytest.raises(SignInFailedError):
        signing.wait(5)


def test_an_answer_without_a_refresh_token_is_no_sign_in(graph: Graph, signing: SignIn) -> None:
    graph.token_answers = [Answer(200, body={"access_token": "a", "expires_in": 3600})]
    signed_in_by_browser(graph, signing)
    with pytest.raises(SignInFailedError):
        signing.wait(5)


def test_a_busy_token_address(graph: Graph, signing: SignIn) -> None:
    graph.token_answers = [Answer(429, headers={"Retry-After": "7"})]
    signed_in_by_browser(graph, signing)
    with pytest.raises(TooBusyError) as busy:
        signing.wait(5)
    assert busy.value.retry_after == 7


@pytest.mark.parametrize(
    "answer",
    [
        Answer(500),
        Answer(503),
        Answer(302, headers={"Location": "https://elsewhere.example/token"}),
        Answer(502),
    ],
)
def test_a_failing_token_address(graph: Graph, signing: SignIn, answer: Answer) -> None:
    graph.token_answers = [answer]
    status, _ = signed_in_by_browser(graph, signing)
    assert status == 400
    with pytest.raises(UnavailableError) as failed:
        signing.wait(5)
    assert failed.value.answered


def test_no_token_address_there(graph: Graph) -> None:
    with socket.socket() as taken:
        taken.bind((HOST, 0))
        nowhere = cast(tuple[str, int], taken.getsockname())[1]  # nothing listens on it
        made = SignIn(
            graph.place.authorize,
            TokenAddress(HOST, TOKEN_PATH, nowhere),
            CLIENT_ID,
            SIGN_IN_SCOPE,
            PAGES,
            extra={"response_mode": "query"},
            public=graph.context,
            timeout=5,
        )
        with made:
            status, _ = signed_in_by_browser(graph, made)
            assert status == 400
            with pytest.raises(UnavailableError) as failed:
                made.wait(5)
    assert not failed.value.answered


def test_an_untrusted_token_address_is_sent_nothing(graph: Graph) -> None:
    with sign_in(CLIENT_ID, PAGES, place=graph.place, public=ssl.create_default_context) as made:
        status, _ = signed_in_by_browser(graph, made)
        assert status == 400
        with pytest.raises(UnavailableError):
            made.wait(5)
    assert exchanges(graph) == []


# Cancelled


def test_cancelled_while_waiting(signing: SignIn) -> None:
    port = port_of(signing.start())
    got: list[SignedIn | None] = []
    waiting = threading.Thread(target=lambda: got.append(signing.wait()))
    waiting.start()
    time.sleep(0.1)
    signing.cancel()
    waiting.join(5)
    assert got == [None]
    assert not listening(port)


def test_given_up_after_its_time(signing: SignIn) -> None:
    port = port_of(signing.start())
    assert signing.wait(0.2) is None
    assert not listening(port)


def test_cancelled_while_exchanging(graph: Graph, signing: SignIn) -> None:
    location = to_sign_in_page(graph, signing.start())
    pages: list[tuple[int, dict[str, str], bytes]] = []
    with graph.lock:
        answering = threading.Thread(target=lambda: pages.append(back(location)))
        answering.start()
        until = time.monotonic() + 5
        while not signing._taken and time.monotonic() < until:  # pyright: ignore[reportPrivateUsage]
            time.sleep(0.01)
        signing.cancel()
    answering.join(10)
    assert pages[0][0] == 400 and b"Not signed in, sadly." in pages[0][2]
    assert signing.wait(1) is None


def test_a_sign_in_starts_once(signing: SignIn) -> None:
    signing.start()
    with pytest.raises(RuntimeError):
        signing.start()


def test_a_cancelled_sign_in_does_not_start(graph: Graph) -> None:
    made = sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context)
    made.cancel()
    with pytest.raises(RuntimeError):
        made.start()
    assert made.wait(1) is None


# Where it listens when it cannot have both addresses


def test_only_127_0_0_1_without_ipv6(
    graph: Graph, signing: SignIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_ipv6(*given: object) -> object:
        raise OSError("no IPv6 here")

    monkeypatch.setattr(module, "_Listener6", no_ipv6)
    address = signing.start()
    listeners = signing._listeners  # pyright: ignore[reportPrivateUsage]
    assert [cast(tuple[str, int], each.server_address)[0] for each in listeners] == ["127.0.0.1"]
    status, _, _ = back(to_sign_in_page(graph, address))
    assert status == 200
    assert signing.wait(5) is not None


def test_another_port_when_127_0_0_1_has_it_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    if not has_ipv6_loopback():
        pytest.skip("no IPv6 loopback here")
    real = module._Listener  # pyright: ignore[reportPrivateUsage]
    refused: list[int] = []

    def taken_once(address: tuple[str, int], handler: type) -> object:
        if address[1] and not refused:
            refused.append(address[1])
            raise OSError("taken")
        return real(address, handler)

    monkeypatch.setattr(module, "_Listener", taken_once)
    listeners = module.listen()
    try:
        ports = {cast(tuple[str, int], each.server_address)[1] for each in listeners}
        assert len(listeners) == 2 and len(ports) == 1 and refused and refused[0] not in ports
    finally:
        for each in listeners:
            each.server_close()


# The page


def test_the_page_is_in_the_apps_language_with_nothing_from_elsewhere() -> None:
    shown = page("ko", "<b>로그인</b> & 끝").decode("utf-8")
    assert '<html lang="ko">' in shown
    assert "&lt;b&gt;로그인&lt;/b&gt; &amp; 끝" in shown
    for loading in ("src=", "href=", "url(", "<script", "<link", "<img", "@import"):
        assert loading not in shown


def test_the_page_is_kept_by_nothing_and_loads_nothing(graph: Graph, signing: SignIn) -> None:
    location = to_sign_in_page(graph, signing.start())
    status, headers, _ = back(location)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Server"] == "Stickle"


# The account's name


def test_no_id_token_no_account(graph: Graph, signing: SignIn) -> None:
    graph.claims = None
    signed_in_by_browser(graph, signing)
    signed = signing.wait(5)
    assert signed is not None and signed.account is None


def test_the_account_by_its_email_when_there_is_no_user_name(graph: Graph, signing: SignIn) -> None:
    graph.claims = {"email": "other@example.com"}
    signed_in_by_browser(graph, signing)
    signed = signing.wait(5)
    assert signed is not None and signed.account == "other@example.com"


@pytest.mark.parametrize(
    "token",
    [None, 7, "", "a.b", "a.!!!.c", "a.bm90IGpzb24.c", "a.WzFd.c"],  # not JSON; a list
)
def test_an_id_token_that_is_not_one(token: object) -> None:
    assert _account(token) is None


def test_an_odd_account_name_is_left_out(graph: Graph, signing: SignIn) -> None:
    graph.claims = {"preferred_username": "a\nb", "email": "x" * 300}
    signed_in_by_browser(graph, signing)
    signed = signing.wait(5)
    assert signed is not None and signed.account is None


# Nothing tells


def test_nothing_says_the_code_the_state_the_tokens_or_the_account(
    graph: Graph, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    told: list[str] = []
    with sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as made:
        address = made.start()
        location = to_sign_in_page(graph, address)
        back(f"http://localhost:{port_of(address)}/?code=x&state=y")  # turned away
        status, _, body = back(location)
        signed = made.wait(5)
        told += [repr(made), str(made)]
    assert signed is not None and status == 200
    told += [repr(signed), str(signed), body.decode()]
    graph.declining = True
    with sign_in(CLIENT_ID, PAGES, place=graph.place, public=graph.context) as refused:
        failed_location = to_sign_in_page(graph, refused.start())
        told.append(back(failed_location)[2].decode())
        try:
            refused.wait(5)
        except SignInFailedError as failure:
            told += [repr(failure), str(failure)]
    captured = capfd.readouterr()
    told += [caplog.text, captured.out, captured.err]
    query = parse_qs(urlsplit(location).query)
    secrets = [
        query["code"][0],
        query["state"][0],
        signed.refresh_token,
        ACCOUNT,
        made._verifier,  # pyright: ignore[reportPrivateUsage]
        *graph.issued,
    ]
    for each in secrets:
        assert not any(each in text for text in told)
