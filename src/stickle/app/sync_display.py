"""How sync stands, where it is seen: the tray, a line at the foot of Stickle
Manager, and each note that has not reached the cloud. Sync stopped, or its
uploads with the cloud full, is told once: a line at the top of Stickle Manager
until the user says OK and, with that window closed, one notification of the
system. So are notes put in the trash here, deleted on the other computers
while this one was away for more than a year, going back to a backup on
every computer that syncs (in the run after it only), and the secret notes
sealed for the recovery key of the computers this one syncs with, once they
opened (until then, the old one of this computer opened them). The lines that say how sync
stands, the button beside what tells of a stop and a click on its notification
lead to what is to be done: the window that asks, the server password, the new
version, or the connection settings.

Loaded with sync off too: nothing here brings sync, which it is handed once it
runs (stickle.app.syncing), nor the windows that ask, until one is wanted.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, override

import apsw
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stickle.app.app_icon import SyncSign, halted_pixmap
from stickle.app.backups import when
from stickle.app.in_use import OnTopWhileInUse, call_up
from stickle.app.link_label import LinkLabel
from stickle.app.titles import shown_title
from stickle.app.tray import SyncShown
from stickle.core.clock import moment_of
from stickle.data.settings import (
    CLOUD_FULL,
    SYNC_GONE_BACK,
    SYNC_LET_GO,
    SYNC_SECRETS_MATCHED,
    SYNC_SECRETS_RESEALED,
    SYNC_STOP_SEEN,
    SYNC_STOP_TOLD,
    SYNC_TRASHED,
    Setting,
    Settings,
)

if TYPE_CHECKING:
    from stickle.app.i18n import Translations
    from stickle.app.notes import NoteManager
    from stickle.app.stickle_window import StickleWindow
    from stickle.app.sync_setup import SyncSetup
    from stickle.app.sync_stopped import SyncStopped
    from stickle.app.syncing import Ended, Syncing
    from stickle.app.tray import Tray
    from stickle.app.turn_on_sync import TurnOnSync
    from stickle.sync.engine import LetGo

log = logging.getLogger(__name__)

DOT_SIZE = 12
# Tells the system once (what it says; what a click on it opens).
type Notify = Callable[[str, Callable[[], object]], object]


def _filled(text: str, first: str, second: str) -> str:
    """text with %1 and %2 in it as given, in one go (neither read again)."""
    return re.sub("%([12])", lambda found: first if found.group(1) == "1" else second, text)


class SyncNoticeLine(QWidget):
    """At the top of Stickle Manager: what sync did that the user should know
    of, with the way on from it if there is one, until the user says OK. The
    way on leaves it there: only OK takes it away."""

    acted = Signal()  # the way on, please
    seen = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.action_button = QPushButton(self)
        self.action_button.clicked.connect(self.acted)
        self.action_button.hide()
        self.ok_button = QPushButton(self)
        self.ok_button.clicked.connect(self.seen)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.action_button)
        buttons.addWidget(self.ok_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        layout.addLayout(buttons)
        self.hide()
        self.retranslate()

    def own_way_on(self) -> str:
        """What the button beside OK says unless told otherwise (named while hidden too)."""
        return ""

    def tell(self, text: str, way_on: str | None = None) -> None:
        """way_on: what the button beside OK says (None: as the line says it;
        empty: there is none, OK alone)."""
        shown = self.own_way_on() if way_on is None else way_on
        if shown:
            self.action_button.setText(shown)
        self.action_button.setVisible(bool(shown))
        self.action_button.setAccessibleDescription(text)
        self.ok_button.setAccessibleDescription(text)
        self.label.setText(text)
        self.show()

    def retranslate(self) -> None:
        self.ok_button.setText(self.tr("OK"))
        if self.action_button.isHidden():
            self.action_button.setText(self.own_way_on())

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class SyncStopLine(SyncNoticeLine):
    """Sync stopped, or its uploads did, and why: told with where that is dealt
    with (see SyncDisplay)."""

    @override
    def own_way_on(self) -> str:
        return self.tr("&Review…")


class SyncTrashLine(SyncNoticeLine):
    """Notes put in the trash here, deleted on the other computers while this
    one was away for more than a year: told with the way to the trash."""

    @override
    def own_way_on(self) -> str:
        return self.tr("S&how the trash")  # S is for the settings


class SyncGoneBackLine(SyncNoticeLine):
    """Every computer that syncs went back to a backup, told in the run after it
    with the way back: the notes as they were just before, without asking again."""

    @override
    def own_way_on(self) -> str:
        return self.tr("&Undo")


class SyncResealedLine(SyncNoticeLine):
    """The secret notes sealed for the recovery key of the computers this one
    syncs with: told with OK alone."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # never shown


class SyncLetGoLine(SyncNoticeLine):
    """A note made secret on another computer while it was edited here in the
    clear, what was written here let go of: told one at a time, with the way to
    its earlier versions, which keep that."""

    @override
    def own_way_on(self) -> str:
        return self.tr("Show &earlier versions…")


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
        # Notes put in the trash here to tell of (also when they could not be kept).
        self._trashed = 0 if settings is None else settings.get(SYNC_TRASHED)
        translations.changed.connect(self._show)
        window.open_standing = self.open_standing
        window.sync_stop_line.acted.connect(self.open_standing)
        window.sync_stop_line.seen.connect(self._seen)
        window.sync_trash_line.acted.connect(window.show_trash)
        window.sync_trash_line.seen.connect(self._trash_seen)
        # Going back to a backup, told in this run only: the way back from it is
        # taken without asking again, not to be pressed days after by chance.
        self._gone_back = None if settings is None else settings.get(SYNC_GONE_BACK)
        if self._gone_back is not None:
            self._set(SYNC_GONE_BACK, None)
            QTimer.singleShot(0, self._tell_gone_back_once)
        window.sync_gone_back_line.acted.connect(self._undo_going_back)
        window.sync_gone_back_line.seen.connect(self._gone_back_seen)
        self._resealed = settings is not None and settings.get(SYNC_SECRETS_RESEALED)
        window.sync_secrets_line.seen.connect(self._resealed_seen)
        self._matched = None if settings is None else settings.get(SYNC_SECRETS_MATCHED)
        window.sync_matched_line.seen.connect(self._matched_seen)
        # Notes made secret elsewhere while edited here, the first one told.
        self._let_go: list[dict[str, str | None]] = (
            [] if settings is None else list(settings.get(SYNC_LET_GO))
        )
        window.sync_let_go_line.acted.connect(self._show_let_go)
        window.sync_let_go_line.seen.connect(self._let_go_seen)

    def follow(self, syncing: Syncing | None) -> None:
        """This run's sync from now on; None: it has stopped, or there is none."""
        if self._syncing is not None:
            self._syncing.status.changed.disconnect(self._show)
            self._syncing.unsent_changed.disconnect(self._unsent)
            self._syncing.unmatched_changed.disconnect(self._manager.show_unmatched)
            self._syncing.round_shown.disconnect(self._round_shown)
        self._syncing = syncing
        if syncing is not None:
            syncing.status.changed.connect(self._show)
            syncing.unsent_changed.connect(self._unsent)
            syncing.unmatched_changed.connect(self._manager.show_unmatched)
            syncing.round_shown.connect(self._round_shown)
        else:
            self._manager.show_unsent(frozenset())
            self._manager.show_unmatched(frozenset())
        if syncing is None:
            self._forget_told()  # a stop of before is over
        elif syncing.status.stopped is None:
            # So is a stop, but not a full cloud: that is not known before a round.
            self._forget_told(but=CLOUD_FULL)
        asking = self._asking
        if asking is not None and not asking.leaving:  # it asked about the sync before
            asking.reject()
        if self._window.settings.isVisible():  # else as it is shown
            self._window.settings.refresh_sync()
        self._show()

    # Where telling of a stop leads

    def open_standing(self) -> None:
        """Where the lines saying how sync stands lead, as does the button beside
        what tells of a stop: the window that asks, while stopped for that; the
        server password (or signing in to OneDrive), while the cloud turns down
        what it was signed in with; the new version, while sync waits for one;
        else the connection settings."""
        status = None if self._syncing is None else self._syncing.status
        if status is not None and status.asks is not None:
            self.open_stopped()
        elif status is not None and status.asks_sign_in:
            self.open_again(signed_out=True)
        elif status is not None and status.update_carries_on:
            self._window.get_new_version()
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
            OnTopWhileInUse(self._asking)
        self._present(self._asking)

    def _asked(self) -> None:
        self._asking = None

    def open_again(self, signed_out: bool = False) -> None:
        """The window that turns sync on, going from the cloud as kept; signed_out:
        from a password typed anew (or signing in anew), what was kept having
        been turned down."""
        syncing, setup = self._syncing, self._setup
        if syncing is None or setup is None:
            return
        if self._again is None:
            from stickle.app.turn_on_sync import TurnOnSync

            self._again = TurnOnSync(setup, None, syncing.kept_login(), signed_out)
            self._again.finished.connect(self._done_again)
            OnTopWhileInUse(self._again)
        self._present(self._again)

    def _done_again(self) -> None:
        self._again = None

    @staticmethod
    def _present(window: QWidget) -> None:
        # Above pinned notes until the user moves on from it (to a browser
        # signing in, say): it has an OnTopWhileInUse.
        call_up(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def _get(self, setting: Setting[str | None]) -> str | None:
        return None if self._settings is None else self._settings.get(setting)

    def _set[T](self, setting: Setting[T], value: T) -> None:
        if self._settings is None or self._settings.get(setting) == value:
            return
        try:
            self._settings.set(setting, value)
        except apsw.Error as error:  # told again at the next start, nothing worse
            log.warning("could not keep what sync told: %s", type(error).__name__)

    def _forget_told(self, but: str | None = None) -> None:
        """A stop told of is over: the next one is told (but: unless it is that one)."""
        for setting in (SYNC_STOP_TOLD, SYNC_STOP_SEEN):
            if self._get(setting) != but:
                self._set(setting, None)

    def _round_shown(self, ended: Ended) -> None:
        syncing = self._syncing
        if syncing is not None and ended.outcome.stopped is None and ended.wait is None:
            self._forget_told(but=syncing.status.told)  # through: still full, perhaps
        trashed = len(set(ended.report.trashed))
        if trashed and not ended.taken_in:  # that, the user saw before letting it in
            self._more_trashed(trashed)
        if ended.report.let_go:
            self._more_let_go(ended.report.let_go)

    def _seen(self) -> None:
        syncing = self._syncing
        if syncing is not None:
            self._set(SYNC_STOP_SEEN, syncing.status.told)
        self._window.sync_stop_line.hide()
        self._window.set_update_told(False)

    def _tell_stop(self) -> None:
        """The line at the top while sync is stopped, or its uploads are, unless
        the user said OK to it; and, once, the system told."""
        line = self._window.sync_stop_line
        syncing = self._syncing
        why = None if syncing is None else syncing.status.told
        if syncing is None or why is None:
            line.hide()
            self._window.set_update_told(False)
            return
        status = syncing.status
        notice = status.notice()
        anew = self._get(SYNC_STOP_TOLD) != why
        if anew:  # OK said to what was told before is not said to this
            self._set(SYNC_STOP_TOLD, why)
            self._set(SYNC_STOP_SEEN, None)
        if self._get(SYNC_STOP_SEEN) == why:
            line.hide()
        else:
            line.tell(notice, status.way_on())
        self._window.set_update_told(not line.isHidden() and status.update_carries_on)
        if anew and self._notify is not None and not self._window.isVisible():
            self._notify(notice, self.open_standing)

    # Notes put in the trash, deleted elsewhere while this computer was away

    def _trashed_text(self) -> str:
        return self.tr(
            "While this computer wasn't syncing, %Ln note(s) were deleted on your other"
            " computers, so they're in the trash here too.",
            "",
            self._trashed,
        )

    def _more_trashed(self, count: int) -> None:
        """Told at the top until OK, counted on until then; the system told once."""
        first = not self._trashed
        self._trashed += count
        self._set(SYNC_TRASHED, self._trashed)
        self._tell_trashed()
        if first and self._notify is not None and not self._window.isVisible():
            self._notify(self._trashed_text(), self._window.show_trash)

    def _trash_seen(self) -> None:
        self._trashed = 0
        self._set(SYNC_TRASHED, 0)
        self._tell_trashed()

    def _tell_trashed(self) -> None:
        line = self._window.sync_trash_line
        if self._trashed:
            line.tell(self._trashed_text())
        else:
            line.hide()

    # Going back to a backup on every computer that syncs

    def _gone_back_text(self) -> str:
        to = "" if self._gone_back is None else self._gone_back["to"]
        return self.tr("Every computer that syncs went back to the backup from %1.").replace(
            "%1", when(moment_of(to)) if to else ""
        )

    def _tell_gone_back_once(self) -> None:
        self._tell_gone_back()
        if self._notify is not None and not self._window.isVisible():
            self._notify(self._gone_back_text(), self._window.open)

    def _tell_gone_back(self) -> None:
        line = self._window.sync_gone_back_line
        if self._gone_back is not None:
            line.tell(self._gone_back_text())
        else:
            line.hide()

    def _undo_going_back(self) -> None:
        if self._gone_back is not None:
            self._window.undo_going_back(self._gone_back["before"])

    def _gone_back_seen(self) -> None:
        self._gone_back = None
        self._tell_gone_back()

    # The secret notes sealed for the recovery key of the computers this one syncs with

    def _resealed_text(self) -> str:
        return self.tr(
            "This computer's secret notes now open with the same recovery key as your other"
            " computers too. You can throw the old one away now."
        )

    def secrets_resealed(self) -> None:
        """Told at the top until OK; the system told once, with the window closed."""
        self._resealed = True
        self._set(SYNC_SECRETS_RESEALED, True)
        self._tell_resealed()
        if self._notify is not None and not self._window.isVisible():
            self._notify(self._resealed_text(), self._window.open)

    def _resealed_seen(self) -> None:
        self._resealed = False
        self._set(SYNC_SECRETS_RESEALED, False)
        self._tell_resealed()

    def _tell_resealed(self) -> None:
        line = self._window.sync_secrets_line
        if self._resealed:
            line.tell(self._resealed_text(), "")
        else:
            line.hide()

    # The secret notes' password matched with the others'

    def _matched_text(self) -> str:
        matched = self._matched or {}
        count = matched.get("notes")
        text = self.tr(
            "%Ln secret note(s) here now use the same password as your other computers.",
            "",
            count if isinstance(count, int) else 0,
        )
        if matched.get("old_key") is True:
            text += " " + self.tr("You can throw the old recovery key away now.")
        return text

    def secrets_matched(self, count: int, old_key: bool) -> None:
        """Told at the top until OK; the system told once, with the window closed.
        old_key: the old recovery key opens none of this computer's secret notes now."""
        matched: dict[str, object] = {"notes": count, "old_key": old_key}
        self._matched = matched
        self._set(SYNC_SECRETS_MATCHED, self._matched)
        self._tell_matched()
        if self._notify is not None and not self._window.isVisible():
            self._notify(self._matched_text(), self._window.open)

    def _matched_seen(self) -> None:
        self._matched = None
        self._set(SYNC_SECRETS_MATCHED, None)
        self._tell_matched()

    def _tell_matched(self) -> None:
        line = self._window.sync_matched_line
        if self._matched is not None:
            line.tell(self._matched_text(), "")
        else:
            line.hide()

    # A note made secret elsewhere while it was edited here

    def _let_go_text(self) -> str | None:
        """What is said of the first note told of, while it is here (those gone
        since are let go of); None when there is none."""
        while self._let_go:
            first = self._let_go[0]
            note = self._manager.note(str(first["note"]))
            if note is not None and not note.deleted:
                by = first["by"] or self.tr("another computer")
                title = shown_title(note) or self.tr("Note")
                said = self.tr(
                    "%1 made “%2” a secret note, so what you changed here wasn't put into it."
                    " It stays in Earlier versions on this computer for 30 days."
                )
                return _filled(said, by, title)
            self._let_go.pop(0)
            self._set(SYNC_LET_GO, self._let_go)
        return None

    def _more_let_go(self, told: Sequence[LetGo]) -> None:
        """Told at the top, one at a time, each until OK; the system told once."""
        first = not self._let_go
        for each in told:
            self._let_go = [kept for kept in self._let_go if kept["note"] != each.note]
            self._let_go.append({"note": each.note, "by": each.by})
        self._set(SYNC_LET_GO, self._let_go)
        self._tell_let_go()
        text = self._let_go_text()
        if first and text is not None and self._notify is not None and not self._window.isVisible():
            self._notify(text, self._show_let_go)

    def _show_let_go(self) -> None:
        """The earlier versions of the note told of (it stays told till OK)."""
        if self._let_go:
            self._manager.show_versions_of(str(self._let_go[0]["note"]))

    def _let_go_seen(self) -> None:
        if self._let_go:
            self._let_go.pop(0)
            self._set(SYNC_LET_GO, self._let_go)
        self._tell_let_go()

    def _tell_let_go(self) -> None:
        line = self._window.sync_let_go_line
        text = self._let_go_text()
        if text is not None:
            line.tell(text)
        else:
            line.hide()

    def _unsent(self, note_ids: Collection[str]) -> None:
        self._manager.show_unsent(note_ids)

    def _show(self) -> None:
        self._tell_stop()
        self._tell_trashed()
        self._tell_gone_back()
        self._tell_resealed()
        self._tell_matched()
        self._tell_let_go()
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
