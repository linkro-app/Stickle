"""Dropbox's API in the test, over TLS with a test certificate authority
(trustme), for the Dropbox target to talk real HTTPS to.

It answers as Dropbox's documentation says it does, for what the target asks:
the app's folder (its files, a page at a time), a download with the file's
rev, an upload in "add" or "update" mode kept as strict_conflict keeps it, a
delete with or without parent_rev, a file's details, a folder's cursor and what
changed since one, the long poll, revoking a token, and access tokens from a
refresh token (no new refresh token: Dropbox's last until revoked). A write of
other bytes gives the file a new rev; the same bytes again keep it.

Signing in: the page to allow the app (the browser's, as the test plays it)
sends the browser back to a registered address with a code, or, given none,
shows the code for the user to enter; the token address exchanges a code once,
for the app it was given to, with the verifier its challenge was made from (a
refresh token only if one was asked for: offline). The account's details tell
its email address.

What Dropbox does not document, it does as the real one was seen to
(test_real_dropbox): a delete whose parent_rev is not the file's is refused as
a conflict. Should Dropbox ever answer that there is no file there instead,
the test says so (mismatch_not_found).

Four servers share one state, as Dropbox's hosts: the API, content and notify,
and the site the browser signs in at. Each answers its own requests only, so
that one sent to another host fails as it would. The test says what goes wrong
(an answer for the next requests, tokens turned down, the app turned away, no
room left, cursors too old) and looks at every request that reached it.
"""

import base64
import hashlib
import json
import secrets
import ssl
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import trustme

from stickle.sync.targets.dropbox import REDIRECT_PORTS, Place

HOST = "127.0.0.1"
APP_KEY = "a1b2c3d4e5f6g7h"  # a test's, not the app's
FIRST_REFRESH = "refresh-token-first-of-a-test"
LASTS = 14400  # seconds an access token is good for, as Dropbox's
API, CONTENT, NOTIFY, WWW = "api", "content", "notify", "www"
TOKEN_PATH = "/oauth2/token"
AUTHORIZE = "/oauth2/authorize"
ACCOUNT = "someone@example.com"  # the account's email address, as Dropbox tells it
LONGPOLL = "/2/files/list_folder/longpoll"
BLOCK = 4 * 1024 * 1024  # Dropbox's content hash is of blocks this size


@dataclass(frozen=True)
class Asked:
    """A request that reached one of the servers."""

    host: str  # API, CONTENT, NOTIFY or WWW
    path: str
    authorization: str | None
    arg: dict[str, object] | None  # Dropbox-API-Arg, read
    body: bytes

    @property
    def args(self) -> dict[str, object]:
        """What an RPC was given."""
        return cast(dict[str, object], json.loads(self.body))


@dataclass(frozen=True)
class Answer:
    """What the next request is answered, whatever it asks."""

    status: int
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: dict[str, object] | bytes = field(default_factory=dict[str, object])


@dataclass(frozen=True)
class _Code:
    app_key: str
    redirect_uri: str | None  # None: shown to the user, to enter
    challenge: str
    scope: str
    offline: bool  # a refresh token asked for


@dataclass
class _File:
    data: bytes
    rev: str
    item_id: str


@dataclass(frozen=True)
class _Change:
    seq: int
    path: str  # lower, no "/" first: "devices/a.json"


@dataclass
class _Cursor:
    folder: str
    seq: int  # what changed after this is told
    epoch: int
    left: list[dict[str, object]] | None = None  # a listing's entries still to come
    limit: int = 2000  # entries on a page of it, as asked


def _parent(path: str) -> str:
    return path.rpartition("/")[0]


def _content_hash(data: bytes) -> str:
    blocks = b"".join(
        hashlib.sha256(data[start : start + BLOCK]).digest() for start in range(0, len(data), BLOCK)
    )
    return hashlib.sha256(blocks).hexdigest()


def _failure(summary: str, error: dict[str, object]) -> dict[str, object]:
    return {"error_summary": summary, "error": error}


def _not_found(tag: str = "path") -> dict[str, object]:
    return _failure(f"{tag}/not_found/.", {".tag": tag, tag: {".tag": "not_found"}})


def _write_failed(reason: str) -> dict[str, object]:
    why: dict[str, object] = {".tag": reason}
    if reason == "conflict":
        why["conflict"] = {".tag": "file"}
    return _failure(
        f"path/{reason}/.", {".tag": "path", "reason": why, "upload_session_id": "AAAA"}
    )


@dataclass
class DropboxState:
    """The servers' state, which the test reads and changes between requests."""

    ports: tuple[int, int, int] = (0, 0, 0)
    www_port: int = 0
    redirect_ports: tuple[int, ...] = REDIRECT_PORTS  # the app's, as registered
    client_context: ssl.SSLContext | None = None
    asked: list[Asked] = field(default_factory=list[Asked])
    files: dict[str, _File] = field(default_factory=dict[str, _File])  # "notes/x.json"
    folders: set[str] = field(default_factory=lambda: {""})
    answers: list[Answer] = field(default_factory=list[Answer])  # the next API and content ones
    notify_answers: list[Answer] = field(default_factory=list[Answer])  # the next long polls
    token_answers: list[Answer] = field(default_factory=list[Answer])  # the next refreshes
    refresh_tokens: set[str] = field(default_factory=lambda: {FIRST_REFRESH})
    # Every access token it gave: until when (monotonic), and the refresh token it came from.
    access_tokens: dict[str, tuple[float, str]] = field(
        default_factory=dict[str, tuple[float, str]]
    )
    issued: list[str] = field(default_factory=list[str])
    refusing_refresh: bool = False  # as when the user took the app's access away
    app_turned_away: bool = False  # as when Dropbox stopped the app
    turning_down_access: bool = False  # every access token, however new
    full: bool = False
    mismatch_not_found: bool = False  # a delete at another rev: "not found", not a conflict
    page: int = 2000  # entries on a page, at most
    hold: float = 2.0  # seconds a long poll waits, at most, in the test
    backoff: int | None = None  # in the long poll's answers
    answer_late: float = 0.0  # seconds an upload's answer comes after the file is written
    declining: bool = False  # the user says no to the app on the sign-in page
    email: object = ACCOUNT  # as the account's details tell it; None: not at all
    revoked: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: threading.Condition = field(init=False)
    closing: bool = False
    _changes: list[_Change] = field(default_factory=list[_Change])
    _cursors: dict[str, _Cursor] = field(default_factory=dict[str, _Cursor])
    _seq: int = 0
    _epoch: int = 0
    _codes: dict[str, _Code] = field(default_factory=dict[str, _Code])

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)

    @property
    def place(self) -> Place:
        """Where the target finds it, as it finds Dropbox."""
        return Place(
            api=HOST,
            content=HOST,
            notify=HOST,
            www=HOST,
            ports=self.ports,
            www_port=self.www_port,
            redirect_ports=self.redirect_ports,
        )

    def context(self) -> ssl.SSLContext:
        """One that trusts the servers' certificate, as a public authority's would be."""
        assert self.client_context is not None
        return self.client_context

    def put_by_hand(self, path: str, data: bytes) -> str:
        """A file written as by another device; its rev."""
        with self.lock:
            return self._write(path, data).rev

    def delete_by_hand(self, path: str) -> None:
        """A file or a folder deleted, as by another device or the user."""
        with self.lock:
            self._delete(path)

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access_tokens.clear()

    def expire_cursors(self) -> None:
        """As Dropbox does to old cursors."""
        with self.lock:
            self._epoch += 1

    def requests(self, host: str, path: str | None = None) -> list[Asked]:
        with self.lock:
            return [
                each
                for each in self.asked
                if each.host == host and (path is None or each.path == path)
            ]

    # As the handler asks

    def _write(self, path: str, data: bytes) -> _File:
        old = self.files.get(path)
        self._seq += 1
        made = _File(data, f"{self._seq:012x}", old.item_id if old else secrets.token_hex(8))
        parts = path.split("/")[:-1]
        for end in range(1, len(parts) + 1):
            folder = "/".join(parts[:end])
            if folder not in self.folders:
                self.folders.add(folder)
                self._changes.append(_Change(self._seq, folder))
        self.files[path] = made
        self._changes.append(_Change(self._seq, path))
        self.changed.notify_all()
        return made

    def _delete(self, path: str) -> None:
        self._seq += 1

        def inside(each: str) -> bool:  # "": all of the app's folder, as the user deletes it
            return bool(each) and (not path or each == path or each.startswith(f"{path}/"))

        gone = [each for each in self.files if inside(each)]
        gone += [each for each in self.folders if inside(each)]
        for each in gone:
            self.files.pop(each, None)
            self.folders.discard(each)
            self._changes.append(_Change(self._seq, each))
        self.changed.notify_all()

    def _metadata(self, path: str, tagged: bool = True) -> dict[str, object]:
        name = path.rpartition("/")[2]
        stored = self.files.get(path)
        if stored is None:
            item: dict[str, object] = {
                ".tag": "folder" if path in self.folders else "deleted",
                "name": name,
                "path_lower": f"/{path}",
                "path_display": f"/{path}",
            }
            if path in self.folders:
                item["id"] = f"id:{hashlib.sha256(path.encode()).hexdigest()[:16]}"
            return item
        item = {
            ".tag": "file",
            "name": name,
            "path_lower": f"/{path}",
            "path_display": f"/{path}",
            "id": f"id:{stored.item_id}",
            "client_modified": "2026-11-24T00:00:00Z",
            "server_modified": "2026-11-24T00:00:00Z",
            "rev": stored.rev,
            "size": len(stored.data),
            "is_downloadable": True,
            "content_hash": _content_hash(stored.data),
        }
        if not tagged:
            del item[".tag"]
        return item

    def _cursor(self, cursor: _Cursor) -> str:
        key = f"AAH{secrets.token_urlsafe(24)}"
        self._cursors[key] = cursor
        return key

    def _since(self, cursor: _Cursor) -> list[str]:
        """What changed in the cursor's folder since it, each once, as last changed."""
        paths: list[str] = []
        for change in self._changes:
            if change.seq > cursor.seq and _parent(change.path) == cursor.folder:
                if change.path in paths:
                    paths.remove(change.path)
                paths.append(change.path)
        return paths

    def _children(self, folder: str) -> list[str]:
        return sorted(
            each for each in {*self.files, *self.folders} if each and _parent(each) == folder
        )

    def _page(
        self, entries: list[dict[str, object]], folder: str, seq: int, limit: int = 2000
    ) -> dict[str, object]:
        """A page of entries, and a cursor for the rest or for what changes next."""
        most = min(limit, self.page)
        shown, left = entries[:most], entries[most:]
        cursor = _Cursor(folder, seq, self._epoch, left or None, limit)
        return {"entries": shown, "cursor": self._cursor(cursor), "has_more": bool(left)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(
        self,
        status: int,
        body: dict[str, object] | bytes | None,
        headers: dict[str, str] | None = None,
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

    def _bad(self, why: str) -> None:
        self._reply(
            400, f"Error in call to API function: {why}".encode(), {"Content-Type": "text/plain"}
        )

    def do_GET(self) -> None:
        server = cast(_Server, self.server)
        box, host = server.box, server.host
        parts = urlsplit(self.path)
        with box.lock:
            box.asked.append(Asked(host, parts.path, self.headers.get("Authorization"), None, b""))
            if host != WWW or parts.path != AUTHORIZE:
                self._reply(404, b"not on this host", {"Content-Type": "text/plain"})
                return
            self._authorize(box, parts.query)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server = cast(_Server, self.server)
        box, host = server.box, server.host
        given = self.headers.get("Dropbox-API-Arg")
        arg = None if given is None else cast(dict[str, object], json.loads(given))
        with box.lock:
            box.asked.append(Asked(host, self.path, self.headers.get("Authorization"), arg, body))
            if host == NOTIFY:
                if self.path != LONGPOLL:
                    self._reply(404, b"not on this host", {"Content-Type": "text/plain"})
                elif box.notify_answers:
                    answer = box.notify_answers.pop(0)
                    self._reply(answer.status, answer.body, answer.headers)
                else:
                    self._longpoll(box, body)
                return
            if host == API and self.path == TOKEN_PATH:
                self._token(box, body)
                return
            if box.answers:
                answer = box.answers.pop(0)
                self._reply(answer.status, answer.body, answer.headers)
                return
            routes = {
                (API, "/2/files/list_folder"): self._list,
                (API, "/2/files/list_folder/continue"): self._continue,
                (API, "/2/files/list_folder/get_latest_cursor"): self._latest,
                (API, "/2/files/delete_v2"): self._delete,
                (API, "/2/files/get_metadata"): self._details,
                (API, "/2/auth/token/revoke"): self._revoke,
                (API, "/2/users/get_current_account"): self._current_account,
                (CONTENT, "/2/files/download"): self._download,
                (CONTENT, "/2/files/upload"): self._upload,
            }
            route = routes.get((host, self.path))
            if route is None:
                self._reply(404, b"not on this host", {"Content-Type": "text/plain"})
                return
            if not self._signed_in(box):
                return
            if host == API:
                if self.headers.get("Content-Type") != "application/json":
                    self._bad('Bad HTTP "Content-Type" header')
                    return
                try:
                    args = json.loads(body)
                except ValueError:
                    self._bad("could not decode input as JSON")
                    return
                given_args = cast(dict[str, object], args) if isinstance(args, dict) else {}
                route(box, given_args, body)
            else:
                if arg is None:
                    self._bad('Must provide HTTP header "Dropbox-API-Arg"')
                    return
                route(box, arg, body)

    def _signed_in(self, box: DropboxState) -> bool:
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        known = box.access_tokens.get(token)
        if box.turning_down_access or known is None:
            self._reply(401, _failure("invalid_access_token/", {".tag": "invalid_access_token"}))
            return False
        if known[0] < time.monotonic():
            self._reply(401, _failure("expired_access_token/", {".tag": "expired_access_token"}))
            return False
        return True

    # The API host

    def _token(self, box: DropboxState, body: bytes) -> None:
        if box.token_answers:
            answer = box.token_answers.pop(0)
            self._reply(answer.status, answer.body, answer.headers)
            return
        form = {name: values[0] for name, values in parse_qs(body.decode("ascii")).items()}
        if box.app_turned_away or form.get("client_id") != APP_KEY:
            self._reply(400, {"error": "invalid_client", "error_description": "Invalid client_id"})
            return
        if form.get("grant_type") == "authorization_code":
            self._redeem(box, form)
            return
        refresh = form.get("refresh_token", "")
        if (
            box.refusing_refresh
            or form.get("grant_type") != "refresh_token"
            or refresh not in box.refresh_tokens
        ):
            self._reply(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "refresh token is invalid or revoked",
                },
            )
            return
        access = f"sl.u.{secrets.token_urlsafe(24)}"
        box.access_tokens[access] = (time.monotonic() + LASTS, refresh)
        box.issued.append(access)
        self._reply(200, {"access_token": access, "token_type": "bearer", "expires_in": LASTS})

    def _redeem(self, box: DropboxState, form: dict[str, str]) -> None:
        given = box._codes.pop(form.get("code", ""), None)  # pyright: ignore[reportPrivateUsage]
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if (
            given is None
            or given.app_key != form.get("client_id")
            or given.redirect_uri != form.get("redirect_uri")
            or not verifier
            or given.challenge != challenge
        ):
            self._reply(400, {"error": "invalid_grant", "error_description": "code doesn't exist"})
            return
        access = f"sl.u.{secrets.token_urlsafe(24)}"
        tokens: dict[str, object] = {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": LASTS,
            "scope": given.scope,
            "uid": "12345678",
            "account_id": "dbid:AAH4f99T0taONIb-OurWxbNQ6ywGRopQngc",
        }
        refresh = ""
        if given.offline:
            refresh = f"refresh-{secrets.token_urlsafe(16)}"
            box.refresh_tokens.add(refresh)
            tokens["refresh_token"] = refresh
        box.access_tokens[access] = (time.monotonic() + LASTS, refresh)
        box.issued.append(access)
        self._reply(200, tokens)

    def _current_account(self, box: DropboxState, _args: dict[str, object], _body: bytes) -> None:
        account: dict[str, object] = {
            "account_id": "dbid:AAH4f99T0taONIb-OurWxbNQ6ywGRopQngc",
            "name": {"display_name": "Some One", "given_name": "Some", "surname": "One"},
            "email_verified": True,
            "disabled": False,
            "country": "KR",
            "locale": "ko",
            "account_type": {".tag": "basic"},
        }
        if box.email is not None:
            account["email"] = box.email
        self._reply(200, account)

    def _revoke(self, box: DropboxState, _args: dict[str, object], _body: bytes) -> None:
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        refresh = box.access_tokens[token][1]
        box.refresh_tokens.discard(refresh)
        for access, (_until, made_from) in list(box.access_tokens.items()):
            if made_from == refresh:
                del box.access_tokens[access]
        box.revoked += 1
        self._reply(200, b"null", {"Content-Type": "application/json"})

    def _list(self, box: DropboxState, args: dict[str, object], _body: bytes) -> None:
        folder = str(args.get("path", "")).lower().removeprefix("/")
        limit = args.get("limit", 2000)
        if not isinstance(limit, int) or not 1 <= limit <= 2000 or args.get("recursive"):
            self._bad("limit, or recursive")
            return
        if folder not in box.folders:
            self._reply(409, _not_found())
            return
        entries = [box._metadata(each) for each in box._children(folder)]  # pyright: ignore[reportPrivateUsage]
        self._reply(200, box._page(entries, folder, box._seq, limit))  # pyright: ignore[reportPrivateUsage]

    def _continue(self, box: DropboxState, args: dict[str, object], _body: bytes) -> None:
        cursor = box._cursors.get(str(args.get("cursor")))  # pyright: ignore[reportPrivateUsage]
        if cursor is None:
            self._bad("invalid cursor")
            return
        if cursor.epoch != box._epoch:  # pyright: ignore[reportPrivateUsage]
            self._reply(409, _failure("reset/", {".tag": "reset"}))
            return
        if cursor.left is not None:
            page = box._page(cursor.left, cursor.folder, cursor.seq, cursor.limit)  # pyright: ignore[reportPrivateUsage]
            self._reply(200, page)
            return
        entries = [box._metadata(each) for each in box._since(cursor)]  # pyright: ignore[reportPrivateUsage]
        self._reply(200, box._page(entries, cursor.folder, box._seq))  # pyright: ignore[reportPrivateUsage]

    def _latest(self, box: DropboxState, args: dict[str, object], _body: bytes) -> None:
        folder = str(args.get("path", "")).lower().removeprefix("/")
        if args.get("recursive"):
            self._bad("recursive")
            return
        if folder not in box.folders:
            self._reply(409, _not_found())
            return
        cursor = _Cursor(folder, box._seq, box._epoch)  # pyright: ignore[reportPrivateUsage]
        self._reply(200, {"cursor": box._cursor(cursor)})  # pyright: ignore[reportPrivateUsage]

    def _delete(self, box: DropboxState, args: dict[str, object], _body: bytes) -> None:
        path = str(args.get("path", "")).lower().removeprefix("/")
        rev = args.get("parent_rev")
        if rev is not None and (not isinstance(rev, str) or len(rev) < 9):
            self._bad("parent_rev: shorter than 9")
            return
        stored = box.files.get(path)
        if stored is None and path not in box.folders:
            self._reply(409, _not_found("path_lookup"))
            return
        if rev is not None and (stored is None or stored.rev != rev):
            if box.mismatch_not_found:
                self._reply(409, _not_found("path_lookup"))
            else:
                error: dict[str, object] = {
                    ".tag": "path_write",
                    "path_write": {".tag": "conflict", "conflict": {".tag": "file"}},
                }
                self._reply(409, _failure("path_write/conflict/file/", error))
            return
        details = box._metadata(path)  # pyright: ignore[reportPrivateUsage]
        box._delete(path)  # pyright: ignore[reportPrivateUsage]
        self._reply(200, {"metadata": details})

    def _details(self, box: DropboxState, args: dict[str, object], _body: bytes) -> None:
        path = str(args.get("path", "")).lower().removeprefix("/")
        if path not in box.files and path not in box.folders:
            self._reply(409, _not_found())
            return
        self._reply(200, box._metadata(path))  # pyright: ignore[reportPrivateUsage]

    # The content host

    def _download(self, box: DropboxState, arg: dict[str, object], _body: bytes) -> None:
        path = str(arg.get("path", "")).lower().removeprefix("/")
        stored = box.files.get(path)
        if stored is None:
            if path in box.folders:
                self._reply(
                    409, _failure("path/not_file/.", {".tag": "path", "path": {".tag": "not_file"}})
                )
            else:
                self._reply(409, _not_found())
            return
        result = json.dumps(box._metadata(path, tagged=False))  # pyright: ignore[reportPrivateUsage]
        self._reply(
            200,
            stored.data,
            {"Content-Type": "application/octet-stream", "Dropbox-API-Result": result},
        )

    def _upload(self, box: DropboxState, arg: dict[str, object], body: bytes) -> None:
        if self.headers.get("Content-Type") != "application/octet-stream":
            self._bad('Bad HTTP "Content-Type" header')
            return
        path = str(arg.get("path", "")).lower().removeprefix("/")
        mode, strict = arg.get("mode", "add"), arg.get("strict_conflict") is True
        if arg.get("autorename") is not False:
            self._bad("this test's uploads never autorename")
            return
        stored = box.files.get(path)
        same = stored is not None and stored.data == body
        if mode == "add":
            if stored is not None and (strict or not same):
                self._reply(409, _write_failed("conflict"))
                return
        elif isinstance(mode, dict) and cast(dict[str, object], mode).get(".tag") == "update":
            rev = cast(dict[str, object], mode).get("update")
            if not isinstance(rev, str) or len(rev) < 9:
                self._bad("update: shorter than 9")
                return
            if (stored is None and strict) or (stored is not None and stored.rev != rev):
                self._reply(409, _write_failed("conflict"))
                return
        elif mode != "overwrite":
            self._bad("mode")
            return
        if path in box.folders:
            self._reply(409, _write_failed("conflict"))
            return
        if box.full:
            self._reply(409, _write_failed("insufficient_space"))
            return
        if not same:
            box._write(path, body)  # pyright: ignore[reportPrivateUsage]
        written = box._metadata(path, tagged=False)  # pyright: ignore[reportPrivateUsage]
        until = time.monotonic() + box.answer_late
        while not box.closing and (left := until - time.monotonic()) > 0:
            box.changed.wait(left)  # the lock let go: the long poll is told first
        self._reply(200, written)

    # The site the browser signs in at

    def _authorize(self, box: DropboxState, query: str) -> None:
        given = {name: values[0] for name, values in parse_qs(query).items()}
        redirect = given.get("redirect_uri")
        if box.app_turned_away or given.get("client_id") != APP_KEY:
            self._reply(400, b"Invalid client_id", {"Content-Type": "text/plain"})  # its own page
            return
        if redirect is not None and redirect not in {
            f"http://127.0.0.1:{port}" for port in box.redirect_ports
        }:
            self._reply(400, b"Invalid redirect_uri", {"Content-Type": "text/plain"})
            return
        answer: dict[str, str]
        if (
            given.get("response_type") != "code"
            or given.get("code_challenge_method") != "S256"
            or not given.get("code_challenge")
        ):
            answer = {"error": "invalid_request", "error_description": "No code_challenge"}
        elif box.declining:
            answer = {"error": "access_denied", "error_description": "The user chose not to"}
        else:
            code = secrets.token_urlsafe(32)
            box._codes[code] = _Code(  # pyright: ignore[reportPrivateUsage]
                given["client_id"],
                redirect,
                given["code_challenge"],
                given.get("scope", ""),
                given.get("token_access_type") == "offline",
            )
            answer = {"code": code}
        if redirect is None:  # the code shown, to enter in the app
            code = answer.get("code")
            text = f"<code>{escape(code)}</code>" if code else "Something went wrong."
            self._reply(200 if code else 400, text.encode(), {"Content-Type": "text/html"})
            return
        if "state" in given:
            answer["state"] = given["state"]
        self._reply(302, b"", {"Location": f"{redirect}?{urlencode(answer)}"})

    # The notify host

    def _longpoll(self, box: DropboxState, body: bytes) -> None:
        if self.headers.get("Authorization") is not None:
            self._bad("unexpected Authorization header")  # it takes none
            return
        try:
            args = cast(dict[str, object], json.loads(body))
        except ValueError, TypeError:
            self._bad("could not decode input as JSON")
            return
        timeout = args.get("timeout", 30)
        cursor = box._cursors.get(str(args.get("cursor")))  # pyright: ignore[reportPrivateUsage]
        if not isinstance(timeout, int) or not 30 <= timeout <= 480 or cursor is None:
            self._bad("timeout, or cursor")
            return
        if cursor.epoch != box._epoch:  # pyright: ignore[reportPrivateUsage]
            self._reply(409, _failure("reset/", {".tag": "reset"}))
            return
        until = time.monotonic() + min(timeout, box.hold)
        while not box._since(cursor) and not box.closing:  # pyright: ignore[reportPrivateUsage]
            left = until - time.monotonic()
            if left <= 0:
                break
            box.changed.wait(left)
        answer: dict[str, object] = {"changes": bool(box._since(cursor))}  # pyright: ignore[reportPrivateUsage]
        if box.backoff is not None:
            answer["backoff"] = box.backoff
        self._reply(200, answer)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    box: DropboxState
    host: str

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # a long poll cut by the client, written to a closed connection


_AUTHORITY: list[trustme.CA] = []  # one for all the tests of a run: making one takes a while


@contextmanager
def dropbox_server() -> Generator[DropboxState]:
    """Serving while in the with block, with a certificate the test authority issued."""
    if not _AUTHORITY:
        _AUTHORITY.append(trustme.CA())
    authority = _AUTHORITY[0]
    leaf = authority.issue_cert(HOST, "localhost")
    serving = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    leaf.configure_cert(serving)  # pyright: ignore[reportUnknownMemberType]
    client = ssl.create_default_context()
    authority.configure_trust(client)  # pyright: ignore[reportUnknownMemberType]
    box = DropboxState(client_context=client)
    servers: list[_Server] = []
    for host in (API, CONTENT, NOTIFY, WWW):
        server = _Server((HOST, 0), _Handler)
        server.socket = serving.wrap_socket(server.socket, server_side=True)
        server.box, server.host = box, host
        servers.append(server)
    found = [cast(tuple[str, int], each.server_address)[1] for each in servers]
    box.ports, box.www_port = (found[0], found[1], found[2]), found[3]
    threads = [
        threading.Thread(target=each.serve_forever, args=(0.05,), daemon=True) for each in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield box
    finally:
        with box.lock:
            box.closing = True
            box.changed.notify_all()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(10)
