"""What turning sync on takes of the app, and the line that leads to the guide.

Loaded with sync off too: nothing here brings sync, or what it takes to reach a
cloud. That comes with the window that turns it on (stickle.app.turn_on_sync).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from stickle.app import note_window
from stickle.app.link_label import LinkLabel
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.support import site_page
from stickle.crypto.sealed import SealedSlot
from stickle.data.sync_connection import Login

if TYPE_CHECKING:
    from stickle.app.syncing import Syncing
    from stickle.sync.engine import FirstLook

# Told as notes go by: what is being done, how many so far, of how many.
type Moving = Callable[[str, int, int], None]


@dataclass(frozen=True)
class SyncSetup:
    """What turning sync on needs, without the windows knowing the notes' keys."""

    current: Callable[[], Syncing | None]  # sync as it runs, if it does
    recovery: RecoveryKeys
    note_count: Callable[[], int]  # the user's notes, out of the trash
    pairs: Callable[[], Mapping[str, SealedSlot]]  # what a new vault's key is sealed for
    # Connected (the login; whether the vault was there before): kept, and rounds start.
    connect: Callable[[Login, bool], Syncing]
    disconnect: Callable[[], None]  # sync left: the connection forgotten, the app going on
    # What merging with the vault of a login would make, nothing kept yet; off
    # the app's thread (None: it could not be looked at). None: not looked at.
    look: Callable[[Login, Moving], FirstLook | None] | None = None
    # Connected to a vault there before, keeping only its notes: this computer's
    # go first, after a backup (if that fails, nothing is kept or let go of).
    connect_cloud_only: Callable[[Login], Syncing] | None = None


class SyncGuideLine(QWidget):
    """One sentence, and the link to the guide on the website."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sentence = QLabel(self)
        self.sentence.setWordWrap(True)
        self.link = LinkLabel(self)
        self.link.linkActivated.connect(note_window.open_link)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.sentence)
        layout.addWidget(self.link)
        self.retranslate()

    def retranslate(self) -> None:
        self.sentence.setText(
            self.tr(
                "If you get stuck, see the sync guide on the Stickle website."
                " It walks through each step and answers common questions."
            )
        )
        opened = self.tr("Open the sync guide")
        self.link.setText(f'<a href="{site_page("guide/sync")}">{opened}</a>')

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
