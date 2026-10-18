"""Once a day, whether a newer Stickle is out.

A minute after the start, then every hour, it asks the website's version
file if a day has passed since it last asked (kept across restarts). The
request carries nothing that tells users apart: no cookie, no version, no
identifier. It runs in a thread of its own, so a slow network never holds up
the notes, and a failure is only logged. A newer version shows in the tray
and the Stickle window for as long as it holds: nothing pops up.
"""

import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from stickle import __version__
from stickle.core.clock import Clock, utc_now
from stickle.core.updates import FEED_URL, MAX_FEED_BYTES, Release, newer_release
from stickle.data.settings import UPDATE_CHECK, UPDATE_CHECKED_AT, Settings
from stickle.platform.tls import https_context

FIRST_DELAY_MS = 60_000
EVERY_MS = 60 * 60 * 1000
EVERY = timedelta(days=1)
TIMEOUT_SECONDS = 15
log = logging.getLogger(__name__)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The file is asked for at one address only, and answered from there."""

    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def fetch_feed() -> bytes | None:
    """The version file, or None if it could not be read (logged)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=https_context()), _NoRedirects()
    )
    request = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "Stickle", "Accept": "application/json"}
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return bytes(response.read(MAX_FEED_BYTES + 1))
    except (urllib.error.URLError, OSError, ValueError) as error:
        log.info("could not check for a newer version: %s", type(error).__name__)
        return None


def _moment(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class UpdateCheck(QObject):
    found = Signal(object)  # the newer Release
    switched = Signal(bool)  # turned on or off
    _answered = Signal(object)  # the file's bytes, or None, from the thread

    def __init__(
        self,
        settings: Settings,
        current: str = __version__,
        clock: Clock = utc_now,
        fetch: Callable[[], bytes | None] = fetch_feed,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._current = current
        self._clock = clock
        self._fetch = fetch
        self.release: Release | None = None
        self._asking = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_if_due)
        self._answered.connect(self._answer)

    @property
    def enabled(self) -> bool:
        return self._settings.get(UPDATE_CHECK)

    def set_enabled(self, on: bool) -> None:
        self._settings.set(UPDATE_CHECK, on)
        if not on:
            self.release = None  # no longer said
        log.info("update check %s", "on" if on else "off")
        self.switched.emit(on)
        if on:
            self.check_if_due()

    def start(self) -> None:
        QTimer.singleShot(FIRST_DELAY_MS, self, self.check_if_due)
        self._timer.start(EVERY_MS)

    def due(self) -> bool:
        last = self._settings.get(UPDATE_CHECKED_AT)
        return last is None or _moment(self._clock()) - _moment(last) >= EVERY

    def check_if_due(self) -> bool:
        """Ask now if it is on, a day has passed, and no question is out. Whether it asked."""
        if not self.enabled or self._asking or not self.due():
            return False
        # Kept before asking: a failure waits for the next day too, as promised.
        self._settings.set(UPDATE_CHECKED_AT, self._clock())
        self._asking = True
        threading.Thread(target=self._ask, name="update-check", daemon=True).start()
        return True

    def _ask(self) -> None:
        self._answered.emit(self._fetch())

    def _answer(self, content: object) -> None:
        self._asking = False
        if not isinstance(content, bytes) or not self.enabled:
            return
        release = newer_release(content, self._current)
        if release is None:
            log.info("no newer version")
            return
        log.info("a newer version is out: %s", release.version)
        self.release = release
        self.found.emit(release)
