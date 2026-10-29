"""Fetching the website's version file, without Qt.

The HTTP and TLS libraries take 4 MB once loaded, for a request made once a
day, and a Python process never gives that back. So Stickle starts its own
program again with FEED_FLAG to fetch it (the file comes back on a pipe, the
memory goes with that process), and fetches it here only if that cannot start.
"""

import base64
import binascii
import logging
import subprocess
import urllib.error
import urllib.request
from typing import IO

from stickle.core.updates import FEED_URL, MAX_FEED_BYTES
from stickle.platform.relaunch import FETCH_FEED, no_window, own_command
from stickle.platform.tls import https_context

FEED_FLAG = FETCH_FEED
TIMEOUT_SECONDS = 15
HELPER_TIMEOUT_SECONDS = 60  # starting it, the request, and its time limit
log = logging.getLogger(__name__)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The file is asked for at one address only, and answered from there."""

    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _fetch() -> bytes:
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=https_context()), _NoRedirects()
    )
    request = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "Stickle", "Accept": "application/json"}
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        return bytes(response.read(MAX_FEED_BYTES + 1))


def fetch_feed() -> bytes | None:
    """The version file, fetched in this process, or None if it could not be
    read (logged)."""
    try:
        return _fetch()
    except (urllib.error.URLError, OSError, ValueError) as error:
        log.info("could not check for a newer version: %s", type(error).__name__)
        return None


def fetch_feed_in_helper(command: list[str] | None = None) -> bytes | None:
    """The version file, fetched by this program started again; here if that
    could not start or did not end in time. None if it could not be read (logged)."""
    try:
        done = subprocess.run(
            command if command is not None else own_command(FEED_FLAG),
            # Not this program's own: Windows can start a program with a standard
            # input that is gone, and handing that on fails.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=HELPER_TIMEOUT_SECONDS,
            creationflags=no_window(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.info("feed helper failed (%s): fetching here", type(error).__name__)
        return fetch_feed()
    if done.returncode != 0:
        # It ran, and could not read the file (offline, say): its reason alone.
        reason = done.stderr.decode("ascii", "replace").strip()[:80] or str(done.returncode)
        log.info("could not check for a newer version: %s", reason)
        return None
    try:
        return base64.b64decode(done.stdout, validate=True)
    except binascii.Error, ValueError:
        log.info("could not check for a newer version: the helper's answer")
        return None


def serve(stdout: IO[str], stderr: IO[str]) -> int:
    """The helper: the file, base64, on stdout; or the error's kind on stderr."""
    try:
        content = _fetch()
    except (urllib.error.URLError, OSError, ValueError) as error:
        stderr.write(type(error).__name__ + "\n")
        return 1
    stdout.write(base64.b64encode(content).decode("ascii"))
    stdout.flush()
    return 0
