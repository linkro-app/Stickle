"""The Connections page with sync on: how it stands, how often it looks, the
computers that sync, and the way to leave.

Imported only when this device syncs."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, override
from urllib.parse import urlsplit

import apsw
from PySide6.QtCore import QDate, QDateTime, QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.background import Job
from stickle.app.times import day_text
from stickle.core.updates import FEED_HOST
from stickle.data.settings import DEVICE_NAME_LONGEST, SYNC_EVERY_CHOICES
from stickle.sync.devices import Other
from stickle.sync.outcome import Stopped

if TYPE_CHECKING:
    from stickle.app.syncing import Ended, Syncing

log = logging.getLogger(__name__)

ROWS_SHOWN = 4  # computers in sight at once; the others by scrolling
# A round asked for that does not end in this long gives the button back all the
# same: something went wrong that nothing will tell of.
ASKED_FOR_AT_MOST_MS = 2 * 60 * 1000


def _filled(pattern: str, *values: str) -> str:
    """%1, %2... of a translated pattern: in one go, as a value may hold such a mark."""
    return re.sub(r"%(\d)", lambda found: values[int(found[1]) - 1], pattern)


def _wrapped(parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _small(parent: QWidget) -> QLabel:
    label = _wrapped(parent)
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * 0.9)
    label.setFont(font)
    return label


def _no_gap(layout: QVBoxLayout | QHBoxLayout) -> None:
    layout.setContentsMargins(0, 0, 0, 0)


def server_of(address: str) -> str:
    """The server of an address as kept: its name, and its port if it has one."""
    return urlsplit(address).netloc


def on_one_line(name: str) -> str:
    """As another computer called itself, whatever is in it: a line of a list."""
    return " ".join(name.split())


class ChangeName(QDialog):
    """What the other computers call this one."""

    def __init__(self, name: str, system_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sentence = _wrapped(self)
        self.name = QLineEdit(name, self)
        self.name.setMaxLength(DEVICE_NAME_LONGEST)
        self.name.setPlaceholderText(system_name)
        self.ok_button = QPushButton(self)
        self.cancel_button = QPushButton(self)
        self.ok_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.sentence)
        layout.addWidget(self.name)
        layout.addLayout(buttons)
        self.setMinimumWidth(380)
        self.retranslate()
        self.name.setFocus()
        self.name.selectAll()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Change display name"))
        self.sentence.setText(
            self.tr(
                "The name your other computers show for this one."
                " It doesn't rename the computer itself."
            )
        )
        self.name.setAccessibleName(self.tr("Display name"))
        self.ok_button.setText(self.tr("OK"))
        self.cancel_button.setText(self.tr("Cancel"))

    def chosen(self) -> str:
        return self.name.text().strip()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class DisconnectSync(QDialog):
    """Sure? Then sync ends here, and what it takes to reach the cloud goes with it."""

    def __init__(
        self, syncing: Syncing, disconnected: Callable[[], None], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._syncing = syncing
        self._disconnected = disconnected
        self._leaving = False
        self.turns_off = _wrapped(self)
        self.folder_stays = _wrapped(self)
        self.password_goes = _wrapped(self)
        self.message = _wrapped(self)
        self.message.hide()
        self.disconnect_button = QPushButton(self)
        self.cancel_button = QPushButton(self)
        # Nothing here happens by a press of Enter.
        self.cancel_button.setDefault(True)
        self.disconnect_button.setAutoDefault(False)
        self.disconnect_button.clicked.connect(self._leave)
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        for label in (self.turns_off, self.folder_stays, self.password_goes, self.message):
            layout.addWidget(label)
        layout.addLayout(buttons)
        self.setMinimumWidth(420)
        self.retranslate()
        self.cancel_button.setFocus()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Disconnect"))
        self.turns_off.setText(
            self.tr("This turns off sync on this computer. Your notes stay on this computer.")
        )
        self.folder_stays.setText(
            self.tr(
                "The Stickle folder in %1 isn't deleted, so your other computers keep syncing."
            ).replace("%1", self._syncing.cloud_name)
        )
        self.password_goes.setText(
            self.tr("The server password saved on this computer is deleted too.")
        )
        self.message.setText(self.tr("Disconnecting…"))
        self.disconnect_button.setText(self.tr("Disconnect"))
        self.cancel_button.setText(self.tr("Cancel"))

    def _leave(self) -> None:
        self._leaving = True
        self.disconnect_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.message.show()
        self._syncing.leaving()
        Job(self._syncing.leave, self._left)

    def _left(self, outcome: object) -> None:
        if isinstance(outcome, Exception):
            log.warning("sync: leaving did not end cleanly: %s", type(outcome).__name__)
        try:
            self._disconnected()
        except apsw.Error as error:  # as it was: on again at the next start
            log.error("sync: could not forget the connection: %s", type(error).__name__)
        self._leaving = False
        self.accept()

    @override
    def reject(self) -> None:
        if not self._leaving:
            super().reject()

    @override
    def done(self, result: int, /) -> None:
        super().done(result)
        self.deleteLater()  # used once

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class SyncPanel(QWidget):
    # Told, rather than holding on to whoever shows it: a window kept alive by
    # something it owns would be deleted in the middle of deleting that.
    disconnected = Signal()
    # Stopped for what a window asks about, the password or an update: where that
    # is dealt with, please.
    review = Signal()

    def __init__(
        self, syncing: Syncing, disconnect: Callable[[], None], parent: QWidget | None = None
    ) -> None:
        """disconnect: sync left, the connection forgotten and the app going on."""
        super().__init__(parent)
        self.syncing = syncing
        self._disconnect_with = disconnect
        self._handed_to: QWidget | None = None  # the key focus, given up by the button
        self._asked = QTimer(self)
        self._asked.setSingleShot(True)
        self._asked.setInterval(ASKED_FOR_AT_MOST_MS)
        self._asked.timeout.connect(self._ready)

        self.account = _wrapped(self)
        self.status = _wrapped(self)
        self.review_button = QPushButton(self)
        self.review_button.clicked.connect(self.review)
        self.sync_now_button = QPushButton(self)
        self.sync_now_button.clicked.connect(self._sync_now)
        self.every_label = QLabel(self)
        self.every_box = QComboBox(self)
        self.every_label.setBuddy(self.every_box)
        for seconds in SYNC_EVERY_CHOICES:
            self.every_box.addItem("", seconds)
        self.every_box.setCurrentIndex(self.every_box.findData(syncing.looks_every()))
        self.every_box.activated.connect(self._every_chosen)
        self.every_note = _small(self)
        self.computers_title = _wrapped(self)
        self.computers = QListWidget(self)
        self.computers.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.computers.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.computers.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rename_button = QPushButton(self)
        self.rename_button.clicked.connect(self._rename)
        self.disconnect_button = QPushButton(self)
        self.disconnect_button.clicked.connect(self._disconnect)
        for button in (
            self.review_button,
            self.sync_now_button,
            self.rename_button,
            self.disconnect_button,
        ):
            button.setAutoDefault(False)

        standing = QHBoxLayout()
        _no_gap(standing)
        standing.addWidget(self.status, 1)
        standing.addWidget(self.review_button)
        standing.addWidget(self.sync_now_button)
        every = QHBoxLayout()
        _no_gap(every)
        every.addWidget(self.every_label)
        every.addWidget(self.every_box)
        every.addStretch(1)
        actions = QHBoxLayout()
        _no_gap(actions)
        actions.addWidget(self.rename_button)
        actions.addStretch(1)
        actions.addWidget(self.disconnect_button)
        layout = QVBoxLayout(self)
        _no_gap(layout)
        layout.addWidget(self.account)
        layout.addLayout(standing)
        layout.addLayout(every)
        layout.addWidget(self.every_note)
        layout.addWidget(self.computers_title)
        layout.addWidget(self.computers)
        layout.addLayout(actions)

        syncing.status.changed.connect(self._show_status)
        syncing.round_shown.connect(self._round_shown)
        self.retranslate()

    def tab_order(self) -> list[QWidget]:
        return [
            self.review_button,
            self.sync_now_button,
            self.every_box,
            self.computers,
            self.rename_button,
            self.disconnect_button,
        ]

    def connects_to(self, update_check: bool) -> str:
        """Where the app connects, line by line."""
        lines = [
            self.tr("Stickle connects to:"),
            self.tr("· %1 — to sync with WebDAV").replace(
                "%1", server_of(self.syncing.login.address)
            ),
        ]
        if update_check:
            lines.append(
                self.tr(
                    "· %1 — a daily check for a new version (you can turn it off in General)"
                ).replace("%1", FEED_HOST)
            )
        return "\n".join(lines)

    def retranslate(self) -> None:
        login = self.syncing.login
        self.account.setText(
            f"{self.syncing.cloud_name} · {server_of(login.address)} · {login.user}"
        )
        self.review_button.setText(self.tr("&Review…"))  # named while hidden too
        self.sync_now_button.setText(self.tr("&Sync now"))
        self.every_label.setText(self.tr("&Get changes from other computers:"))
        labels = (
            self.tr("Every 30 seconds"),
            self.tr("Every minute"),
            self.tr("Every 5 minutes"),
            self.tr("Every 15 minutes"),
            self.tr("Every 30 minutes"),
            self.tr("Every hour"),
        )
        for index, label in enumerate(labels):
            self.every_box.setItemText(index, label)
        self.every_note.setText(
            self.tr("Shorter is faster but uses a little more battery on a laptop.")
        )
        self.computers_title.setText(self.tr("Computers that sync"))
        self.computers.setAccessibleName(self.tr("Computers that sync"))
        self.rename_button.setText(self.tr("C&hange display name…"))
        self.disconnect_button.setText(self.tr("&Disconnect…"))
        self._show_status()
        self._show_computers()

    def _show_status(self) -> None:
        status = self.syncing.status
        text = status.text()
        self.status.setText(text)
        self.status.setAccessibleName(text)
        # Not to this page: Sync now is beside it, for once the permissions are fixed.
        way_on = "" if status.stopped is Stopped.NO_ACCESS else status.way_on()
        if way_on:
            self.review_button.setText(way_on)
        self.review_button.setVisible(bool(way_on))
        self.review_button.setAccessibleDescription(text)

    def _line(self, other: Other) -> str:
        name = on_one_line(other.name)
        if other.synced is None:
            return _filled(self.tr("%1 — %2 · Stickle %3"), name, other.os, other.version)
        day = QDateTime.fromMSecsSinceEpoch(other.synced).date()
        when = self.tr("today") if day == QDate.currentDate() else day_text(day)
        return _filled(
            self.tr("%1 — %2 · Stickle %3 · last synced %4"), name, other.os, other.version, when
        )

    def _show_computers(self) -> None:
        here = self.tr("This computer — %1").replace("%1", self.syncing.this_name())
        self.computers.clear()
        self.computers.addItems([here, *(self._line(each) for each in self.syncing.others())])
        rows = min(self.computers.count(), ROWS_SHOWN)
        self.computers.setFixedHeight(
            self.computers.sizeHintForRow(0) * rows + 2 * self.computers.frameWidth()
        )

    def _sync_now(self) -> None:
        had_focus = self.sync_now_button.hasFocus()
        self.sync_now_button.setEnabled(False)
        self._handed_to = self.window().focusWidget() if had_focus else None
        self._asked.start()
        self.syncing.sync_now()

    def _ready(self) -> None:
        self._asked.stop()
        self.sync_now_button.setEnabled(True)
        if self._handed_to is not None and self.window().focusWidget() is self._handed_to:
            self.sync_now_button.setFocus()
        self._handed_to = None

    def _round_shown(self, _ended: Ended) -> None:
        self._ready()
        self._show_computers()

    def _every_chosen(self, index: int) -> None:
        self.syncing.set_looks_every(int(self.every_box.itemData(index)))

    def _rename(self) -> None:
        window = ChangeName(self.syncing.chosen_name(), self.syncing.system_name(), self.window())
        window.accepted.connect(lambda: self._renamed(window.chosen()))
        window.finished.connect(window.deleteLater)
        window.open()

    def _renamed(self, name: str) -> None:
        self.syncing.rename(name)
        self._show_computers()

    def _disconnect(self) -> None:
        window = DisconnectSync(self.syncing, self._disconnect_with, self.window())
        window.accepted.connect(self.disconnected)
        window.open()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
