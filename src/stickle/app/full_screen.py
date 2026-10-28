"""Notes out of the way of programs shown full screen: a slide show, a video, a game.

Twice a second the system is asked (cheaply, while nothing is full screen) which
windows of other programs are; the screens they cover are handed to the notes
(see NoteManager.cover), whose notes there go out of sight until it ends. The
programs that sent notes away while Stickle runs are kept, in memory only, for
the settings to offer as exceptions; the exceptions alone are stored.
"""

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QScreen

from stickle.data.settings import FULL_SCREEN_EXCEPTIONS, HIDE_FOR_FULL_SCREEN, Settings
from stickle.platform.full_screen import FullScreen, FullScreenSource

log = logging.getLogger(__name__)

CHECK_MS = 500


class FullScreenWatch(QObject):
    """Which screens full-screen programs cover now (screens), as it changes."""

    covered_changed = Signal()
    # A program sent notes away for the first time in this run (see apps).
    app_found = Signal()

    def __init__(self, source: FullScreenSource | None, settings: Settings | None = None) -> None:
        super().__init__()
        self._source = source
        self._settings = settings
        self.screens: set[QScreen] = set()
        # The programs that sent notes away in this run, the first first.
        self.apps: list[str] = []
        self._known: list[int] = []
        self._on = settings.get(HIDE_FOR_FULL_SCREEN) if settings else True
        self._exceptions = settings.get(FULL_SCREEN_EXCEPTIONS) if settings else []
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self.check)

    @property
    def available(self) -> bool:
        """Whether full-screen windows can be told here (not under Wayland)."""
        return self._source is not None

    @property
    def on(self) -> bool:
        return self._on

    @property
    def exceptions(self) -> list[str]:
        """The programs over which notes stay in sight."""
        return list(self._exceptions)

    def start(self) -> None:
        if self._source is not None and self._on:
            self._timer.start()
            self.check()

    def switch(self, on: bool) -> None:
        """Turned on or off (and kept so on this computer)."""
        self._on = on
        if self._settings is not None:
            self._settings.set(HIDE_FOR_FULL_SCREEN, on)
        if on:
            self.start()
        else:
            self._timer.stop()
            self._known = []
            self._cover(set())

    def except_app(self, app: str) -> None:
        """Notes stay in sight over this program from now on."""
        if _excepted(app, self._exceptions):
            return
        self._keep_exceptions([*self._exceptions, app])

    def unexcept_app(self, app: str) -> None:
        self._keep_exceptions([name for name in self._exceptions if name != app])

    def _keep_exceptions(self, names: list[str]) -> None:
        if self._settings is not None:
            self._settings.set(FULL_SCREEN_EXCEPTIONS, names)
        self._exceptions = names
        if self._timer.isActive():
            self.check()

    def check(self) -> None:
        if self._source is None:
            return
        try:
            found = self._source.find(self._known)
        except OSError as error:
            log.warning("could not look for full-screen windows: %s", type(error).__name__)
            found = []
        self._known = [shown.window for shown in found]
        covering = [shown for shown in found if not _excepted(shown.app, self._exceptions)]
        for shown in covering:
            if shown.app and not _excepted(shown.app, self.apps):
                self.apps.append(shown.app)
                self.app_found.emit()
        self._cover(covered_screens(covering, QGuiApplication.screens()))

    def _cover(self, screens: set[QScreen]) -> None:
        if screens != self.screens:
            self.screens = screens
            log.info("screens covered by full-screen programs: %d", len(screens))
            self.covered_changed.emit()


def _excepted(app: str, names: list[str]) -> bool:
    """Among the names, as file names compare on Windows (whatever the case)."""
    return app.casefold() in (name.casefold() for name in names)


def covered_screens(found: list[FullScreen], screens: list[QScreen]) -> set[QScreen]:
    """The screens the windows cover: by the display's name where the system
    gives it (Windows), else by where the screen's middle is."""
    covered: set[QScreen] = set()
    for screen in screens:
        for shown in found:
            if (shown.monitor_name and screen.name() == shown.monitor_name) or _inside(
                screen, shown.monitor
            ):
                covered.add(screen)
                break
    return covered


def _inside(screen: QScreen, monitor: tuple[int, int, int, int]) -> bool:
    # Qt places each screen where the system does and only scales its size.
    area = screen.geometry()
    scale = screen.devicePixelRatio()
    x = area.x() + area.width() * scale / 2
    y = area.y() + area.height() * scale / 2
    left, top, width, height = monitor
    return left <= x < left + width and top <= y < top + height
