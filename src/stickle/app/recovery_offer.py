"""Offering the recovery key once more to whoever skipped it at first start.

Not a reminder that comes back: it is offered a single time, when there
is something to lose, that is once five notes are stored, whether that
is at a start or when a note is written. After that only the Stickle
window's "Make a new recovery key" makes one.
"""

import logging
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication

from stickle.app.recovery_key_dialog import RecoveryKeyDialog
from stickle.data.notes import NoteRepository
from stickle.data.settings import RECOVERY_KEY_KEPT, RECOVERY_KEY_OFFERED_AGAIN, Settings

log = logging.getLogger(__name__)
NOTES_WORTH_KEEPING = 5


def _exec(dialog: RecoveryKeyDialog) -> bool:
    return dialog.exec() == RecoveryKeyDialog.DialogCode.Accepted


class RecoveryOffer:
    def __init__(
        self,
        settings: Settings,
        notes: NoteRepository,
        make_recovery_key: Callable[[], str],
        show: Callable[[RecoveryKeyDialog], bool] = _exec,
        secret_notes_on: Callable[[], bool] = lambda: False,
    ) -> None:
        self._settings = settings
        self._notes = notes
        self._make = make_recovery_key
        self._show = show
        self._secret_notes_on = secret_notes_on

    def check(self) -> None:
        """Offer it now if it is due; at most once, ever."""
        if self._settings.get(RECOVERY_KEY_KEPT) or self._settings.get(RECOVERY_KEY_OFFERED_AGAIN):
            return
        if self._secret_notes_on():
            # A new key here would not open them (their key is not at hand to seal
            # for it), while the one they are sealed for stopped working; turning
            # them on spoke of the recovery key already.
            return
        if sum(1 for note in self._notes.all() if not note.deleted) < NOTES_WORTH_KEEPING:
            return
        self._settings.set(RECOVERY_KEY_OFFERED_AGAIN, True)
        try:
            recovery_key = self._make()
        except OSError as error:
            log.error("could not make a recovery key: %s", type(error).__name__)
            return
        reason = QCoreApplication.translate(
            "RecoveryOffer",
            "Your notes are adding up. You skipped the recovery key at first start: this "
            "is a new one. Keep it now if you can; Stickle will not ask again.",
        )
        if self._show(RecoveryKeyDialog(recovery_key, reason=reason)):
            self._settings.set(RECOVERY_KEY_KEPT, True)
            log.info("recovery key kept when offered again")
        else:
            log.info("recovery key skipped again; not offered any more")
