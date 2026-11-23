"""Microsoft Graph and its sign-in address in the test, over TLS with a test
certificate authority (trustme), for the OneDrive target to talk real HTTPS to.

It answers as Microsoft's documentation says they do, for what the target asks:
the app's folder (its files, a page at a time), a file's details with an
address to download it from, an upload that fails if a file is there, an
upload session made only if the file's eTag is as given (and its upload
refused if the file is written or deleted meanwhile), a delete likewise,
and access tokens from a refresh token (a new refresh token each time, the one
before still good). Every write gives the file a new eTag.

It also plays the sign-in page, as if the user signed in and allowed the app
at once (or said no): it sends the browser back with a code, which the token
address takes once, from the app it was given to, with the PKCE verifier and
the same redirect address. The test's own code plays the browser.

The test says what goes wrong (an answer for the next requests, tokens turned
down, the app turned away, no room left) and looks at every request that
reached it. One server plays all of Microsoft's hosts, told apart by path.
"""

import base64
import hashlib
import http.client
import json
import secrets
import ssl
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import trustme

from stickle.sync.targets.onedrive import Place

HOST = "127.0.0.1"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"  # a test's, not the app's
FIRST_REFRESH = "refresh-token-first-of-a-test"
TOKEN_PATH = "/consumers/oauth2/v2.0/token"
AUTHORIZE_PATH = "/consumers/oauth2/v2.0/authorize"
ACCOUNT = "someone@example.com"  # the account signed in to, as its ID token names it
APPROOT = "/v1.0/me/drive/special/approot"
LASTS = 3600  # seconds an access token is good for
DRIVE = "D0C0FFEE12345678"


@dataclass(frozen=True)
class Asked:
    """A request that reached the server."""

    method: str
    path: str  # with its query
    host: str | None  # its Host header
    authorization: str | None
    body: bytes


@dataclass(frozen=True)
class Answer:
    """What the next request to Graph is answered, whatever it asks."""

    status: int
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: dict[str, object] = field(default_factory=dict[str, object])


@dataclass
class _File:
    data: bytes
    item_id: str
    version: int

    @property
    def tag(self) -> str:
        return f'"{{{self.item_id}}},{self.version}"'


@dataclass
class _Session:
    path: str
    data: bytearray
    tag: str | None = None  # an upload's: the file's when it was made; None: none there


@dataclass(frozen=True)
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    scope: str


def _id_token(claims: dict[str, object]) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmVk"


@dataclass
class Graph:
    """The server's state, which the test reads and changes between requests."""

    port: int = 0
    client_context: ssl.SSLContext | None = None
    asked: list[Asked] = field(default_factory=list[Asked])
    files: dict[str, _File] = field(default_factory=dict[str, _File])
    # Odd things listed with the real files, by folder: raw items as Graph gives them.
    also_listed: dict[str, list[dict[str, object]]] = field(
        default_factory=dict[str, list[dict[str, object]]]
    )
    answers: list[Answer] = field(default_factory=list[Answer])  # for the next Graph requests
    # What another device does to a file right after an upload session for it
    # is made: writes this, or deletes it (None).
    meanwhile: dict[str, bytes | None] = field(default_factory=dict[str, bytes | None])
    token_answers: list[Answer] = field(default_factory=list[Answer])  # for the next refreshes
    refresh_tokens: set[str] = field(default_factory=lambda: {FIRST_REFRESH})
    access_tokens: dict[str, float] = field(default_factory=dict[str, float])  # until, monotonic
    issued: list[str] = field(default_factory=list[str])  # every access token it gave
    issued_refresh: list[str] = field(default_factory=list[str])  # every refresh token, in turn
    refusing_refresh: bool = False  # as when the user took the app's access away
    app_turned_away: bool = False  # as when Microsoft stopped the app
    turning_down_access: bool = False  # every access token, however new
    full: bool = False
    content_host: str = HOST  # in the download and upload addresses it gives
    content_port: int | None = None  # in them likewise; None: its own
    next_link_host: str = HOST  # in the links to a listing's next page
    next_link_by_id: bool = False  # in them, the folder by its id, as Graph may give it
    declining: bool = False  # the user says no to the app on the sign-in page
    # What the ID token says of the account; None: no ID token.
    claims: dict[str, object] | None = field(
        default_factory=lambda: {"preferred_username": ACCOUNT, "name": "Someone"}
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    _sessions: dict[str, _Session] = field(default_factory=dict[str, _Session])
    _codes: dict[str, _Code] = field(default_factory=dict[str, _Code])
    _written: int = 0

    @property
    def place(self) -> Place:
        """Where the target finds it, as it finds Microsoft."""
        return Place(graph=HOST, login=HOST, content=(HOST,), port=self.port)

    def context(self) -> ssl.SSLContext:
        """One that trusts the server's certificate, as a public authority's would be."""
        assert self.client_context is not None
        return self.client_context

    def put_by_hand(self, path: str, data: bytes) -> str:
        """A file written as by another device; its tag."""
        with self.lock:
            return self._write(path, data).tag

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access_tokens.clear()

    def graph_requests(self) -> list[Asked]:
        return [each for each in self.asked if each.path.startswith("/v1.0/")]

    # As the handler asks

    def _write(self, path: str, data: bytes) -> _File:
        self._written += 1
        old = self.files.get(path)
        made = _File(
            data,
            old.item_id if old is not None else secrets.token_hex(8).upper(),
            self._written,
        )
        self.files[path] = made
        return made

    def _item(self, path: str, stored: _File) -> dict[str, object]:
        key = secrets.token_urlsafe(12)
        self._sessions[f"d:{key}"] = _Session(path, bytearray())
        return {
            "id": stored.item_id,
            "name": path.rpartition("/")[2],
            "size": len(stored.data),
            "eTag": stored.tag,
            "cTag": f'"c:{{{stored.item_id}}},{stored.version}"',
            "file": {"mimeType": "application/octet-stream"},
            "@microsoft.graph.downloadUrl": (
                f"https://{self.content_host}:{self.content_port or self.port}/download/{key}"
            ),
        }

    def _folders(self) -> set[str]:
        made: set[str] = {""}
        for path in self.files:
            parts = path.split("/")[:-1]
            for end in range(1, len(parts) + 1):
                made.add("/".join(parts[:end]))
        return made


def _folder_id(folder: str) -> str:
    return hashlib.sha256(f"folder:{folder}".encode()).hexdigest()[:16].upper()


def _by_id(graph: Graph, path: str) -> tuple[str, str] | None:
    """A folder's listing asked for by its id."""
    prefix = f"/v1.0/drives/{DRIVE}/items/"
    if not path.startswith(prefix) or not path.endswith("/children"):
        return None
    wanted = path[len(prefix) : -len("/children")]
    for folder in graph._folders():  # pyright: ignore[reportPrivateUsage]
        if _folder_id(folder) == wanted:
            return folder, "/children"
    return None


def _split(path: str) -> tuple[str, str] | None:
    """A Graph path as the item's path in the app's folder and what is asked of it."""
    if not path.startswith(APPROOT):
        return None
    rest = path[len(APPROOT) :]
    if rest in ("", "/children"):
        return "", rest
    if not rest.startswith(":/"):
        return None
    inner = rest[2:]
    item, colon, then = inner.partition(":")
    return unquote(item), then if colon else ""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(
        self, status: int, body: dict[str, object] | bytes, headers: dict[str, str] | None = None
    ) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if not isinstance(body, bytes):
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str) -> None:
        self._reply(status, {"error": {"code": code, "message": code}})

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        graph = cast(_Server, self.server).graph
        with graph.lock:
            graph.asked.append(
                Asked(
                    self.command,
                    self.path,
                    self.headers.get("Host"),
                    self.headers.get("Authorization"),
                    body,
                )
            )
            parts = urlsplit(self.path)
            if parts.path == AUTHORIZE_PATH and self.command == "GET":
                self._authorize(graph, parse_qs(parts.query))
            elif parts.path == TOKEN_PATH and self.command == "POST":
                self._token(graph, body)
            elif parts.path.startswith("/download/") and self.command == "GET":
                self._download(graph, parts.path.removeprefix("/download/"))
            elif parts.path.startswith("/upload/") and self.command == "PUT":
                self._upload(graph, parts.path.removeprefix("/upload/"), body)
            elif parts.path.startswith("/v1.0/"):
                self._graph(graph, parts.path, parse_qs(parts.query), body)
            else:
                self._error(404, "itemNotFound")

    do_GET = do_PUT = do_POST = do_DELETE = _handle  # noqa: N815

    def _token(self, graph: Graph, body: bytes) -> None:
        if graph.token_answers:
            answer = graph.token_answers.pop(0)
            self._reply(answer.status, answer.body, answer.headers)
            return
        form = {name: values[0] for name, values in parse_qs(body.decode("ascii")).items()}
        if graph.app_turned_away or form.get("client_id") != CLIENT_ID:
            self._reply(400, {"error": "unauthorized_client", "error_description": "AADSTS700016"})
            return
        if form.get("grant_type") == "authorization_code":
            self._redeem(graph, form)
            return
        if (
            graph.refusing_refresh
            or form.get("grant_type") != "refresh_token"
            or form.get("refresh_token") not in graph.refresh_tokens
        ):
            self._reply(400, {"error": "invalid_grant", "error_description": "AADSTS70000"})
            return
        self._reply(200, self._issue(graph, form.get("scope", "")))

    @staticmethod
    def _issue(graph: Graph, scope: str) -> dict[str, object]:
        access = f"access-{secrets.token_urlsafe(16)}"
        refresh = f"refresh-{secrets.token_urlsafe(16)}"
        graph.access_tokens[access] = time.monotonic() + LASTS
        graph.issued.append(access)
        graph.refresh_tokens.add(refresh)  # the one before still good
        graph.issued_refresh.append(refresh)
        return {
            "token_type": "Bearer",
            "scope": scope,
            "expires_in": LASTS,
            "access_token": access,
            "refresh_token": refresh,
        }

    def _authorize(self, graph: Graph, query: dict[str, list[str]]) -> None:
        given = {name: values[0] for name, values in query.items()}
        redirect = given.get("redirect_uri", "")
        if not redirect.startswith("http://localhost:"):
            self._reply(400, b"not a redirect address the app has")  # Microsoft's own page
            return
        answer: dict[str, str]
        if graph.app_turned_away or given.get("client_id") != CLIENT_ID:
            answer = {"error": "unauthorized_client", "error_description": "<b>AADSTS700016</b>"}
        elif graph.declining:
            answer = {"error": "access_denied", "error_description": "<b>The user said no</b>"}
        elif (
            given.get("response_type") != "code"
            or given.get("code_challenge_method") != "S256"
            or not given.get("code_challenge")
        ):
            answer = {"error": "invalid_request", "error_description": "AADSTS900144"}
        else:
            code = f"M.C5_{secrets.token_urlsafe(24)}"
            graph._codes[code] = _Code(  # pyright: ignore[reportPrivateUsage]
                given["client_id"], redirect, given["code_challenge"], given.get("scope", "")
            )
            answer = {"code": code}
        if "state" in given:
            answer["state"] = given["state"]
        self._reply(302, b"", {"Location": f"{redirect}?{urlencode(answer)}"})

    def _redeem(self, graph: Graph, form: dict[str, str]) -> None:
        given = graph._codes.pop(form.get("code", ""), None)  # pyright: ignore[reportPrivateUsage]
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if (
            given is None
            or given.client_id != form.get("client_id")
            or given.redirect_uri != form.get("redirect_uri")
            or not verifier
            or given.challenge != challenge
        ):
            self._reply(400, {"error": "invalid_grant", "error_description": "AADSTS70008"})
            return
        tokens = self._issue(graph, given.scope)
        if "openid" in given.scope.split() and graph.claims is not None:
            tokens["id_token"] = _id_token(graph.claims)
        self._reply(200, tokens)

    def _download(self, graph: Graph, key: str) -> None:
        session = graph._sessions.get(f"d:{key}")  # pyright: ignore[reportPrivateUsage]
        stored = None if session is None else graph.files.get(session.path)
        if stored is None:
            self._error(404, "itemNotFound")
            return
        self._reply(200, stored.data, {"Content-Type": "application/octet-stream"})

    def _upload(self, graph: Graph, key: str, body: bytes) -> None:
        session = graph._sessions.get(f"u:{key}")  # pyright: ignore[reportPrivateUsage]
        if session is None:
            self._error(404, "itemNotFound")
            return
        stored = graph.files.get(session.path)
        if (None if stored is None else stored.tag) != session.tag:
            del graph._sessions[f"u:{key}"]  # pyright: ignore[reportPrivateUsage]
            self._error(404, "itemNotFound")  # as Graph answers: written or deleted since
            return
        given = (self.headers.get("Content-Range") or "").removeprefix("bytes ")
        span, _, whole = given.partition("/")
        start, _, end = span.partition("-")
        if int(start) != len(session.data) or int(end) - int(start) + 1 != len(body):
            self._error(416, "invalidRange")
            return
        session.data += body
        if len(session.data) < int(whole):
            self._reply(202, {"nextExpectedRanges": [f"{len(session.data)}-"]})
            return
        del graph._sessions[f"u:{key}"]  # pyright: ignore[reportPrivateUsage]
        if graph.full:
            self._error(507, "quotaLimitReached")
            return
        there = session.path in graph.files
        made = graph._write(session.path, bytes(session.data))  # pyright: ignore[reportPrivateUsage]
        self._reply(200 if there else 201, graph._item(session.path, made))  # pyright: ignore[reportPrivateUsage]

    def _graph(self, graph: Graph, path: str, query: dict[str, list[str]], body: bytes) -> None:
        if graph.answers:
            answer = graph.answers.pop(0)
            self._reply(answer.status, answer.body, answer.headers)
            return
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        until = graph.access_tokens.get(token)
        if graph.turning_down_access or until is None or until < time.monotonic():
            self._error(401, "InvalidAuthenticationToken")
            return
        split = _split(path) or _by_id(graph, path)
        if split is None:
            self._error(400, "invalidRequest")
            return
        item, then = split
        match self.command, then:
            case "GET", "/children":
                self._list(graph, item, query)
            case "GET", "":
                self._details(graph, item)
            case "PUT", "/content":
                self._made(graph, item, query, body)
            case "POST", "/createUploadSession":
                self._session(graph, item)
            case "DELETE", "":
                self._delete(graph, item)
            case _:
                self._error(400, "invalidRequest")

    def _list(self, graph: Graph, folder: str, query: dict[str, list[str]]) -> None:
        if folder not in graph._folders():  # pyright: ignore[reportPrivateUsage]
            self._error(404, "itemNotFound")
            return
        prefix = f"{folder}/" if folder else ""
        children: list[dict[str, object]] = []
        subfolders: set[str] = set()
        for path, stored in sorted(graph.files.items()):
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                subfolders.add(rest.partition("/")[0])
                continue
            children.append(graph._item(path, stored))  # pyright: ignore[reportPrivateUsage]
        children[:0] = [
            {"name": name, "size": 0, "folder": {"childCount": 1}} for name in sorted(subfolders)
        ]
        children += graph.also_listed.get(folder, [])
        top = int(query.get("$top", ["200"])[0])
        skip = int(query.get("$skiptoken", ["0"])[0])
        page: dict[str, object] = {"value": children[skip : skip + top]}
        if skip + top < len(children):
            if graph.next_link_by_id:
                place = f"/v1.0/drives/{DRIVE}/items/{_folder_id(folder)}/children"
            elif folder:
                place = f"{APPROOT}:/{quote(folder)}:/children"
            else:
                place = f"{APPROOT}/children"
            page["@odata.nextLink"] = (
                f"https://{graph.next_link_host}:{graph.port}{place}"
                f"?$select=name,size,eTag,file&$top={top}&$skiptoken={skip + top}"
            )
        self._reply(200, page)

    def _details(self, graph: Graph, path: str) -> None:
        stored = graph.files.get(path)
        if stored is None:
            if path in graph._folders():  # pyright: ignore[reportPrivateUsage]
                self._reply(200, {"name": path.rpartition("/")[2], "folder": {"childCount": 1}})
                return
            self._error(404, "itemNotFound")
            return
        self._reply(200, graph._item(path, stored))  # pyright: ignore[reportPrivateUsage]

    def _made(self, graph: Graph, path: str, query: dict[str, list[str]], body: bytes) -> None:
        failing = query.get("@microsoft.graph.conflictBehavior", ["replace"])[0] == "fail"
        there = path in graph.files
        if failing and there:
            self._error(409, "nameAlreadyExists")
            return
        if graph.full:
            self._error(507, "quotaLimitReached")
            return
        made = graph._write(path, body)  # pyright: ignore[reportPrivateUsage]
        self._reply(200 if there else 201, graph._item(path, made))  # pyright: ignore[reportPrivateUsage]

    def _session(self, graph: Graph, path: str) -> None:
        if path.rpartition("/")[0] not in graph._folders():  # pyright: ignore[reportPrivateUsage]
            self._error(404, "itemNotFound")  # as Graph answers for a folder not there
            return
        stored = graph.files.get(path)
        wanted = self.headers.get("If-Match")
        if wanted is not None and (stored is None or stored.tag != wanted):
            self._error(412, "preconditionFailed")
            return
        key = secrets.token_urlsafe(12)
        graph._sessions[f"u:{key}"] = _Session(  # pyright: ignore[reportPrivateUsage]
            path, bytearray(), None if stored is None else stored.tag
        )
        if path in graph.meanwhile:
            if (change := graph.meanwhile.pop(path)) is None:
                graph.files.pop(path, None)
            else:
                graph._write(path, change)  # pyright: ignore[reportPrivateUsage]
        self._reply(
            200,
            {
                "uploadUrl": (
                    f"https://{graph.content_host}:{graph.content_port or graph.port}/upload/{key}"
                ),
                "expirationDateTime": "2030-01-01T00:00:00Z",
                "nextExpectedRanges": ["0-"],
            },
        )

    def _delete(self, graph: Graph, path: str) -> None:
        stored = graph.files.get(path)
        if stored is None:
            self._error(404, "itemNotFound")
            return
        wanted = self.headers.get("If-Match")
        if wanted is not None and stored.tag != wanted:
            self._error(412, "preconditionFailed")
            return
        del graph.files[path]
        self._reply(204, b"")


# The browser, as the test plays it


def to_sign_in_page(graph: Graph, address: str) -> str:
    """As a browser at the sign-in page: where it is sent back to."""
    parts = urlsplit(address)
    connection = http.client.HTTPSConnection(HOST, parts.port, context=graph.context(), timeout=10)
    try:
        connection.request("GET", f"{parts.path}?{parts.query}")
        response = connection.getresponse()
        response.read()
        location = response.getheader("Location")
    finally:
        connection.close()
    assert response.status == 302 and location is not None
    return location


def back(
    location: str, *, via: str = "127.0.0.1", host: str | None = None, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    """As a browser sent back to the listener: its status, headers and page."""
    parts = urlsplit(location)
    assert parts.scheme == "http" and parts.hostname == "localhost" and parts.port
    connection = http.client.HTTPConnection(via, parts.port, timeout=10)
    try:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request(method, path, headers={"Host": host or parts.netloc})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    graph: Graph


_AUTHORITY: list[trustme.CA] = []  # one for all the tests of a run: making one takes a while


@contextmanager
def graph_server() -> Generator[Graph]:
    """Serving while in the with block, with a certificate the test authority issued."""
    if not _AUTHORITY:
        _AUTHORITY.append(trustme.CA())
    authority = _AUTHORITY[0]
    leaf = authority.issue_cert(HOST, "localhost")
    serving = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    leaf.configure_cert(serving)  # pyright: ignore[reportUnknownMemberType]
    client = ssl.create_default_context()
    authority.configure_trust(client)  # pyright: ignore[reportUnknownMemberType]
    server = _Server((HOST, 0), _Handler)
    server.socket = serving.wrap_socket(server.socket, server_side=True)
    graph = Graph(port=cast(tuple[str, int], server.server_address)[1], client_context=client)
    server.graph = graph
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield graph
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
