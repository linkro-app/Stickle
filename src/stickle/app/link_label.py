"""Links in a label that the keyboard reaches one at a time, as it does buttons."""

from typing import override

from PySide6.QtCore import Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QFrame, QLabel, QWidget


class LinkLabel(QLabel):
    """Links reached with Tab and Shift+Tab and opened with Enter, as well as by a click.

    Qt's own label stops once more with no link chosen, where Enter pressed the
    window's default button instead (Close, in About Stickle), and Shift+Tab
    could not leave it past its first link.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

    @override
    def focusInEvent(self, ev: QFocusEvent) -> None:
        super().focusInEvent(ev)
        # Arrived at by Tab: its first link chosen at once; by Shift+Tab, its last.
        if ev.reason() == Qt.FocusReason.TabFocusReason:
            self.focusNextPrevChild(True)
        elif ev.reason() == Qt.FocusReason.BacktabFocusReason:
            self.focusNextPrevChild(False)

    @override
    def focusNextPrevChild(self, next: bool) -> bool:
        chosen = self.selectionStart()
        moved = super().focusNextPrevChild(next)
        if not self.hasFocus() or self.selectionStart() != chosen:
            return moved
        # No link further that way: on to the rest of the window.
        return QFrame.focusNextPrevChild(self, next)
