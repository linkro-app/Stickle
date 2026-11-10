"""A WebDAV server to sync with (stickle.sync.targets.base).

The Stickle folder is right under the address the user gave. Its folders are
made before the first write into them, never after: a write into a folder that
is not there leaves some servers (a Synology NAS, for one) failing about half
of the other writes for minutes.

A server may keep a condition or not, and telling which would take leaving a
file of ours on it to try. So conditions are always sent and never counted on:
a file is read before it is written (keeps_conditions is False), and two
writes may leave one tag (unique_tags is False).

Who is talked to, before anything is sent:

- Over https, a server whose certificate a public authority issued for its
  address, or the one certificate the user looked at and chose to trust. If
  that one is replaced by another a public authority issued, it is no longer
  needed. Any other certificate stops here (CertificateError) and nothing is
  sent, the password least of all.
- Over http, an address at home only: on the way there, the password can be read.

A wrong password is not tried again, here or by anyone above: some servers
shut a whole household out after a few.

Nothing here logs, and no error says what the address or the password is.
"""

import base64
import hashlib
import http.client
import ipaddress
import ssl
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError, iterparse

from stickle.core.cloud_time import date_millis
from stickle.platform.tls import https_context
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    CertificateError,
    Changes,
    Condition,
    Entry,
    FullError,
    NoAccessError,
    NotThereError,
    Read,
    RefusedError,
    SignInNeededError,
    TargetError,
    TooBusyError,
    UnavailableError,
)

FOLDER = "Stickle"  # right under the address the user gave
TIMEOUT = 30.0  # seconds, for each step of a request
HTTPS_PORT = 443

_USER_AGENT = "Stickle"
_DAV = "{DAV:}"
_RESPONSE = f"{_DAV}response"
_HREF = f"{_DAV}href"
_ETAG = f".//{_DAV}getetag"
_LENGTH = f".//{_DAV}getcontentlength"
_COLLECTION = f".//{_DAV}collection"
_LISTED = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b"<resourcetype/><getcontentlength/><getetag/>"
    b"</prop></propfind>"
)
_LIST_HEADERS = {"Depth": "1", "Content-Type": 'application/xml; charset="utf-8"'}
_ITSELF_HEADERS = {**_LIST_HEADERS, "Depth": "0"}
_MULTI_STATUS = 207
_NOT_IMPLEMENTED = 501  # as a web server that is no WebDAV server answers PROPFIND
_WRITTEN = (200, 201, 204)
_DELETED = (200, 202, 204)
_NO_FOLDER = (404, 409)  # as a server answers a write into a folder that is not there
_FOLDER_THERE = (200, 201, 204, 301, 405)  # 405: making a folder that is there
_PARENT_MISSING = 409

# OpenSSL's X509_V_FLAG_NO_CHECK_TIME. Only for the one certificate the user
# trusted: the same certificate past its date proves as much as before, and
# trusting it again could not help.
_ANY_TIME = 0x200000

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


class NoWebDavFolderError(TargetError):
    """Nothing at that address answers as a WebDAV folder."""


class AddressError(ValueError):
    """Not an address to connect to."""


class OutsideHomeError(AddressError):
    """http to an address that is not at home: the password could be read on the way."""


@dataclass(frozen=True)
class Address:
    secure: bool
    host: str
    port: int | None
    path: str  # percent-encoded, starting with "/" and not ending with it; "" at the top

    @property
    def text(self) -> str:
        """As it is kept, and parse_address reads it back."""
        scheme = "https" if self.secure else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port is None else f":{self.port}"
        return f"{scheme}://{host}{port}{self.path}"


def is_home(host: str) -> bool:
    """Whether host is plainly on the user's own network: a private address, or
    a name ending in .local. A name that only resolves to one proves nothing."""
    name = host.rstrip(".").lower()
    if name.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(name.partition("%")[0]).is_private
    except ValueError:
        return False


def parse_address(text: str) -> Address:
    """The address the user typed: https unless it says http. AddressError if it
    is not one to connect to, OutsideHomeError if it is http away from home."""
    typed = text.strip()
    if "://" not in typed:
        typed = f"https://{typed}"
    try:
        parts = urlsplit(typed)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise AddressError("not an address") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise AddressError("neither https nor http")
    if not host:
        raise AddressError("no server named")
    if parts.username is not None or parts.password is not None:
        raise AddressError("a sign-in in the address")
    if parts.query or parts.fragment:
        raise AddressError("more than a place")
    if scheme == "http" and not is_home(host):
        raise OutsideHomeError("http away from home")
    folders = [unquote(part) for part in parts.path.split("/") if part]
    if any(part in (".", "..") or "/" in part for part in folders):
        raise AddressError("a path that goes elsewhere")
    path = "".join(f"/{quote(part, safe='')}" for part in folders)
    return Address(scheme == "https", host, port, path)


def fingerprint(certificate: bytes) -> str:
    """A certificate's (DER) SHA-256, in hex: what the user compares with the server's."""
    return hashlib.sha256(certificate).hexdigest()


def certificate_of(address: Address) -> bytes:
    """The certificate (DER) the server shows. Only looked at, to show the user:
    nothing is sent over this connection. UnavailableError if none can be had."""
    try:
        shown = ssl.get_server_certificate(
            (address.host, address.port or HTTPS_PORT), timeout=TIMEOUT
        )
    except OSError:
        raise UnavailableError() from None
    return ssl.PEM_cert_to_DER_cert(shown)


class _OnlyThisCertificate(http.client.HTTPSConnection):
    """A connection that goes no further than the handshake unless the server
    shows this certificate, however it connects or connects again."""

    def __init__(self, address: Address, certificate: bytes, timeout: float = TIMEOUT) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False  # whatever name it has: it is this very certificate
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN | _ANY_TIME
        context.load_verify_locations(cadata=certificate)
        super().__init__(address.host, address.port, timeout=timeout, context=context)
        self._certificate = certificate

    def connect(self) -> None:
        super().connect()
        if self.sock.getpeercert(binary_form=True) != self._certificate:
            self.close()
            raise ssl.SSLCertVerificationError("another certificate")


def _tag(value: str | None) -> str | None:
    """A tag as sent back to the server: in quotes, as some servers leave out of a listing."""
    tag = None if value is None else value.strip()
    if not tag or not tag.isascii() or not tag.isprintable():
        return None
    return tag if tag.startswith(('"', "W/")) else f'"{tag}"'


def _waiting(asked: str | None, date: str | None) -> float | None:
    """Seconds a Retry-After asks: a number of them, or a date."""
    if asked is None:
        return None
    try:
        return max(float(int(asked)), 0.0)
    except ValueError:
        pass
    until, now = date_millis(asked), date_millis(date)
    return None if until is None or now is None else max((until - now) / 1000, 0.0)


@dataclass(frozen=True)
class _Answer:
    status: int
    data: bytes
    tag: str | None
    wait: float | None  # as Retry-After asks


def _answer(response: http.client.HTTPResponse, data: bytes) -> _Answer:
    return _Answer(
        response.status,
        data,
        _tag(response.getheader("ETag")),
        _waiting(response.getheader("Retry-After"), response.getheader("Date")),
    )


def _error(answer: _Answer) -> TargetError:
    match answer.status:
        case 401:
            return SignInNeededError()
        case 403:
            return NoAccessError()
        case 404 | 410:
            return NotThereError()
        case 409 | 412:
            return RefusedError()
        case 429:
            return TooBusyError(answer.wait)
        case 503 if answer.wait is not None:
            return TooBusyError(answer.wait)
        case 507:
            return FullError()
        case _:
            return UnavailableError(answered=True)


def _entry(folder: str, response: Element) -> Entry | None:
    """A file as listed; None for a folder, or a name that could not be a file's."""
    href = response.findtext(_HREF)
    if href is None or response.find(_COLLECTION) is not None:
        return None
    # Only the last part: a server behind a proxy may start its paths elsewhere.
    name = unquote(urlsplit(href.strip()).path.rstrip("/").rpartition("/")[2])
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        return None
    try:
        size = int(response.findtext(_LENGTH) or 0)
    except ValueError:
        size = 0
    return Entry(f"{folder}/{name}" if folder else name, _tag(response.findtext(_ETAG)), size)


def _entries(response: http.client.HTTPResponse, folder: str) -> Iterator[Entry]:
    """The files of a listing as it arrives: what is read of it is let go of at
    once, a listing being much larger as a tree than as text."""
    root: Element | None = None
    for event, element in iterparse(response, events=("start", "end")):
        if root is None:
            root = element
        elif event == "end" and element.tag == _RESPONSE:
            entry = _entry(folder, element)
            root.clear()
            if entry is not None:
                yield entry


class WebDav:
    """A target for one server, used from one thread at a time. One connection
    is kept between requests (close it when done)."""

    def __init__(
        self,
        address: Address,
        user: str,
        password: str,
        certificate: bytes | None = None,
        *,
        public: Callable[[], ssl.SSLContext] = https_context,
        heard: Callable[[int, int], None] | None = None,
        no_longer_trusted: Callable[[], None] | None = None,
        keeps_conditions: bool = False,
        timeout: float = TIMEOUT,
    ) -> None:
        """certificate: the one the user chose to trust (DER). public: what checks
        a public authority's. heard: told a response's Date, as milliseconds,
        and how many milliseconds it took. no_longer_trusted: told when a public
        authority's certificate has taken the place of the one trusted.
        keeps_conditions: only where this server is known to. timeout: seconds,
        for each step of a request."""
        self._address = address
        self._root = f"{address.path}/{FOLDER}"
        sign_in = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._sign_in = f"Basic {sign_in}"
        self._certificate = certificate
        self._public = public
        self._context: ssl.SSLContext | None = None
        self._heard = heard
        self._no_longer_trusted = no_longer_trusted
        self._keeps_conditions = keeps_conditions
        self._timeout = timeout
        self._kept: http.client.HTTPConnection | None = None
        self._folders: set[str] = set()  # known to be there

    @property
    def keeps_conditions(self) -> bool:
        return self._keeps_conditions

    @property
    def unique_tags(self) -> bool:
        return False

    def close(self) -> None:
        if self._kept is not None:
            self._kept.close()
            self._kept = None

    def check(self) -> None:
        """Whether the address is a WebDAV folder this user may look at, before
        anything is made there. NoWebDavFolderError if it answers otherwise; a
        wrong password, a certificate not trusted and no answer come up as
        they do for any request."""
        answer = self._ask("PROPFIND", f"{self._address.path}/", _LISTED, _ITSELF_HEADERS)
        if answer.status == _MULTI_STATUS:
            return
        if answer.status in (401, 403, 429) or (500 <= answer.status != _NOT_IMPLEMENTED):
            raise _error(answer)
        raise NoWebDavFolderError()

    # The four things a target does

    def list(self, folder: str) -> Generator[Entry]:
        connection, response = self._start(
            "PROPFIND", f"{self._url(folder)}/", _LISTED, _LIST_HEADERS
        )
        if response.status != _MULTI_STATUS:
            answer = self._finish(connection, response)
            if answer.status in (404, 410):
                return  # none made yet
            raise _error(answer)
        whole = False
        try:
            yield from _entries(response, folder)
            whole = response.isclosed()
        except OSError, ParseError, http.client.HTTPException:
            raise UnavailableError() from None
        finally:
            if whole:
                self._keep(connection)
            else:
                connection.close()

    def get(self, path: str) -> Read:
        answer = self._ask("GET", self._url(path))
        if answer.status != 200:
            raise _error(answer)
        return Read(answer.data, answer.tag)

    def put(self, path: str, data: bytes, condition: Condition) -> str | None:
        url = self._url(path)
        headers = self._condition(condition)
        self._make_folders(path)
        answer = self._ask("PUT", url, data, headers)
        if answer.status in _NO_FOLDER:  # removed since they were made
            self._folders.clear()
            self._make_folders(path)
            answer = self._ask("PUT", url, data, headers)
        if answer.status not in _WRITTEN:
            raise _error(answer)
        return answer.tag

    def delete(self, path: str, condition: AsRead) -> None:
        answer = self._ask("DELETE", self._url(path), None, self._condition(condition))
        if answer.status not in _DELETED:
            raise _error(answer)

    def changes(self, cursor: str | None) -> Iterator[Changes] | None:
        return None

    def wait_for_change(self, cursor: str, seconds: float) -> bool | None:
        return None

    # Below them

    def _url(self, path: str) -> str:
        if not path:
            return self._root
        parts = "/".join(quote(part, safe="") for part in path.split("/"))
        return f"{self._root}/{parts}"

    @staticmethod
    def _condition(condition: Condition) -> dict[str, str]:
        match condition:
            case Absent():
                return {"If-None-Match": "*"}
            case AsRead(tag=tag):
                sent = _tag(tag)
                return {} if sent is None else {"If-Match": sent}

    def _make_folders(self, path: str) -> None:
        """The Stickle folder, and path's folder in it, once each. The address
        itself is the user's: if that is not there, nothing is made of it."""
        folder = path.rpartition("/")[0]
        for each in ("", folder) if folder else ("",):
            if each in self._folders:
                continue
            answer = self._ask("MKCOL", f"{self._url(each)}/")
            if answer.status == _PARENT_MISSING:
                raise UnavailableError(answered=True)
            if answer.status not in _FOLDER_THERE:
                raise _error(answer)
            self._folders.add(each)

    def _ask(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Answer:
        connection, response = self._start(method, url, body, headers or {})
        return self._finish(connection, response)

    def _finish(
        self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse
    ) -> _Answer:
        try:
            data = response.read()
        except OSError, http.client.HTTPException:
            connection.close()
            raise UnavailableError() from None
        self._keep(connection)
        return _answer(response, data)

    def _keep(self, connection: http.client.HTTPConnection) -> None:
        """Kept for the next request, unless the server is closing it or one is
        kept already (a listing being read while others are made)."""
        if self._kept is None and connection.sock is not None:
            self._kept = connection
        else:
            connection.close()

    def _start(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """The request made, its response still to read. A connection kept from
        before may have been closed by the server meanwhile: then once more on a
        new one. If the request did get through, its condition or the merge
        that follows deals with it, as with any answer lost."""
        sent = {"Authorization": self._sign_in, "User-Agent": _USER_AGENT, **headers}
        connection, may_be_closed = self._kept, self._kept is not None
        self._kept = None
        while True:
            try:
                if connection is None:
                    connection = self._open()
                began = time.monotonic()
                connection.request(method, url, body=body, headers=sent)
                response = connection.getresponse()
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
                return connection, response

    def _tell_time(self, response: http.client.HTTPResponse, began: float) -> None:
        if self._heard is None:
            return
        date = date_millis(response.getheader("Date"))
        if date is not None:
            self._heard(date, round((time.monotonic() - began) * 1000))

    def _open(self) -> http.client.HTTPConnection:
        """A connection to the server, if it is the one to talk to. Nothing has
        been sent over it yet."""
        address = self._address
        if not address.secure:
            return http.client.HTTPConnection(address.host, address.port, timeout=self._timeout)
        if self._certificate is not None:
            trusted = _OnlyThisCertificate(address, self._certificate, self._timeout)
            try:
                trusted.connect()
            except ssl.SSLCertVerificationError:
                trusted.close()
            else:
                return trusted
        if self._context is None:
            self._context = self._public()
        public = http.client.HTTPSConnection(
            address.host, address.port, timeout=self._timeout, context=self._context
        )
        try:
            public.connect()
        except ssl.SSLCertVerificationError:
            public.close()
            raise CertificateError(certificate_of(address)) from None
        if self._certificate is not None:
            self._certificate = None
            if self._no_longer_trusted is not None:
                self._no_longer_trusted()
        return public
