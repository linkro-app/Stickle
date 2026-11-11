"""How sync stands, where it is seen: the tray, a line at the foot of Stickle
Manager, and each note that has not reached the cloud. Stopped for something a
window asks about, it is told once: a line at the top of Stickle Manager until
the user says OK and, with that window closed, one notification of the system.
The lines that say how sync stands then lead to that window.

Loaded with sync off too: nothing here brings sync, which it is handed once it
runs (stickle.app.syncing), nor the windows that ask, until one is wanted.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, override

import apsw
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stickle.app.app_icon import SyncSign, halted_pixmap
from stickle.app.in_use import call_up
from stickle.app.link_label import LinkLabel
from stickle.app.tray import SyncShown
from stickle.data.settings import SYNC_STOP_SEEN, SYNC_STOP_TOLD, Setting, Settings

if TYPE_CHECKING:
    from stickle.app.i18n import Translations
    from stickle.app.notes import NoteManager
    from stickle.app.stickle_window import StickleWindow
    from stickle.app.sync_setup import SyncSetup
    from stickle.app.sync_stopped import SyncStopped
    from stickle.app.syncing import Ended, Syncing
    from stickle.app.tray import Tray
    from stickle.app.turn_on_sync import TurnOnSync

log = logging.getLogger(__name__)

DOT_SIZE = 12
# Tells the system once (what it says; what a click on it opens).
type Notify = Callable[[str, Callable[[], object]], object]


class SyncStopLine(QWidget):
    """At the top of Stickle Manager: sync stopped, and why, with the way to the
    window that asks what to do, until the user says OK."""

    review = Signal()
    seen = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.review_button = QPushButton(self)
        self.review_button.clicked.connect(self.review)
        self.ok_button = QPushButton(self)
        self.ok_button.clicked.connect(self.seen)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.review_button)
        buttons.addWidget(self.ok_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        layout.addLayout(buttons)
        self.hide()
        self.retranslate()

    def tell(self, text: str) -> None:
        self.label.setText(text)
        self.review_button.setAccessibleDescription(text)
        self.ok_button.setAccessibleDescription(text)
        self.show()

    def retranslate(self) -> None:
        self.review_button.setText(self.tr("&Review…"))
        self.ok_button.setText(self.tr("OK"))

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class SyncLine(QWidget):
    """The line at the foot of Stickle Manager. Pressed, it opens the connection
    settings. Out of sight on a computer that does not sync."""

    pressed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line = ""
        self.dot = QLabel(self)
        self.dot.setPixmap(halted_pixmap(DOT_SIZE, self.devicePixelRatioF()))
        self.dot.hide()
        self.link = LinkLabel(self)
        self.link.setWordWrap(True)
        font = QFont(self.link.font())
        font.setPointSizeF(font.pointSizeF() * 0.9)
        self.link.setFont(font)
        self.link.linkActivated.connect(self._activated)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.dot)
        layout.addWidget(self.link, 1)
        self.hide()
        self.retranslate()

    def show_status(self, line: str, halted: bool) -> None:
        """line: empty for none. halted: the dot before it."""
        self._line = line
        self.setVisible(bool(line))
        self.dot.setVisible(halted)
        self._write()

    def _activated(self, _link: str) -> None:
        self.pressed.emit()

    def _write(self) -> None:
        colour = self.palette().windowText().color().name()
        style = f"color:{colour}; text-decoration:none"
        self.link.setText(f'<a href="connections" style="{style}">{html.escape(self._line)}</a>')
        self.link.setAccessibleDescription(self._line)

    def retranslate(self) -> None:
        opens = self.tr("Open the connection settings")
        self.link.setToolTip(opens)
        self.link.setAccessibleName(opens)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.PaletteChange:
            self._write()
        super().changeEvent(event)


class SyncDisplay(QObject):
    """Keeps what shows how sync stands up with it, as it goes on and off."""

    def __init__(
        self,
        tray: Tray,
        window: StickleWindow,
        manager: NoteManager,
        translations: Translations,
        parent: QObject | None = None,
        settings: Settings | None = None,
        setup: SyncSetup | None = None,
        notify: Notify | None = None,
    ) -> None:
        """settings: where telling of a stop once is kept; setup: what going on
        again with the server as kept takes; notify: tells the system once."""
        super().__init__(parent)
        self._tray = tray
        self._window = window
        self._manager = manager
        self._settings = settings
        self._setup = setup
        self._notify = notify
        self._syncing: Syncing | None = None
        self._asking: SyncStopped | None = None  # the window that asks, while open
        self._again: TurnOnSync | None = None  # going on with the server as kept, while open
        translations.changed.connect(self._show)
        window.open_standing = self.open_standing
        window.sync_stop_line.review.connect(self.open_stopped)
        window.sync_stop_line.seen.connect(self._seen)

    def follow(self, syncing: Syncing | None) -> None:
        """This run's sync from now on; None: it has stopped, or there is none."""
        if self._syncing is not None:
            self._syncing.status.changed.disconnect(self._show)
            self._syncing.unsent_changed.disconnect(self._unsent)
            self._syncing.round_shown.disconnect(self._round_shown)
        self._syncing = syncing
        if syncing is not None:
            syncing.status.changed.connect(self._show)
            syncing.unsent_changed.connect(self._unsent)
            syncing.round_shown.connect(self._round_shown)
        else:
            self._manager.show_unsent(frozenset())
        if syncing is None or syncing.status.stopped is None:
            self._forget_told()  # a stop of before is over
        asking = self._asking
        if asking is not None and not asking.leaving:  # it asked about the sync before
            asking.reject()
        if self._window.settings.isVisible():  # else as it is shown
            self._window.settings.refresh_sync()
        self._show()

    # Stopped for what a window asks about

    def open_standing(self) -> None:
        """Where the lines saying how sync stands lead: the window that asks,
        while it is stopped for that; else the connection settings."""
        if self._syncing is not None and self._syncing.status.asks is not None:
            self.open_stopped()
        else:
            self._window.open_connections()

    def open_stopped(self) -> None:
        """The window asking what to do, brought forward if it is open."""
        syncing = self._syncing
        if syncing is None or self._setup is None or syncing.status.asks is None:
            return
        if self._asking is None:
            from stickle.app.sync_stopped import SyncStopped

            self._asking = SyncStopped(syncing, self.open_again, self._setup.disconnect)
            self._asking.finished.connect(self._asked)
        self._present(self._asking)

    def _asked(self) -> None:
        self._asking = None

    def open_again(self) -> None:
        """The window that turns sync on, going from the server as kept."""
        syncing, setup = self._syncing, self._setup
        if syncing is None or setup is None:
            return
        if self._again is None:
            from stickle.app.turn_on_sync import TurnOnSync

            self._again = TurnOnSync(setup, None, syncing.kept_login())
            self._again.finished.connect(self._done_again)
        self._present(self._again)

    def _done_again(self) -> None:
        self._again = None

    @staticmethod
    def _present(window: QWidget) -> None:
        call_up(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def _get(self, setting: Setting[str | None]) -> str | None:
        return None if self._settings is None else self._settings.get(setting)

    def _set(self, setting: Setting[str | None], why: str | None) -> None:
        if self._settings is None or self._settings.get(setting) == why:
            return
        try:
            self._settings.set(setting, why)
        except apsw.Error as error:  # told again at the next start, nothing worse
            log.warning("could not keep a sync stop told: %s", type(error).__name__)

    def _forget_told(self) -> None:
        self._set(SYNC_STOP_TOLD, None)
        self._set(SYNC_STOP_SEEN, None)

    def _round_shown(self, ended: Ended) -> None:
        if ended.outcome.stopped is None and ended.wait is None:  # it went through
            self._forget_told()

    def _seen(self) -> None:
        syncing = self._syncing
        if syncing is not None:
            self._set(SYNC_STOP_SEEN, syncing.status.asks)
        self._window.sync_stop_line.hide()

    def _tell_stop(self) -> None:
        """The line at the top while stopped for what a window asks about, unless
        the user said OK to it; and, once, the system told."""
        line = self._window.sync_stop_line
        syncing = self._syncing
        why = None if syncing is None else syncing.status.asks
        if syncing is None or why is None:
            line.hide()
            return
        notice = syncing.status.notice()
        if self._get(SYNC_STOP_SEEN) == why:
            line.hide()
        else:
            line.tell(notice)
        if self._get(SYNC_STOP_TOLD) != why:
            self._set(SYNC_STOP_TOLD, why)
            if self._notify is not None and not self._window.isVisible():
                self._notify(notice, self.open_stopped)

    def _unsent(self, note_ids: Collection[str]) -> None:
        self._manager.show_unsent(note_ids)

    def _show(self) -> None:
        self._tell_stop()
        if self._syncing is None:
            self._tray.set_sync(None)
            self._window.sync_line.show_status("", False)
            return
        status = self._syncing.status
        line = status.text()
        if status.halted:
            sign = SyncSign.HALTED
        elif status.offline:
            sign = SyncSign.OFFLINE
        else:
            sign = SyncSign.ON
        self._tray.set_sync(SyncShown(line, sign, status.halted, status.update_carries_on))
        self._window.sync_line.show_status(line, status.halted)
