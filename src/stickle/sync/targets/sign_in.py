"""Signing in to a cloud in the system's browser: OAuth 2.0 with PKCE (S256).

The app opens the address SignIn.start gives in the user's browser. While they
sign in there, a listener on this computer's loopback addresses only
(127.0.0.1 and ::1, on one port; or on 127.0.0.1 alone, on the first free one
of the ports the app has registered) waits for the cloud to send the browser
back. Only an answer with this sign-in's state ends it; anything else is
answered with a page, and the wait goes on. A code is exchanged for tokens at
the cloud's token address, with the verifier only this sign-in knows, and only
then is the browser shown its page: signed in, or not. However the sign-in
ends, the listener is closed.

Should every registered port be taken, a cloud that can show the user the
code instead (Dropbox) is asked to: the user enters it in the app (enter), and
it is exchanged the same way. Only this sign-in's verifier makes it good.

There is no client secret: the app is a public client, which PKCE is for. What
is kept of a sign-in is the refresh token, and the name of the account if the
cloud tells it: in an ID token, which comes from the token address itself over
checked https, so its signature need not be checked (OpenID Connect Core,
3.1.3.7); or as the cloud answers when asked with the access token.

Nothing here logs. The code, the state, the verifier, the tokens and the
account's name are in no error, repr or page, and the cloud's own words for
an error are not passed on either.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import socket
import socketserver
import ssl
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from stickle.platform.tls import https_context
from stickle.sync.targets.base import (
    NoAccessError,
    TargetError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.oauth import TIMEOUT, TokenAddress, error_code, post_form

HELD = 10.0  # seconds a connection to the listener has to ask what it asks
MOST_FIELDS = 16  # in the query of an answer, at most
ACCOUNT_MOST = 256  # characters of an account's name, at most

_TRIES = 8  # at finding a port free on both loopback addresses
_POLL = 0.1  # seconds the listener takes to notice it is to close, at most
_DECLINED = "access_denied"
_CODE = re.compile(r"[A-Za-z0-9._~+/=-]{8,512}")  # a code a user enters, as clouds make them
_APP_TURNED_AWAY = frozenset({"invalid_client", "unauthorized_client"})

# The page loads nothing, runs nothing and goes nowhere.
_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
_STYLE = (
    "body{margin:0;min-height:100vh;display:flex;align-items:center;"
    "justify-content:center;padding:2rem;box-sizing:border-box;"
    "font:1.25rem/1.5 system-ui,sans-serif}p{max-width:32rem;text-align:center}"
)


class SignInFailedError(TargetError):
    """The sign-in ended without tokens: the user said no, or the cloud did."""

    def __init__(self, *args: object, declined: bool = False) -> None:
        super().__init__(*args)
        self.declined = declined  # the user said no to the app


@dataclass(frozen=True)
class Pages:
    """What the browser is shown at the end, in the app's language."""

    language: str  # as for a page's lang: "ko", "en"
    done: str
    failed: str


@dataclass(frozen=True)
class SignedIn:
    refresh_token: str = field(repr=False)
    account: str | None = field(default=None, repr=False)  # as the user knows it, if told


def page(language: str, text: str) -> bytes:
    """A page of one sentence, with nothing from anywhere else."""
    return (
        "<!doctype html>"
        f'<html lang="{escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>Stickle</title><style>{_STYLE}</style></head>"
        f"<body><p>{escape(text)}</p></body></html>"
    ).encode()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _fields(query: str) -> dict[str, str]:
    """An answer's query: each name given once, the others left out."""
    try:
        given = parse_qs(query, keep_blank_values=True, max_num_fields=MOST_FIELDS, errors="strict")
    except ValueError:  # too many, or not UTF-8
        return {}
    return {name: values[0] for name, values in given.items() if len(values) == 1}


def named(value: object) -> str | None:
    """An account's name as the user knows it, if it is one to show."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= ACCOUNT_MOST and value.isprintable() else None


def id_token_account(tokens: Mapping[str, object]) -> str | None:
    """The account's name, from the ID token in a token address's answer."""
    return _account(tokens.get("id_token"))


def _account(token: object) -> str | None:
    """The account's name, from an ID token, as the user knows it."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:  # not base64, not UTF-8 or not JSON
        return None
    if not isinstance(claims, dict):
        return None
    for name in ("preferred_username", "email"):
        found = named(cast(dict[str, object], claims).get(name))
        if found is not None:
            return found
    return None


def _exchange_error(status: int, data: bytes, wait: float | None) -> TargetError:
    if status in (400, 401):
        return NoAccessError() if error_code(data) in _APP_TURNED_AWAY else SignInFailedError()
    if status == 429 or (status == 503 and wait is not None):
        return TooBusyError(wait)
    return UnavailableError(answered=True)


class _Handler(BaseHTTPRequestHandler):
    timeout = HELD
    server_version = "Stickle"

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        pass  # a request's line holds the code

    def do_GET(self) -> None:
        cast(_Listener, self.server).answering(self)


class _Listener(ThreadingHTTPServer):
    """One loopback address, for one sign-in."""

    daemon_threads = True
    allow_reuse_address = False  # on Windows, that would let another take the port too
    allow_reuse_port = False
    answering: Callable[[BaseHTTPRequestHandler], None]

    def server_bind(self) -> None:
        if sys.platform == "win32":
            # No other program may listen on the port too and hear the browser.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        # Not HTTPServer's own, which looks the host's name up.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = cast(tuple[str, int], self.server_address)[1]

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # a browser gone midway; nothing to tell


class _Listener6(_Listener):
    address_family = socket.AF_INET6


def listen_on(ports: Sequence[int]) -> list[_Listener]:
    """A listener on 127.0.0.1, on the first of these ports that is free;
    none if none is."""
    for port in ports:
        try:
            return [_Listener(("127.0.0.1", port), _Handler)]
        except OSError:
            continue  # taken, or not to be had
    return []


def listen() -> list[_Listener]:
    """Listeners on one port, on both loopback addresses if this computer has
    IPv6 (where a browser may take "localhost" to be), else on 127.0.0.1."""
    for _ in range(_TRIES):
        try:
            six = _Listener6(("::1", 0), _Handler)
        except OSError:
            break  # no IPv6 loopback here
        try:
            return [_Listener(("127.0.0.1", six.server_port), _Handler), six]
        except OSError:
            six.server_close()  # the port is taken on 127.0.0.1: another one
    return [_Listener(("127.0.0.1", 0), _Handler)]


class SignIn:
    """One sign-in, from the address for the browser to the tokens. start, then
    wait (or, when pasting, enter the code the user was shown); cancel from any
    thread (as when the user closes the window)."""

    def __init__(
        self,
        authorize: str,
        tokens: TokenAddress,
        client_id: str,
        scope: str,
        pages: Pages,
        *,
        extra: Mapping[str, str] | None = None,
        redirect_host: str = "localhost",
        ports: Sequence[int] = (),
        paste: bool = False,
        account: Callable[[Mapping[str, object]], str | None] = id_token_account,
        public: Callable[[], ssl.SSLContext] = https_context,
        timeout: float = TIMEOUT,
    ) -> None:
        """authorize: the cloud's address to sign in at (https, no query).
        extra: more of the query for it, as the cloud wants. redirect_host: as
        the cloud has it registered for the app. ports: those registered with
        it, if it takes no other (then redirect_host is 127.0.0.1); none: any.
        paste: with them all taken, the cloud shows the user the code instead.
        account: the account's name, from the token address's answer (what
        goes wrong there leaves it unknown). timeout: seconds, for each step of
        the exchange."""
        self._authorize = authorize
        self._tokens = tokens
        self._client_id = client_id
        self._scope = scope
        self._pages = pages
        self._extra = dict(extra or {})
        self._redirect_host = redirect_host
        self._ports = tuple(ports)
        self._paste = paste
        self._account_of = account
        self._public = public
        self._timeout = timeout
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(64)  # 86 characters: RFC 7636 asks 43 to 128
        self._lock = threading.Lock()
        self._closing = threading.Lock()
        self._entering = threading.Lock()  # one code entered at a time
        self._ended = threading.Event()
        self._started = False
        self._pasting = False
        self._taken = False  # an answer with the state came
        self._cancelled = False
        self._outcome: SignedIn | TargetError | None = None
        self._listeners: list[_Listener] = []
        self._port = 0

    def __repr__(self) -> str:
        return "SignIn()"

    def __enter__(self) -> SignIn:
        return self

    def __exit__(self, *exception: object) -> None:
        self.cancel()

    @property
    def _redirect(self) -> str:
        return f"http://{self._redirect_host}:{self._port}"

    @property
    def pasting(self) -> bool:
        """Whether the user is to enter the code the cloud shows them (every
        registered port taken), rather than the browser bringing it back."""
        with self._lock:
            return self._pasting

    def start(self) -> str:
        """Listening from now on (or else, when pasting, nothing): the address
        to open in the browser."""
        with self._lock:
            if self._started or self._cancelled:
                raise RuntimeError("a sign-in starts once")
            self._started = True
        with self._closing:
            try:
                self._listeners = listen_on(self._ports) if self._ports else listen()
            except OSError:
                raise UnavailableError() from None
            if not self._listeners:
                if not self._paste:
                    raise UnavailableError()
                with self._lock:
                    self._pasting = True
            else:
                self._port = self._listeners[0].server_port
            for listener in self._listeners:
                listener.answering = self._answer
                threading.Thread(
                    target=listener.serve_forever, kwargs={"poll_interval": _POLL}, daemon=True
                ).start()
        query = {
            "client_id": self._client_id,
            "response_type": "code",
            **({} if self.pasting else {"redirect_uri": self._redirect}),
            "scope": self._scope,
            "state": self._state,
            "code_challenge": _challenge(self._verifier),
            "code_challenge_method": "S256",
            **self._extra,
        }
        return f"{self._authorize}?{urlencode(query, quote_via=quote)}"

    def enter(self, code: str) -> SignedIn | None:
        """When pasting: the code the user was shown, exchanged; None if the
        sign-in was cancelled meanwhile. One that is no code, or that the cloud
        refuses, raises (SignInFailedError), and another may be entered."""
        with self._lock:
            if not self._pasting:
                raise RuntimeError("the browser brings the code back")
        code = code.strip()
        if not _CODE.fullmatch(code):
            raise SignInFailedError()  # sent nowhere
        with self._entering:
            with self._lock:
                if self._cancelled:
                    return None
                if self._outcome is not None:
                    raise RuntimeError("signed in already")
            signed = self._exchange(code)
            with self._lock:
                if self._cancelled:
                    return None
                self._outcome = signed
            self._ended.set()
            return signed

    def wait(self, timeout: float | None = None) -> SignedIn | None:
        """The tokens, once the sign-in ended with them; None if it was cancelled,
        or given up on after timeout seconds. A sign-in that failed raises."""
        if not self._ended.wait(timeout):
            self.cancel()
            self._ended.wait()
        with self._lock:
            outcome = None if self._cancelled else self._outcome
        if isinstance(outcome, TargetError):
            raise outcome
        return outcome

    def cancel(self) -> None:
        """Ends the sign-in, unless an answer ended it already; the listener is
        closed when this returns."""
        with self._lock:
            ending = self._outcome is None
            if ending:
                self._cancelled = True
        self._close()
        if ending:
            self._ended.set()

    def _close(self) -> None:
        with self._closing:
            listeners, self._listeners = self._listeners, []
            for listener in listeners:
                listener.shutdown()
                listener.server_close()

    def _answer(self, asked: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(asked.path)
        host = (asked.headers.get("Host") or "").lower()
        if host != f"{self._redirect_host}:{self._port}" or parts.path != "/":
            self._send(asked, 404, None)
            return
        given = _fields(parts.query)
        state = given.get("state", "")
        if not hmac.compare_digest(state.encode("utf-8"), self._state.encode("ascii")):
            self._send(asked, 400, self._pages.failed)  # not this sign-in's: waiting on
            return
        with self._lock:
            taken, self._taken = self._taken, True
        if taken:
            self._send(asked, 400, self._pages.failed)
            return
        outcome = self._outcome_of(given)
        with self._lock:
            kept = not self._cancelled
            if kept:
                self._outcome = outcome
        done = kept and isinstance(outcome, SignedIn)
        self._send(asked, 200 if done else 400, self._pages.done if done else self._pages.failed)
        if kept:
            self._close()
            self._ended.set()

    def _outcome_of(self, given: Mapping[str, str]) -> SignedIn | TargetError:
        error, code = given.get("error"), given.get("code")
        if error is not None or not code:
            if error == _DECLINED:
                return SignInFailedError(declined=True)
            if error in _APP_TURNED_AWAY:
                return NoAccessError()
            return SignInFailedError()
        try:
            return self._exchange(code)
        except TargetError as failure:
            return failure

    def _exchange(self, code: str) -> SignedIn:
        form = {
            "client_id": self._client_id,
            "grant_type": "authorization_code",
            "code": code,
            **({} if self.pasting else {"redirect_uri": self._redirect}),
            "code_verifier": self._verifier,
        }
        context = self._public()
        status, data, wait = post_form(
            self._tokens, urlencode(form).encode("ascii"), context, self._timeout
        )
        if status != 200:
            raise _exchange_error(status, data, wait)
        try:
            answer = json.loads(data)
        except ValueError:
            raise UnavailableError(answered=True) from None
        if not isinstance(answer, dict):
            raise UnavailableError(answered=True)
        tokens = cast(dict[str, object], answer)
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise SignInFailedError()  # nothing to keep the sign-in by
        try:
            account = named(self._account_of(tokens))
        except TargetError:
            account = None  # signed in all the same: only the name is not known
        return SignedIn(refresh, account)

    def _send(self, asked: BaseHTTPRequestHandler, status: int, text: str | None) -> None:
        body = b"" if text is None else page(self._pages.language, text)
        try:
            asked.send_response(status)
            asked.send_header("Content-Type", "text/html; charset=utf-8")
            asked.send_header("Content-Length", str(len(body)))
            asked.send_header("Cache-Control", "no-store")
            asked.send_header("Content-Security-Policy", _POLICY)
            asked.send_header("Referrer-Policy", "no-referrer")
            asked.send_header("X-Content-Type-Options", "nosniff")
            asked.send_header("Connection", "close")
            asked.end_headers()
            asked.wfile.write(body)
        except OSError:
            pass  # the browser went away
        asked.close_connection = True
