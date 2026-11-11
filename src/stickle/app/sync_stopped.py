"""The window that asks what to do while sync is stopped for it: a big change
from another computer, the Stickle folder gone from the cloud or made anew
there, or the server's certificate changed.

It never opens by itself: only from the line that tells of the stop once and
the lines that say how sync stands (it is stopped, so nothing is lost by
waiting, and a window popping up would take the keyboard from whatever is
being typed). Nothing changes here or in the cloud until one of its buttons
says so; closed, sync stays stopped. Pressed, it waits for what comes of it,
and closes once sync goes on. Going on with the folder or the certificate as
it is now is done by the window that turns sync on, from the server as kept.

Imported only when this device syncs, and only once the window is asked for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.background import Job
from stickle.app.error_text import error_style
from stickle.app.sync_status import ASKING
from stickle.app.syncing import BigChange
from stickle.sync.outcome import Stopped

if TYPE_CHECKING:
    from stickle.app.syncing import Ended, Syncing

log = logging.getLogger(__name__)

WIDTH = 480
ROWS_SHOWN = 5  # first lines in sight at once in an unfolded list; the others by scrolling


def _wrapped(parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class _Folded(QWidget):
    """A count that unfolds into the first line of each note it counts."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.toggle = QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.toggled.connect(self._unfolded)
        self.lines = QListWidget(self)
        self.lines.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.lines.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.lines.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lines.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.lines)

    def show_lines(self, count: str, lines: list[tuple[str, bool]]) -> None:
        """count: what the button says; lines: each first line, and whether it
        stands for one (a secret note, an empty one) rather than being one."""
        self.toggle.setText(count)
        self.toggle.setAccessibleName(count)
        self.lines.setAccessibleName(count)
        self.lines.clear()
        for text, standing_for in lines:
            item = QListWidgetItem(text)
            if standing_for:
                font = QFont(self.lines.font())
                font.setItalic(True)
                item.setFont(font)
            self.lines.addItem(item)
        rows = min(len(lines), ROWS_SHOWN)
        if rows:
            self.lines.setFixedHeight(
                self.lines.sizeHintForRow(0) * rows + 2 * self.lines.frameWidth()
            )
        self.setVisible(bool(lines))

    def _unfolded(self, unfolded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if unfolded else Qt.ArrowType.RightArrow)
        self.lines.setVisible(unfolded)
        window = self.window()
        if isinstance(window, SyncStopped):
            window.fit()


class SyncStopped(QDialog):
    def __init__(
        self,
        syncing: Syncing,
        again: Callable[[], None],
        disconnect: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        """again: the window that turns sync on, from the server as kept;
        disconnect: sync left, the connection forgotten and the app going on."""
        super().__init__(parent)
        self._syncing = syncing
        self._again = again
        self._disconnect = disconnect
        self._why: Stopped | None = None
        self._taking = False  # the big change let in: waiting for the round
        self.leaving = False  # disconnecting: waiting for sync to end
        self._said: Callable[[], str] = lambda: ""
        self._failed = False

        self.text = _wrapped(self)
        self.trash = _Folded(self)
        self.gone = _Folded(self)
        self.copies = _Folded(self)
        self.after = _wrapped(self)
        self.until = _wrapped(self)
        self.message = _wrapped(self)
        self.message.hide()
        self.first_button = QPushButton(self)
        self.second_button = QPushButton(self)
        self.first_button.clicked.connect(self._first)
        self.second_button.clicked.connect(self._second)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.first_button)
        buttons.addWidget(self.second_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        for folded in (self.trash, self.gone, self.copies):
            layout.addWidget(folded)
        layout.addWidget(self.after)
        layout.addWidget(self.until)
        layout.addWidget(self.message)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self.setMinimumWidth(WIDTH)
        self.resize(WIDTH, self.height())

        syncing.status.changed.connect(self._follow)
        syncing.round_shown.connect(self._round_shown)
        self._watching = True
        self._show_why(syncing.status.stopped)

    # What it says

    @property
    def why(self) -> Stopped | None:
        """What it asks about."""
        return self._why

    def _show_why(self, why: Stopped | None) -> None:
        self._why = why
        self._clear_message()
        self.retranslate()
        # Nothing that changes the notes is one Enter away: for a big change Enter
        # is "Not now" (unless the other button is the one with the keyboard),
        # and the others' first button opens one more window.
        big = why is Stopped.BIG_CHANGE
        self.first_button.setDefault(not big)
        self.second_button.setDefault(big)
        if big:
            shown = [f for f in (self.trash, self.gone, self.copies) if not f.isHidden()]
            focus: QWidget = shown[0].toggle if shown else self.second_button
        else:
            focus = self.first_button
        focus.setFocus()
        self.fit()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Sync stopped"))
        cloud = self._syncing.cloud_name
        big = self._why is Stopped.BIG_CHANGE
        # Nothing to unfold but for a big change (named all the same).
        self._show_big_change(self._syncing.big_change() if big else BigChange([], [], []))
        self.until.setVisible(big)
        disconnect = self.tr("&Disconnect")
        match self._why:
            case Stopped.BIG_CHANGE:
                self.text.setText(
                    self.tr(
                        "Another computer removed or changed many notes at once, so sync"
                        " stopped before bringing that in. The notes on this computer are"
                        " untouched, and you can keep using them."
                    )
                )
                self.after.setText(
                    self.tr(
                        "A backup is made first. Afterwards you can still get notes back from"
                        " the trash or the backup."
                    )
                )
                self.until.setText(
                    self.tr("Until then, changes on this computer won't upload either.")
                )
                first, second = self.tr("&Back up and bring in"), self.tr("&Not now")
            case Stopped.VAULT_GONE:
                self.text.setText(
                    self.tr(
                        "The Stickle folder is gone from %1. The notes on this computer are"
                        " untouched."
                    ).replace("%1", cloud)
                )
                self.after.setText(
                    self.tr(
                        "If you deleted it on purpose, disconnect. If you make it again, your"
                        " other computers need the recovery key or spare password once to"
                        " keep syncing."
                    )
                )
                first, second = self.tr("&Make it again from this computer's notes…"), disconnect
            case Stopped.VAULT_CHANGED:
                self.text.setText(
                    self.tr(
                        "The Stickle folder in %1 was made anew, perhaps by another of your"
                        " computers."
                    ).replace("%1", cloud)
                )
                self.after.setText(
                    self.tr("To keep syncing, enter the recovery key or spare password once.")
                )
                first, second = self.tr("&Sync with the new folder…"), disconnect
            case _:
                self.text.setText(
                    self.tr(
                        "The server's certificate has changed. If you changed it on the server"
                        " yourself, there's nothing to worry about."
                    )
                )
                self.after.setText(
                    self.tr(
                        "If you don't know why, check that the new fingerprint matches the one"
                        " on the server's admin page. If it doesn't, someone may be"
                        " intercepting the connection."
                    )
                )
                first, second = self.tr("&View the new certificate…"), disconnect
        self.first_button.setText(first)
        self.second_button.setText(second)
        if self.message.isVisible():
            self.message.setText(self._said())

    def _show_big_change(self, change: BigChange) -> None:
        def lines(titles: list[str | None]) -> list[tuple[str, bool]]:
            shown: list[tuple[str, bool]] = []
            for title in titles:
                if title is None:
                    shown.append((self.tr("Secret note"), True))
                else:
                    shown.append((title or self.tr("Empty note"), not title))
            return shown

        self.trash.show_lines(
            self.tr("%Ln note(s) would go to the trash", "", len(change.trash)),
            lines(change.trash),
        )
        self.gone.show_lines(
            self.tr("%Ln note(s) would be deleted for good", "", len(change.gone)),
            lines(change.gone),
        )
        self.copies.show_lines(
            self.tr("%Ln new conflict copies", "", len(change.copies)), lines(change.copies)
        )

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _say(self, said: Callable[[], str], failed: bool = False) -> None:
        self._said, self._failed = said, failed
        self.message.setStyleSheet(error_style(self.message) if failed else "")
        self.message.setText(said())
        self.message.show()
        self.first_button.setAccessibleDescription(said() if failed else "")
        self.fit()

    def _clear_message(self) -> None:
        self._said = lambda: ""
        self.message.clear()
        self.message.hide()
        self.first_button.setAccessibleDescription("")

    def _busy(self, busy: bool) -> None:
        self.first_button.setEnabled(not busy)
        self.second_button.setEnabled(not busy)

    def fit(self) -> None:
        """As tall as what is on it now: wrapped text is not told to the window."""
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()
        wanted = layout.totalHeightForWidth(self.width())
        self.resize(self.width(), wanted if wanted > 0 else layout.totalSizeHint().height())

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.fit()

    # What the buttons do

    def _first(self) -> None:
        if self._why is Stopped.BIG_CHANGE:
            self._taking = True
            self._busy(True)
            self._say(lambda: self.tr("Backing up, then bringing it in…"))
            self._syncing.take_big_change()
            return
        self.accept()
        self._again()

    def _second(self) -> None:
        if self._why is Stopped.BIG_CHANGE:
            self.reject()  # not now: stopped as it is
            return
        self.leaving = True
        self._busy(True)
        self._say(lambda: self.tr("Disconnecting…"))
        self._syncing.leaving()
        Job(self._syncing.leave, self._left)

    def _left(self, outcome: object) -> None:
        if isinstance(outcome, Exception):
            log.warning("sync: leaving did not end cleanly: %s", type(outcome).__name__)
        try:
            self._disconnect()
        except Exception as error:  # as it was: on again at the next start
            log.error("sync: could not forget the connection: %s", type(error).__name__)
        self.leaving = False
        self.accept()

    # What comes of it

    def _follow(self) -> None:
        """Sync went on, or stopped for something else: closed, or asking that."""
        if self.leaving or self._taking:
            return  # what the button did is waited for
        why = self._syncing.status.stopped
        if why not in ASKING:
            self.reject()
        elif why is not self._why:
            self._show_why(why)

    def _round_shown(self, ended: Ended) -> None:
        if not self._taking:
            return
        self._taking = False
        self._busy(False)
        why = ended.outcome.stopped
        if ended.backup_failed:
            self._say(
                lambda: self.tr(
                    "Couldn't make the backup, so nothing was brought in. Check that the disk"
                    " has room, then try again."
                ),
                failed=True,
            )
        elif why is Stopped.BIG_CHANGE:
            self._show_why(why)
            self._say(
                lambda: self.tr(
                    "More changed on the other computer meanwhile, so sync stopped again."
                )
            )
        else:
            self._follow()

    @override
    def reject(self) -> None:
        if not self.leaving:
            super().reject()

    @override
    def done(self, result: int, /) -> None:
        if self._watching:
            self._watching = False
            self._syncing.status.changed.disconnect(self._follow)
            self._syncing.round_shown.disconnect(self._round_shown)
        super().done(result)
        self.deleteLater()  # used once
