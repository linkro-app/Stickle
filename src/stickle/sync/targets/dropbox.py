"""A Dropbox to sync with, through its API (version 2).

The Stickle folder is the app's own folder in the user's Dropbox (Apps/Stickle,
or as the user renamed it): the only one the app may reach, and the same for
every device signed in to the same account.

Each of the four things a target does takes one request:

- list: the folder's files, a page at a time.
- get: the download, which comes with the file's rev.
- put, a file made: an upload that fails if a file is there, even one of the
  same bytes (strict_conflict).
- put, a file written over: an upload made only if the file is still at the
  rev read, and refused if it was deleted since (strict_conflict). A file
  written over without a tag is refused unsent: read, it has one.
- delete: only if the file is still at the rev read (parent_rev); without a
  tag, as it is. Should Dropbox answer that there is no file, with a rev given,
  it is looked for (one request more): there, the delete is refused.

Conditions are kept. A rev is a version of the file: a write of other bytes
leaves a new one, and the same bytes again keep it.

What changed elsewhere is not asked here: sync reads devices/, as for the other
clouds. A feed (feed()) tells when a folder changes, waiting on Dropbox's long
poll, for sync not to look every few minutes (stickle.sync.watcher). It knows
the revs this target wrote, to tell its own writes from others'.

The refresh token comes from a sign-in in the browser (sign_in): the browser
comes back to one of the ports registered for the app, or the user enters the
code Dropbox shows them. Dropbox gives no new one when the access token is made
again: it lasts until revoked (revoke()).

Who is talked to, before anything is sent: only Dropbox's own API hosts, over
https, with a certificate a public authority issued for them. The API host
(tokens, the account's name, listings, deletes) and the content host (reads,
writes) are sent the access token; the notify host (the long poll) no token at
all, only its cursor. No redirect is followed. Dropbox's own site, where the
user signs in, is the browser's to open.

Nothing here logs, and no error says what a token, a path, a cursor or an
account is.
"""

import contextlib
import http.client
import io
import json
import re
import select
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import cast

from stickle.cloud_apps import dropbox_app_key
from stickle.core.cloud_time import date_millis
from stickle.platform.tls import https_context
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Condition,
    CursorExpiredError,
    Entry,
    FullError,
    NoAccessError,
    NotThereError,
    Read,
    RefusedError,
    SignInNeededError,
    TargetError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
    Waited,
)
from stickle.sync.targets.oauth import TokenAddress, Tokens, waiting
from stickle.sync.targets.sign_in import Pages, SignIn

TIMEOUT = 30.0  # seconds, for each step of a request
# Seconds a connection kept from a request may lie idle and still be used for
# the next: longer, and it may have been cut off unnoticed as the network changed
# (a new one costs a handshake; servers mostly close idle ones sooner anyway).
IDLE_REOPEN = 20.0
PAGE = 2000  # files on a page of a listing, at most: Dropbox's own most
WAIT = 480  # seconds a long poll waits for a change, at most: Dropbox's own most
# Seconds a long poll's answer is waited for: Dropbox adds up to 90 to its wait,
# for its clients not to come back all at once.
WAIT_TIMEOUT = WAIT + 90 + TIMEOUT
KEPT_REVS = 64  # of what this target wrote, to tell its own writes in a feed
_MOST = 1024 * 1024  # bytes of a long poll's answer read, at most
# A long poll's connection, quiet this many seconds, has the system ask after
# the other end every so many seconds, so many times, before giving it up: a
# connection the network went from under (another Wi-Fi, another address)
# fails within two minutes, not only when the wait's time is up. Not every
# system tells a connection that only reads that its address is gone.
KEEPALIVE_IDLE = 60
KEEPALIVE_EVERY = 10
KEEPALIVE_PROBES = 3

# The ports the app has registered with Dropbox, each as http://127.0.0.1:<port>:
# Dropbox sends the browser back to no other. Below every system's ephemeral
# ports, apart from each other, none a known program's.
REDIRECT_PORTS = (17391, 21587, 24683, 28749, 31253)
# What the app asks of the account: to read and write files in its folder, and
# to know the account's name. Named, Dropbox grants these and no more.
SCOPE = "files.metadata.read files.content.read files.content.write account_info.read"

_USER_AGENT = "Stickle"
_idle_now = time.monotonic  # how long a kept connection lay idle (tests set it)
_REV = re.compile(r"[0-9a-f]{9,}")  # as Dropbox's revs are; nothing else is one
_TOKEN_PATH = "/oauth2/token"
_TOKEN_TURNED_DOWN = frozenset({"expired_access_token", "invalid_access_token"})

# What a connection kept since an earlier request meets if the server closed it
# meanwhile.
_CLOSED_MEANWHILE = (
    http.client.BadStatusLine,  # RemoteDisconnected is one
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


@dataclass(frozen=True)
class Place:
    """Dropbox's addresses: as they are, or a test's."""

    api: str = "api.dropboxapi.com"
    content: str = "content.dropboxapi.com"
    notify: str = "notify.dropboxapi.com"
    www: str = "www.dropbox.com"  # the browser's, to sign in at: the app sends it nothing
    # Tests only: other ports than https's own, for the API, content and notify,
    # and for the sign-in page.
    ports: tuple[int, int, int] | None = None
    www_port: int | None = None
    redirect_ports: tuple[int, ...] = REDIRECT_PORTS

    @property
    def tokens(self) -> TokenAddress:
        return TokenAddress(self.api, _TOKEN_PATH, self.port(0))

    @property
    def authorize(self) -> str:
        port = "" if self.www_port is None else f":{self.www_port}"
        return f"https://{self.www}{port}/oauth2/authorize"

    @property
    def hosts(self) -> list[str]:
        """Where a target connects, as told to the user."""
        return [self.api, self.content, self.notify]

    def port(self, which: int) -> int | None:
        return None if self.ports is None else self.ports[which]


DROPBOX = Place()


def sign_in(
    app_key: str,
    pages: Pages,
    *,
    place: Place = DROPBOX,
    public: Callable[[], ssl.SSLContext] = https_context,
    timeout: float = TIMEOUT,
) -> SignIn:
    """A sign-in to a Dropbox account, with a refresh token to keep (offline).
    The user is shown the page to allow Stickle each time, even when they did
    before: else it would go on to the account the browser is signed in to,
    without saying which. With every registered port taken, Dropbox shows them
    the code to enter instead."""
    return SignIn(
        place.authorize,
        place.tokens,
        app_key,
        SCOPE,
        pages,
        extra={"token_access_type": "offline", "force_reapprove": "true", "locale": pages.language},
        redirect_host="127.0.0.1",
        ports=place.redirect_ports,
        paste=True,
        account=lambda tokens: _account(tokens, place, public, timeout),
        public=public,
        timeout=timeout,
    )


def _account(
    tokens: Mapping[str, object],
    place: Place,
    public: Callable[[], ssl.SSLContext],
    timeout: float,
) -> str | None:
    """The account's email address, as Dropbox tells it to the access token a
    sign-in was given; None if it does not."""
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    host = _Host(place.api, place.port(0), public, timeout)
    try:
        answer = host.ask(
            "/2/users/get_current_account",
            b"null",
            {"Content-Type": "application/json", "Authorization": f"Bearer {access}"},
        )
    finally:
        host.close()
    if answer.status != 200:
        return None
    said = _json(answer.data)
    email = cast(dict[str, object], said).get("email") if isinstance(said, dict) else None
    return email if isinstance(email, str) else None


@dataclass(frozen=True)
class _Answer:
    status: int
    data: bytes
    wait: float | None  # as Retry-After asks
    result: str | None  # Dropbox-API-Result: a download's file, as Dropbox has it


def _json(data: bytes | str | None) -> object:
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _object(data: bytes | str | None) -> dict[str, object]:
    """A JSON object Dropbox answered."""
    answer = _json(data)
    if not isinstance(answer, dict):
        raise UnavailableError(answered=True)
    return cast(dict[str, object], answer)


def _text(answer: dict[str, object], name: str) -> str | None:
    value = answer.get(name)
    return value if isinstance(value, str) and value else None


def _rev(answer: dict[str, object]) -> str | None:
    rev = _text(answer, "rev")
    return rev if rev is not None and _REV.fullmatch(rev) else None


def _tags(error: object) -> set[str]:
    """Every ".tag" in what Dropbox said went wrong: what, and why."""
    found: set[str] = set()
    left = [error]
    while left:
        each = left.pop()
        if isinstance(each, dict):
            inside = cast(dict[str, object], each)
            tag = inside.get(".tag")
            if isinstance(tag, str):
                found.add(tag)
            left.extend(inside.values())
        elif isinstance(each, list):
            left.extend(cast(list[object], each))
    return found


def _error_tags(answer: _Answer) -> set[str]:
    said = _json(answer.data)
    if not isinstance(said, dict):
        return set()
    return _tags(cast(dict[str, object], said).get("error"))


def _retry_after(answer: _Answer) -> float | None:
    """Seconds to wait, as the header asks, or else as Dropbox's answer says."""
    if answer.wait is not None:
        return answer.wait
    said = _json(answer.data)
    if not isinstance(said, dict):
        return None
    error = cast(dict[str, object], said).get("error")
    if not isinstance(error, dict):
        return None
    after = cast(dict[str, object], error).get("retry_after")
    if isinstance(after, bool) or not isinstance(after, int | float) or after < 0:
        return None
    return float(after)


def _renewable(answer: _Answer) -> bool:
    """A 401 that another access token may get past: the one sent ran out, or
    is not known (not, say, a scope the app was never given)."""
    tags = _error_tags(answer)
    return not tags or bool(tags & _TOKEN_TURNED_DOWN)


def _error(answer: _Answer) -> TargetError:
    if answer.status == 409:  # what went wrong with what was asked: said in the answer
        tags = _error_tags(answer)
        if "too_many_write_operations" in tags:
            return TooBusyError(_retry_after(answer))
        if "insufficient_space" in tags:
            return FullError()
        if "conflict" in tags:
            return RefusedError()
        if "not_found" in tags or "not_file" in tags:
            return NotThereError()
        if "reset" in tags:
            return CursorExpiredError()
        if "no_write_permission" in tags:
            return NoAccessError()
        return UnavailableError(answered=True)
    match answer.status:
        case 401:
            return TokenExpiredError() if _renewable(answer) else NoAccessError()
        case 403:
            return NoAccessError()
        case 429:
            return TooBusyError(_retry_after(answer))
        case 503 if answer.wait is not None:
            return TooBusyError(answer.wait)
        case 507:
            return FullError()
        case _:
            return UnavailableError(answered=True)


def _parts(path: str) -> list[str]:
    parts = path.split("/") if path else []
    if any(part in ("", ".", "..") or "\\" in part or not part.isprintable() for part in parts):
        raise ValueError("not a path in the Stickle folder")
    return parts


def _arg(args: dict[str, object]) -> str:
    """The arguments as a header takes them: ASCII only, as Dropbox asks."""
    return json.dumps(args, ensure_ascii=True).replace("\x7f", "\\u007f")


def _read(response: http.client.HTTPResponse) -> _Answer:
    data = response.read()
    return _Answer(
        response.status,
        data,
        waiting(response.getheader("Retry-After"), response.getheader("Date")),
        response.getheader("Dropbox-API-Result"),
    )


def _keep_alive(sock: socket.socket) -> None:
    """The system asks after the other end of the connection while it is quiet
    (TCP keepalive), as often as above where the system lets that be said."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for option, value in (
        (idle, KEEPALIVE_IDLE),  # macOS names it TCP_KEEPALIVE
        (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_EVERY),
        (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_PROBES),
    ):
        if isinstance(option, int):
            with contextlib.suppress(OSError):  # an older system: its own timings
                sock.setsockopt(socket.IPPROTO_TCP, option, value)


class _Received:
    """Bytes come over a socket, as http.client reads a response from one."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def makefile(self, *_given: object, **_named: object) -> io.BytesIO:
        return io.BytesIO(self._data)


def _answered(data: bytes, closed: bool) -> _Answer | None:
    """The answer in what came so far; None: not all of it yet. closed: no
    more is coming."""
    if b"\r\n\r\n" not in data:
        if closed:
            raise UnavailableError()
        return None
    response = http.client.HTTPResponse(cast(socket.socket, _Received(data)), method="POST")
    try:
        response.begin()
        if response.length is None and not response.chunked and not closed:
            return None  # its end is where the connection closes
        body = response.read()
    except http.client.IncompleteRead:
        if closed:
            raise UnavailableError() from None
        return None
    except http.client.HTTPException:
        raise UnavailableError() from None
    return _Answer(
        response.status,
        body,
        waiting(response.getheader("Retry-After"), response.getheader("Date")),
        None,
    )


class _Host:
    """One of Dropbox's hosts over https, a connection to it kept between requests."""

    def __init__(
        self,
        host: str,
        port: int | None,
        context: Callable[[], ssl.SSLContext],
        timeout: float,
        heard: Callable[[int, int], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._context = context
        self._timeout = timeout
        self._heard = heard
        self._kept: http.client.HTTPSConnection | None = None
        self._kept_at = 0.0  # when it was last used

    def close(self) -> None:
        if self._kept is not None:
            self._kept.close()
            self._kept = None

    def ask(self, path: str, body: bytes, headers: dict[str, str]) -> _Answer:
        """The request made (all of Dropbox's are POSTs) and its answer read. A
        connection kept from before may have been closed by Dropbox meanwhile:
        then once more on a new one. If the request did get through, its
        condition or the merge that follows deals with it, as with any answer lost."""
        sent = {"User-Agent": _USER_AGENT, **headers}
        connection, may_be_closed = self._kept, self._kept is not None
        self._kept = None
        if connection is not None and _idle_now() - self._kept_at > IDLE_REOPEN:
            connection.close()  # idle a while (see IDLE_REOPEN): a new one
            connection, may_be_closed = None, False
        while True:
            try:
                if connection is None:
                    connection = http.client.HTTPSConnection(
                        self._host, self._port, timeout=self._timeout, context=self._context()
                    )
                began = time.monotonic()
                connection.request("POST", path, body=body, headers=sent)
                response = connection.getresponse()
                answer = _read(response)
            except _CLOSED_MEANWHILE:
                if connection is not None:
                    connection.close()
                connection = None
                if not may_be_closed:
                    raise UnavailableError() from None
                may_be_closed = False
            except OSError, http.client.HTTPException:  # ssl's errors among them
                if connection is not None:
                    connection.close()
                raise UnavailableError() from None
            else:
                self._tell_time(response, began)
                if response.will_close:
                    connection.close()
                else:
                    self._kept = connection
                    self._kept_at = _idle_now()
                return answer

    def _tell_time(self, response: http.client.HTTPResponse, began: float) -> None:
        if self._heard is None:
            return
        date = date_millis(response.getheader("Date"))
        if date is not None:
            self._heard(date, round((time.monotonic() - began) * 1000))


class _Shared:
    """What a target and its feeds share, each on its own thread: the sign-in's
    tokens, the check of certificates, and the revs the target wrote."""

    def __init__(self, public: Callable[[], ssl.SSLContext]) -> None:
        self._public = public
        self._lock = threading.Lock()  # for the tokens, held while they are made again
        self._context_lock = threading.Lock()  # apart: tokens made again need the context
        self._revs = threading.Condition(threading.Lock())  # apart: a write is not held up
        self._context: ssl.SSLContext | None = None
        self._tokens: Tokens | None = None
        self._written: deque[tuple[str, str]] = deque(maxlen=KEPT_REVS)
        self._writes = 0  # begun so far
        self._writing: set[int] = set()  # sent, their answers not in yet

    def signed_in(self, tokens: Tokens) -> None:
        self._tokens = tokens

    def context(self) -> ssl.SSLContext:
        with self._context_lock:
            if self._context is None:
                self._context = self._public()
            return self._context

    def access(self) -> str:
        assert self._tokens is not None
        with self._lock:
            return self._tokens.access()

    def renew(self, turned_down: str) -> str:
        """Another access token than the one turned down: made again, unless
        another thread made one meanwhile."""
        assert self._tokens is not None
        with self._lock:
            now = self._tokens.access()
            return now if now != turned_down else self._tokens.renew()

    def writing(self) -> int:
        """A write about to be sent: until its answer is in (done), a file listed
        may be what it wrote."""
        with self._revs:
            self._writes += 1
            self._writing.add(self._writes)
            return self._writes

    def wrote(self, path_lower: str, rev: str) -> None:
        with self._revs:
            self._written.append((path_lower, rev))
            self._revs.notify_all()

    def done(self, write: int) -> None:
        with self._revs:
            self._writing.discard(write)
            self._revs.notify_all()

    def mine(self, path_lower: str, rev: str, seconds: float) -> bool:
        """Whether this target wrote it, at that rev, of what was listed before
        now. Dropbox may tell of a write before its answer is in, so this waits
        for the writes under way now (not those begun later, which were not
        listed), for that many seconds at most: then the file is another's."""
        until = time.monotonic() + seconds
        with self._revs:
            under_way = set(self._writing)
            while (path_lower, rev) not in self._written:
                left = until - time.monotonic()
                if not under_way & self._writing or left <= 0:
                    return False
                self._revs.wait(left)
            return True


def _signed(
    shared: _Shared, host: _Host, path: str, body: bytes, headers: dict[str, str]
) -> _Answer:
    """A request with the access token; once more with another if Dropbox turns
    that one down."""
    token = shared.access()
    answer = host.ask(path, body, {**headers, "Authorization": f"Bearer {token}"})
    if answer.status == 401 and _renewable(answer):
        token = shared.renew(token)
        answer = host.ask(path, body, {**headers, "Authorization": f"Bearer {token}"})
    return answer


def _rpc(shared: _Shared, host: _Host, path: str, args: dict[str, object] | None) -> _Answer:
    body = b"null" if args is None else json.dumps(args).encode()
    return _signed(shared, host, path, body, {"Content-Type": "application/json"})


class Dropbox:
    """A target for one signed-in account, used from one thread at a time (and
    its feeds each from another). A connection to each of the API and content
    hosts is kept between requests (close it when done)."""

    def __init__(
        self,
        app_key: str,
        refresh_token: str,
        renewed: Callable[[str], None],
        *,
        place: Place = DROPBOX,
        folder: str = "",
        public: Callable[[], ssl.SSLContext] = https_context,
        heard: Callable[[int, int], None] | None = None,
        timeout: float = TIMEOUT,
        page: int = PAGE,
    ) -> None:
        """renewed: told a new refresh token should Dropbox ever give one, to be
        kept in place of the last. folder: in the app's folder, for tests only.
        public: what checks a public authority's certificate. heard: told a
        response's Date, as milliseconds, and how many milliseconds it took.
        timeout: seconds, for each step of a request."""
        self._place = place
        self._folder = _parts(folder)
        self._shared = _Shared(public)
        self._shared.signed_in(
            Tokens(
                place.tokens,
                app_key,
                refresh_token,
                renewed,
                public=self._shared.context,
                timeout=timeout,
            )
        )
        self._timeout = timeout
        self._page = page
        self._api = _Host(place.api, place.port(0), self._shared.context, timeout, heard)
        self._content = _Host(place.content, place.port(1), self._shared.context, timeout, heard)

    def __repr__(self) -> str:
        return "Dropbox()"

    @property
    def keeps_conditions(self) -> bool:
        return True

    @property
    def unique_tags(self) -> bool:
        return True

    def close(self) -> None:
        self._api.close()
        self._content.close()

    # The four things a target does

    def list(self, folder: str) -> Generator[Entry]:
        args: dict[str, object] = {
            "path": self._path(folder),
            "recursive": False,
            "include_deleted": False,
            "limit": self._page,
        }
        answer = _rpc(self._shared, self._api, "/2/files/list_folder", args)
        if answer.status == 409 and "not_found" in _error_tags(answer):
            return  # none made yet
        while True:
            if answer.status != 200:
                error = _error(answer)
                # A listing's cursor too old by its next page: this round fails for now.
                raise (
                    UnavailableError(answered=True)
                    if isinstance(error, CursorExpiredError)
                    else error
                )
            page = _object(answer.data)
            files = page.get("entries")
            if not isinstance(files, list):
                raise UnavailableError(answered=True)
            entries = [self._entry(folder, each) for each in cast(list[object], files)]
            more, cursor = page.get("has_more") is True, _text(page, "cursor")
            yield from (entry for entry in entries if entry is not None)
            if not more:
                return
            if cursor is None:
                raise UnavailableError(answered=True)
            answer = _rpc(
                self._shared, self._api, "/2/files/list_folder/continue", {"cursor": cursor}
            )

    def get(self, path: str) -> Read:
        answer = _signed(
            self._shared,
            self._content,
            "/2/files/download",
            b"",
            {"Dropbox-API-Arg": _arg({"path": self._path(path)})},
        )
        if answer.status != 200:
            raise _error(answer)  # none there, or a folder of that name: NotThereError
        rev = _rev(_object(answer.result))
        if rev is None:
            raise UnavailableError(answered=True)
        return Read(answer.data, rev)

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        if not data:
            raise ValueError("nothing to write")
        mode: object
        match condition:
            case Absent():
                mode = "add"
            case AsRead(tag=str() as tag) if _REV.fullmatch(tag):
                mode = {".tag": "update", "update": tag}
            case AsRead():
                raise RefusedError()  # no tag, or none Dropbox gives: not as read
        args: dict[str, object] = {
            "path": self._path(path),
            "mode": mode,
            "autorename": False,
            "mute": True,  # no word of it to the user's other Dropbox apps
            "strict_conflict": True,
        }
        write = self._shared.writing()
        try:
            answer = _signed(
                self._shared,
                self._content,
                "/2/files/upload",
                data,
                {"Dropbox-API-Arg": _arg(args), "Content-Type": "application/octet-stream"},
            )
            if answer.status != 200:
                raise _error(answer)
            written = _object(answer.data)
            rev, path_lower = _rev(written), _text(written, "path_lower")
            if rev is None:
                raise UnavailableError(answered=True)  # written, but at what rev is not known
            if path_lower is not None:
                self._shared.wrote(path_lower, rev)
            return rev
        finally:
            self._shared.done(write)

    def delete(self, path: str, condition: AsRead) -> None:
        if not path:
            raise ValueError("the Stickle folder itself")
        tag = condition.tag
        if tag is not None and not _REV.fullmatch(tag):
            # Not a rev: no file is at it, so this is refused if there is a file.
            if self._there(path):
                raise RefusedError()
            raise NotThereError()
        args: dict[str, object] = {"path": self._path(path)}
        if tag is not None:
            args["parent_rev"] = tag
        answer = _rpc(self._shared, self._api, "/2/files/delete_v2", args)
        if answer.status == 200:
            return
        error = _error(answer)
        if isinstance(error, NotThereError) and tag is not None and self._there(path):
            raise RefusedError()  # there, at another rev
        raise error

    # Beside them

    def feed(self, folder: str) -> DropboxFeed:
        """What tells when folder changes, to be used from another thread."""
        return DropboxFeed(self._path(folder), self._shared, self._place, self._timeout)

    def revoke(self) -> None:
        """Signs Stickle out of the account: the refresh token, and every access
        token made from it, stop working. Signed out already, there is nothing to do."""
        try:
            answer = _rpc(self._shared, self._api, "/2/auth/token/revoke", None)
        except SignInNeededError, NoAccessError:
            return
        if answer.status in (200, 401):
            return
        raise _error(answer)

    # Below them

    def _path(self, path: str) -> str:
        """Where Dropbox has it: "/" and the path in the app's folder; "" for that folder."""
        parts = [*self._folder, *_parts(path)]
        return "/" + "/".join(parts) if parts else ""

    def _there(self, path: str) -> bool:
        answer = _rpc(self._shared, self._api, "/2/files/get_metadata", {"path": self._path(path)})
        if answer.status == 200:
            return True
        error = _error(answer)
        if isinstance(error, NotThereError):
            return False
        raise error

    def _entry(self, folder: str, listed: object) -> Entry | None:
        """A file as listed; None for a folder, or a name that could not be a file's."""
        if not isinstance(listed, dict):
            return None
        item = cast(dict[str, object], listed)
        if item.get(".tag") != "file":
            return None
        name = _text(item, "name")
        if name is None or name in (".", "..") or "/" in name or "\\" in name:
            return None
        size = item.get("size")
        bytes_ = size if isinstance(size, int) and not isinstance(size, bool) else 0
        return Entry(f"{folder}/{name}" if folder else name, _rev(item), bytes_)


class DropboxFeed:
    """Tells when a folder changes, by Dropbox's long poll: used from one thread
    at a time, but for cut(). A connection to the API host is kept between
    requests; each wait has one of its own (close it when done).

    A wait reads its answer as it comes, beside a pair of sockets that cut()
    writes to. That wakes it on every system: closing or shutting a socket
    another thread waits on does not, on Windows. Its connection is kept
    alive (KEEPALIVE_IDLE), for one the network went from under to fail soon."""

    def __init__(self, path: str, shared: _Shared, place: Place, timeout: float) -> None:
        self._path = path
        self._shared = shared
        self._place = place
        self._timeout = timeout
        self._api = _Host(place.api, place.port(0), shared.context, timeout)
        self._lock = threading.Lock()
        self._cut = False
        self._waker: socket.socket | None = None  # cut() writes to it
        self._woken: socket.socket | None = None  # and a wait sees it here

    def __repr__(self) -> str:
        return "DropboxFeed()"

    def close(self) -> None:
        self._api.close()
        with self._lock:
            for each in (self._waker, self._woken):
                if each is not None:
                    each.close()
            self._waker = self._woken = None

    def latest(self) -> str:
        args: dict[str, object] = {"path": self._path, "recursive": False, "include_deleted": True}
        answer = _rpc(self._shared, self._api, "/2/files/list_folder/get_latest_cursor", args)
        if answer.status != 200:
            raise _error(answer)
        cursor = _text(_object(answer.data), "cursor")
        if cursor is None:
            raise UnavailableError(answered=True)
        return cursor

    def wait(self, cursor: str) -> Waited:
        """Sent no token: only the cursor, to the notify host."""
        woken = self._pair()
        if self._cut_taken():
            raise UnavailableError()
        body = json.dumps({"cursor": cursor, "timeout": WAIT}).encode()
        connection = http.client.HTTPSConnection(
            self._place.notify,
            self._place.port(2),
            timeout=self._timeout,
            context=self._shared.context(),
        )
        try:
            connection.request(
                "POST",
                "/2/files/list_folder/longpoll",
                body=body,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
            )
            assert isinstance(connection.sock, ssl.SSLSocket)
            _keep_alive(connection.sock)
            answer = self._answer(connection.sock, woken)
        except OSError, http.client.HTTPException:  # ssl's errors among them
            raise UnavailableError() from None
        finally:
            connection.close()
        if answer.status != 200:
            raise _error(answer)  # a cursor too old: CursorExpiredError
        said = _object(answer.data)
        changes, backoff = said.get("changes"), said.get("backoff")
        if not isinstance(changes, bool):
            raise UnavailableError(answered=True)
        if isinstance(backoff, bool) or not isinstance(backoff, int | float) or backoff < 0:
            backoff = None
        return Waited(changes, None if backoff is None else float(backoff))

    def since(self, cursor: str) -> tuple[bool, str]:
        """A file this target wrote, at the rev it wrote it, is its own (one it is
        writing, once the answer is in); a file another wrote, one deleted, or a
        folder, is not."""
        others = False
        while True:
            answer = _rpc(
                self._shared, self._api, "/2/files/list_folder/continue", {"cursor": cursor}
            )
            if answer.status != 200:
                raise _error(answer)  # a cursor too old: CursorExpiredError
            page = _object(answer.data)
            entries, next_cursor = page.get("entries"), _text(page, "cursor")
            if not isinstance(entries, list) or next_cursor is None:
                raise UnavailableError(answered=True)
            if not all(self._mine(each) for each in cast(list[object], entries)):
                others = True
            cursor = next_cursor
            if page.get("has_more") is not True:
                return others, cursor

    def cut(self) -> None:
        with self._lock:
            self._cut = True
            if self._waker is not None:
                with contextlib.suppress(OSError):
                    self._waker.send(b"x")

    def _pair(self) -> socket.socket:
        with self._lock:
            if self._woken is None:
                self._waker, self._woken = socket.socketpair()
                self._woken.setblocking(False)
            return self._woken

    def _cut_taken(self) -> bool:
        """Whether cut() was called since the last wait it ended: then this
        one ends, and what it wrote is read off."""
        with self._lock:
            if not self._cut:
                return False
            self._cut = False
            while self._woken is not None:
                try:
                    if not self._woken.recv(64):
                        break
                except OSError:  # BlockingIOError among them: nothing more
                    break
            return True

    def _answer(self, sock: ssl.SSLSocket, woken: socket.socket) -> _Answer:
        """The answer, read as it comes until it is all there; or the wait cut."""
        sock.setblocking(False)
        read = bytearray()
        until = time.monotonic() + WAIT_TIMEOUT
        while True:
            try:
                data = sock.recv(_MOST)
            except ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError:
                left = until - time.monotonic()
                if left <= 0:
                    raise UnavailableError() from None
                ready, _, _ = select.select([sock, woken], [], [], left)
                if woken in ready and self._cut_taken():
                    raise UnavailableError() from None
                continue
            except ssl.SSLZeroReturnError, ssl.SSLEOFError:
                data = b""
            read += data
            if len(read) > _MOST:
                raise UnavailableError(answered=True)
            answer = _answered(bytes(read), closed=not data)
            if answer is not None:
                return answer

    def _mine(self, listed: object) -> bool:
        if not isinstance(listed, dict):
            return False
        item = cast(dict[str, object], listed)
        if item.get(".tag") != "file":
            return False
        path_lower, rev = _text(item, "path_lower"), _rev(item)
        return (
            path_lower is not None
            and rev is not None
            and self._shared.mine(path_lower, rev, self._timeout)
        )


@dataclass(frozen=True)
class App:
    """Stickle as registered with Dropbox, and where Dropbox is: what a sign-in
    and a target both take."""

    app_key: str = field(repr=False)
    place: Place = DROPBOX
    public: Callable[[], ssl.SSLContext] = https_context

    def sign_in(self, pages: Pages, *, timeout: float = TIMEOUT) -> SignIn:
        return sign_in(self.app_key, pages, place=self.place, public=self.public, timeout=timeout)

    def target(
        self,
        refresh_token: str,
        renewed: Callable[[str], None],
        *,
        heard: Callable[[int, int], None] | None = None,
        timeout: float = TIMEOUT,
    ) -> Dropbox:
        return Dropbox(
            self.app_key,
            refresh_token,
            renewed,
            place=self.place,
            public=self.public,
            heard=heard,
            timeout=timeout,
        )


def registered() -> App | None:
    """Stickle as this build has it registered with Dropbox; None: it has not."""
    app_key = dropbox_app_key()
    return None if app_key is None else App(app_key)
