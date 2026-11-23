"""A OneDrive (a personal Microsoft account) to sync with, through Microsoft Graph.

The Stickle folder is the app's own folder in the user's OneDrive (Apps/Stickle,
or as their language names it): the only one the app may reach, and the same
for every device signed in to the same account.

Each of the four things a target does takes these requests:

- list: the folder's files, a page of 200 at a time.
- get: the file's details (its eTag, and an address to download it from), then
  the download. Should the file change in between, the tag is the older one:
  the next write is refused, and the file read again.
- put, a file made: one upload that fails if a file is there already.
- put, a file written over: an upload session, made only if the file's eTag is
  still the one read (if-match), then the file to it, which is refused too if
  the file was written or deleted since the session was made. A file written
  over without a tag is refused unsent: read, it has one.
- delete: only if the eTag is still the one read; without a tag, as it is.

Conditions are kept, and every write leaves the file a new eTag.

The refresh token comes from a sign-in in the browser (sign_in; see
stickle.sync.targets.sign_in), which asks for no more than the app's folder,
staying signed in, and the account's name.

Who is talked to, before anything is sent: only Microsoft's own addresses, over
https, with a certificate a public authority issued for them. Graph alone is
sent the access token, and the sign-in address alone the refresh token
(stickle.sync.targets.oauth). The download and upload addresses Graph gives
need neither: they are sent no token, and nothing at all unless they are of
Microsoft's hosts for them (CONTENT_HOSTS). No redirect is followed.

Nothing here logs, and no error says what a token, a path or an address is.
"""

import http.client
import json
import ssl
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import SplitResult, quote, urlsplit

from stickle.cloud_apps import onedrive_client_id
from stickle.core.cloud_time import date_millis
from stickle.platform.tls import https_context
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Changes,
    Condition,
    Entry,
    FullError,
    NoAccessError,
    NotThereError,
    Read,
    RefusedError,
    TargetError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.oauth import TokenAddress, Tokens, waiting
from stickle.sync.targets.sign_in import Pages, SignIn

TIMEOUT = 30.0  # seconds, for each step of a request
PAGE = 200  # files on a page of a listing
# Bytes of a file sent to an upload session at a time: a multiple of 320 KiB,
# as Graph asks, and well under its 60 MiB.
CHUNK = 32 * 320 * 1024
SCOPE = "Files.ReadWrite.AppFolder offline_access"
# At sign-in, also the account's name (in the ID token), for the user to know
# which account the device syncs with.
SIGN_IN_SCOPE = f"openid profile {SCOPE}"

# Where Graph's download and upload addresses are: any host under one starting
# with ".", or that one host.
CONTENT_HOSTS = (".1drv.com", ".microsoftpersonalcontent.com", "api.onedrive.com")

_USER_AGENT = "Stickle"
_GRAPH = "/v1.0/"
_APPROOT = f"{_GRAPH}me/drive/special/approot"
_LISTED = f"$select=name,size,eTag,file&$top={PAGE}"
_MADE_ONLY = "@microsoft.graph.conflictBehavior=fail"
_SESSION = json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode()
_WRITTEN = (200, 201)
_MORE_WANTED = 202  # a part of a file sent to an upload session, the rest to come
_DELETED = (200, 204)

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
    """Microsoft's addresses: as they are, or a test's."""

    graph: str = "graph.microsoft.com"
    login: str = "login.microsoftonline.com"
    tenant: str = "consumers"  # personal accounts
    content: tuple[str, ...] = CONTENT_HOSTS
    port: int | None = None  # tests only: another than https's own, for all of them

    @property
    def tokens(self) -> TokenAddress:
        return TokenAddress(self.login, f"/{self.tenant}/oauth2/v2.0/token", self.port)

    @property
    def authorize(self) -> str:
        port = "" if self.port is None else f":{self.port}"
        return f"https://{self.login}{port}/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def hosts(self) -> list[str]:
        """Where a target and a sign-in connect, as told to the user (* for any name under)."""
        content = [f"*{host}" if host.startswith(".") else host for host in self.content]
        return [self.graph, self.login, *content]


MICROSOFT = Place()


def sign_in(
    client_id: str,
    pages: Pages,
    *,
    place: Place = MICROSOFT,
    public: Callable[[], ssl.SSLContext] = https_context,
    timeout: float = TIMEOUT,
) -> SignIn:
    """A sign-in to a personal Microsoft account. The user picks the account
    each time, even when the browser is signed in to one already: else it
    would be taken without asking, and maybe not the one they meant."""
    return SignIn(
        place.authorize,
        place.tokens,
        client_id,
        SIGN_IN_SCOPE,
        pages,
        extra={"response_mode": "query", "prompt": "select_account"},
        redirect_host="localhost",  # as the app is registered: Microsoft takes any port
        public=public,
        timeout=timeout,
    )


@dataclass(frozen=True)
class _Answer:
    status: int
    data: bytes
    wait: float | None  # as Retry-After asks


def _error(answer: _Answer) -> TargetError:
    match answer.status:
        case 401:
            return TokenExpiredError()
        case 403:
            return NoAccessError()
        case 404 | 410:
            return NotThereError()
        case 409 | 412:
            return RefusedError()
        case 429 | 509:
            return TooBusyError(answer.wait)
        case 503 if answer.wait is not None:
            return TooBusyError(answer.wait)
        case 507:
            return FullError()
        case _:
            return UnavailableError(answered=True)


def _object(data: bytes) -> dict[str, object]:
    """A JSON object Graph answered."""
    try:
        answer = json.loads(data)
    except ValueError:
        raise UnavailableError(answered=True) from None
    if not isinstance(answer, dict):
        raise UnavailableError(answered=True)
    return cast(dict[str, object], answer)


def _text(answer: dict[str, object], name: str) -> str | None:
    value = answer.get(name)
    return value if isinstance(value, str) and value else None


def _tag(answer: dict[str, object]) -> str | None:
    tag = _text(answer, "eTag")
    return tag if tag is not None and tag.isascii() and tag.isprintable() else None


def _parts(path: str) -> list[str]:
    parts = path.split("/") if path else []
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise ValueError("not a path in the Stickle folder")
    return parts


def _hosted(host: str, hosts: tuple[str, ...]) -> bool:
    name = host.lower()
    return any(
        (name.endswith(each) and len(name) > len(each)) if each.startswith(".") else name == each
        for each in hosts
    )


class OneDrive:
    """A target for one signed-in account, used from one thread at a time. One
    connection to Graph is kept between requests (close it when done)."""

    def __init__(
        self,
        client_id: str,
        refresh_token: str,
        renewed: Callable[[str], None],
        *,
        place: Place = MICROSOFT,
        folder: str = "",
        public: Callable[[], ssl.SSLContext] = https_context,
        heard: Callable[[int, int], None] | None = None,
        timeout: float = TIMEOUT,
        chunk: int = CHUNK,
    ) -> None:
        """renewed: told a new refresh token as soon as Microsoft gives one, to
        be kept in place of the last. folder: in the app's folder, for tests
        only. public: what checks a public authority's certificate. heard:
        told a response's Date, as milliseconds, and how many milliseconds it
        took. timeout: seconds, for each step of a request."""
        self._place = place
        self._folder = "/".join(_parts(folder))
        self._public = public
        self._context: ssl.SSLContext | None = None
        self._tokens = Tokens(
            place.tokens,
            client_id,
            refresh_token,
            renewed,
            scope=SCOPE,
            public=self._https,
            timeout=timeout,
        )
        self._heard = heard
        self._timeout = timeout
        self._chunk = chunk
        self._kept: http.client.HTTPSConnection | None = None

    @property
    def keeps_conditions(self) -> bool:
        return True

    @property
    def unique_tags(self) -> bool:
        return True

    def close(self) -> None:
        if self._kept is not None:
            self._kept.close()
            self._kept = None

    # The four things a target does

    def list(self, folder: str) -> Generator[Entry]:
        url: str | None = f"{self._item(folder, '/children')}?{_LISTED}"
        while url is not None:
            answer = self._graph("GET", url)
            if answer.status in (404, 410):
                return  # none made yet
            if answer.status != 200:
                raise _error(answer)
            page = _object(answer.data)
            files = page.get("value")
            if not isinstance(files, list):
                raise UnavailableError(answered=True)
            entries = [self._entry(folder, each) for each in cast(list[object], files)]
            url = self._next(_text(page, "@odata.nextLink"))
            yield from (entry for entry in entries if entry is not None)

    def get(self, path: str) -> Read:
        answer = self._graph("GET", self._item(path))
        if answer.status != 200:
            raise _error(answer)
        item = _object(answer.data)
        if not isinstance(item.get("file"), dict):
            raise NotThereError()  # a folder of that name
        where = _text(item, "@microsoft.graph.downloadUrl")
        if where is None:
            raise UnavailableError(answered=True)
        download = self._content("GET", where)
        if download.status != 200:
            raise _error(download)
        return Read(download.data, _tag(item))

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        if not data:
            raise ValueError("nothing to write")
        match condition:
            case Absent():
                url = f"{self._item(path, '/content')}?{_MADE_ONLY}"
                answer = self._graph("PUT", url, data, {"Content-Type": "application/octet-stream"})
                if answer.status not in _WRITTEN:
                    raise _error(answer)
                return _tag(_object(answer.data))
            case AsRead(tag=str() as tag):
                return self._written_over(path, data, tag)
            case AsRead():
                raise RefusedError()

    def delete(self, path: str, condition: AsRead) -> None:
        if not path:
            raise ValueError("the Stickle folder itself")
        headers = {} if condition.tag is None else {"If-Match": condition.tag}
        answer = self._graph("DELETE", self._item(path), None, headers)
        if answer.status not in _DELETED:
            raise _error(answer)

    def changes(self, cursor: str | None) -> Iterator[Changes] | None:
        return None  # as WebDAV: what changed is told in devices/

    def wait_for_change(self, cursor: str, seconds: float) -> bool | None:
        return None

    # Below them

    def _item(self, path: str, then: str = "") -> str:
        parts = [*_parts(self._folder), *_parts(path)]
        if not parts:
            return f"{_APPROOT}{then}"
        inside = "/".join(quote(part, safe="") for part in parts)
        return f"{_APPROOT}:/{inside}:{then}" if then else f"{_APPROOT}:/{inside}"

    def _entry(self, folder: str, listed: object) -> Entry | None:
        """A file as listed; None for a folder, or a name that could not be a file's."""
        if not isinstance(listed, dict):
            return None
        item = cast(dict[str, object], listed)
        name = _text(item, "name")
        if name is None or name in (".", "..") or "/" in name or "\\" in name:
            return None
        if not isinstance(item.get("file"), dict):
            return None
        size = item.get("size")
        bytes_ = size if isinstance(size, int) and not isinstance(size, bool) else 0
        return Entry(f"{folder}/{name}" if folder else name, _tag(item), bytes_)

    def _next(self, link: str | None) -> str | None:
        """The next page's place on Graph, if the link is to Graph: there, as
        Graph may give it, by the folder's id rather than its path."""
        if link is None:
            return None
        parts = urlsplit(link)
        if (
            parts.scheme != "https"
            or parts.hostname != self._place.graph
            or not self._port_is(parts)
            or not parts.path.startswith(_GRAPH)
        ):
            raise UnavailableError(answered=True)
        return f"{parts.path}?{parts.query}" if parts.query else parts.path

    def _port_is(self, parts: SplitResult) -> bool:
        try:
            port = parts.port
        except ValueError:
            return False
        wanted = self._place.port
        return port == wanted or (wanted is None and port == 443)

    def _written_over(self, path: str, data: bytes, tag: str) -> str | None:
        made = self._graph(
            "POST",
            self._item(path, "/createUploadSession"),
            _SESSION,
            {"Content-Type": "application/json", "If-Match": tag},
        )
        if made.status == 404:
            raise RefusedError()  # gone since it was read
        if made.status != 200:
            raise _error(made)
        where = _text(_object(made.data), "uploadUrl")
        if where is None:
            raise UnavailableError(answered=True)
        size = len(data)
        for start in range(0, size, self._chunk):
            end = min(start + self._chunk, size)
            sent = self._content(
                "PUT",
                where,
                data[start:end],
                {"Content-Range": f"bytes {start}-{end - 1}/{size}"},
            )
            if end < size and sent.status == _MORE_WANTED:
                continue
            if end == size and sent.status in _WRITTEN:
                return _tag(_object(sent.data))
            if sent.status == 404:
                raise RefusedError()  # written or deleted since the session was made
            raise _error(sent)
        raise AssertionError("an upload with nothing in it")

    def _https(self) -> ssl.SSLContext:
        if self._context is None:
            self._context = self._public()
        return self._context

    def _content(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Answer:
        """A request to a download or upload address Graph gave: on a connection
        of its own, with no token, and only to Microsoft's hosts for them."""
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https"
            or host is None
            or parts.username is not None
            or not _hosted(host, self._place.content)
            or not self._port_is(parts)
        ):
            raise UnavailableError(answered=True)
        place = f"{parts.path or '/'}?{parts.query}" if parts.query else parts.path or "/"
        connection = http.client.HTTPSConnection(
            host, self._place.port, timeout=self._timeout, context=self._https()
        )
        try:
            connection.request(
                method, place, body=body, headers={"User-Agent": _USER_AGENT, **(headers or {})}
            )
            response = connection.getresponse()
            return self._answer(response)
        except OSError, http.client.HTTPException:
            raise UnavailableError() from None
        finally:
            connection.close()

    def _graph(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Answer:
        """A request to Graph with the access token; once more with another if
        Graph turns that one down."""
        answer = self._ask(method, url, body, headers or {}, self._tokens.access())
        if answer.status == 401:
            answer = self._ask(method, url, body, headers or {}, self._tokens.renew())
        return answer

    def _ask(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str], token: str
    ) -> _Answer:
        """The request made and its answer read. A connection kept from before
        may have been closed by Graph meanwhile: then once more on a new one. If
        the request did get through, its condition or the merge that follows
        deals with it, as with any answer lost."""
        sent = {
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            **headers,
        }
        connection, may_be_closed = self._kept, self._kept is not None
        self._kept = None
        while True:
            try:
                if connection is None:
                    connection = http.client.HTTPSConnection(
                        self._place.graph,
                        self._place.port,
                        timeout=self._timeout,
                        context=self._https(),
                    )
                began = time.monotonic()
                connection.request(method, url, body=body, headers=sent)
                response = connection.getresponse()
                answer = self._answer(response)
            except _CLOSED_MEANWHILE:
                if connection is not None:
                    connection.close()
                connection = None
                if not may_be_closed:
                    raise UnavailableError() from None
                may_be_closed = False
            except OSError, http.client.HTTPException:
                if connection is not None:
                    connection.close()
                raise UnavailableError() from None
            else:
                self._tell_time(response, began)
                if response.will_close:
                    connection.close()
                else:
                    self._kept = connection
                return answer

    @staticmethod
    def _answer(response: http.client.HTTPResponse) -> _Answer:
        data = response.read()
        return _Answer(
            response.status,
            data,
            waiting(response.getheader("Retry-After"), response.getheader("Date")),
        )

    def _tell_time(self, response: http.client.HTTPResponse, began: float) -> None:
        if self._heard is None:
            return
        date = date_millis(response.getheader("Date"))
        if date is not None:
            self._heard(date, round((time.monotonic() - began) * 1000))


@dataclass(frozen=True)
class App:
    """Stickle as registered with Microsoft, and where Microsoft is: what a
    sign-in and a target both take."""

    client_id: str = field(repr=False)
    place: Place = MICROSOFT
    public: Callable[[], ssl.SSLContext] = https_context

    def sign_in(self, pages: Pages, *, timeout: float = TIMEOUT) -> SignIn:
        return sign_in(self.client_id, pages, place=self.place, public=self.public, timeout=timeout)

    def target(
        self,
        refresh_token: str,
        renewed: Callable[[str], None],
        *,
        heard: Callable[[int, int], None] | None = None,
        timeout: float = TIMEOUT,
    ) -> OneDrive:
        return OneDrive(
            self.client_id,
            refresh_token,
            renewed,
            place=self.place,
            public=self.public,
            heard=heard,
            timeout=timeout,
        )


def registered() -> App | None:
    """Stickle as this build has it registered with Microsoft; None: it has not."""
    client_id = onedrive_client_id()
    return None if client_id is None else App(client_id)
