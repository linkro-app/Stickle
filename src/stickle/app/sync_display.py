"""How sync stands, where it is seen: the tray, a line at the foot of Stickle
Manager, and each note that has not reached the cloud.

Loaded with sync off too: nothing here brings sync, which it is handed once it
runs (stickle.app.syncing).
"""

from __future__ import annotations

import html
from collections.abc import Collection
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from stickle.app.app_icon import SyncSign, halted_pixmap
from stickle.app.link_label import LinkLabel
from stickle.app.tray import SyncShown

if TYPE_CHECKING:
    from stickle.app.i18n import Translations
    from stickle.app.notes import NoteManager
    from stickle.app.stickle_window import StickleWindow
    from stickle.app.syncing import Syncing
    from stickle.app.tray import Tray

DOT_SIZE = 12


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
    ) -> None:
        super().__init__(parent)
        self._tray = tray
        self._window = window
        self._manager = manager
        self._syncing: Syncing | None = None
        translations.changed.connect(self._show)

    def follow(self, syncing: Syncing | None) -> None:
        """This run's sync from now on; None: it has stopped, or there is none."""
        if self._syncing is not None:
            self._syncing.status.changed.disconnect(self._show)
            self._syncing.unsent_changed.disconnect(self._unsent)
        self._syncing = syncing
        if syncing is not None:
            syncing.status.changed.connect(self._show)
            syncing.unsent_changed.connect(self._unsent)
        else:
            self._manager.show_unsent(frozenset())
        self._show()

    def _unsent(self, note_ids: Collection[str]) -> None:
        self._manager.show_unsent(note_ids)

    def _show(self) -> None:
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
