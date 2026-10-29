"""Reach the credential store from a short-lived helper process.

The libraries that talk to the store (keyring, and on Linux secretstorage,
jeepney and cryptography) take 10 MB and more once loaded, and a Python process
never gives that back. So Stickle starts its own program again with
HELPER_FLAG: that process loads them, answers what it is asked on its standard
input (a JSON line a request, a JSON line an answer) and ends when the input
closes, or after a while with nothing asked; the memory goes with it.

Should the helper not start, not say it is ready in time, or end before
answering, the store is reached in this process instead, as it always was: the
notes still open, only the memory is not saved. The store's own failures
(locked, missing, refused) are passed on as they are, not retried here.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stickle.platform.credentials import Backend

log = logging.getLogger(__name__)

HELPER_FLAG = "--credential-helper"
READY = "ready"
# Starting the program again takes a tenth of a second, up to two seconds the
# first time Microsoft Defender sees a new build: well within this.
READY_TIMEOUT = 15.0
IDLE_TIMEOUT = 60.0  # the helper ends by itself after this long with nothing asked
CLOSE_TIMEOUT = 2.0


class StoreReplyError(Exception):
    """The store, reached by the helper, failed; kind says how (a type name)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _HelperFailedError(Exception):
    """The helper itself did not work: reach the store here instead."""


def helper_command() -> list[str]:
    """This program started again as the helper: the built program itself (inside
    the AppImage's mount, which is open already), or Python running the source."""
    if "__compiled__" in globals():  # a Nuitka build: argv[0] is the program
        return [str(Path(sys.argv[0]).resolve()), HELPER_FLAG]
    return [sys.executable, "-m", "stickle", HELPER_FLAG]


class HelperBackend:
    """A credential store backend whose work is done by the helper process,
    started at the first request and kept for those that follow (until close)."""

    def __init__(
        self,
        fallback: Callable[[], Backend],
        command: list[str] | None = None,
        ready_timeout: float = READY_TIMEOUT,
    ) -> None:
        self._fallback_factory = fallback
        self._command = command if command is not None else helper_command()
        self._ready_timeout = ready_timeout
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._fallback: Backend | None = None
        self._lock = threading.Lock()

    def get_password(self, service: str, username: str) -> str | None:
        reply = self._ask(
            {"op": "get", "service": service, "name": username},
            lambda backend: backend.get_password(service, username),
        )
        return cast(str | None, reply)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._ask(
            {"op": "set", "service": service, "name": username, "value": password},
            lambda backend: backend.set_password(service, username, password),
        )

    def delete_password(self, service: str, username: str) -> None:
        self._ask(
            {"op": "delete", "service": service, "name": username},
            lambda backend: backend.delete_password(service, username),
        )

    def close(self) -> None:
        """Let the helper end (its input closed), waited for a moment."""
        with self._lock:
            self._end_helper()

    @property
    def through_helper(self) -> bool:
        """Whether the requests so far went through the helper (none fell back)."""
        return self._fallback is None

    def _ask(self, request: dict[str, str], here: Callable[[Backend], object]) -> object:
        with self._lock:
            if self._fallback is None:
                try:
                    return self._through_helper(request)
                except _HelperFailedError as error:
                    log.warning("credential helper failed (%s): reaching the store here", error)
                    self._end_helper()
                    self._fallback = self._fallback_factory()
            return here(self._fallback)

    def _through_helper(self, request: dict[str, str]) -> object:
        process = self._process or self._start()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        except OSError as error:
            raise _HelperFailedError(f"could not ask: {type(error).__name__}") from error
        # No time limit while it is alive: the store may be waiting for the
        # person, as a Linux keyring does for its password.
        while True:
            try:
                line = self._lines.get(timeout=1.0)
            except queue.Empty:
                if process.poll() is not None:
                    raise _HelperFailedError(f"ended ({process.returncode})") from None
                continue
            if line is None:
                raise _HelperFailedError("ended before answering")
            try:
                reply = json.loads(line)
            except ValueError as error:
                raise _HelperFailedError("an answer that is not JSON") from error
            if not isinstance(reply, dict):
                raise _HelperFailedError("an answer that is not an object")
            answer = cast(dict[str, Any], reply)
            if "error" in answer:
                raise StoreReplyError(str(answer["error"]))
            return answer.get("value")

    def _start(self) -> subprocess.Popen[str]:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW  # Python from source has a console
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # nothing of the store's ever reaches a log
                text=True,
                encoding="ascii",
                creationflags=flags,
            )
        except OSError as error:
            raise _HelperFailedError(f"could not start: {type(error).__name__}") from error
        assert process.stdout is not None
        threading.Thread(
            target=_read_lines,
            args=(process.stdout, self._lines),
            name="stickle-helper",
            daemon=True,
        ).start()
        try:
            first = self._lines.get(timeout=self._ready_timeout)
        except queue.Empty:
            process.kill()
            raise _HelperFailedError("not ready in time") from None
        if first != READY:
            process.kill()
            raise _HelperFailedError("did not say it was ready")
        self._process = process
        return process

    def _end_helper(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=CLOSE_TIMEOUT)
        except OSError, subprocess.TimeoutExpired:
            process.kill()


def _read_lines(stream: IO[str], lines: queue.Queue[str | None]) -> None:
    for line in stream:
        lines.put(line.strip())
    lines.put(None)  # ended


def serve(stdin: IO[str], stdout: IO[str], backend: Callable[[], Backend]) -> int:
    """The helper: ready, then a JSON answer for each JSON request, until the
    input ends or nothing comes for IDLE_TIMEOUT."""
    stdout.write(READY + "\n")
    stdout.flush()
    store: Backend | None = None
    idle = _Idle(IDLE_TIMEOUT)
    for line in stdin:
        idle.reset()
        try:
            request = cast(dict[str, str], json.loads(line))
            if store is None:
                store = backend()
            reply = _answer(store, request)
        except Exception as error:
            # The store's error by its kind only: its message may name the secret.
            reply = {"error": _kind(error)}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()
    return 0


def _answer(store: Backend, request: dict[str, str]) -> dict[str, object]:
    op, service, name = request["op"], request["service"], request["name"]
    if op == "get":
        return {"value": store.get_password(service, name)}
    if op == "set":
        store.set_password(service, name, request["value"])
        return {"value": None}
    if op == "delete":
        store.delete_password(service, name)
        return {"value": None}
    raise ValueError(op)


def _kind(error: Exception) -> str:
    from stickle.platform.credentials import CredentialStoreUnavailableError

    if isinstance(error, CredentialStoreUnavailableError):
        return str(error)  # no store here: says which, without secrets
    return type(error).__name__


class _Idle:
    """Ends the helper after a while with nothing asked (its owner forgot it)."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._timer: threading.Timer | None = None
        self.reset()

    def reset(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._seconds, _end_now)
        self._timer.daemon = True
        self._timer.start()


def _end_now() -> None:
    os._exit(0)  # at once, from the timer's thread: the main one waits on its input
