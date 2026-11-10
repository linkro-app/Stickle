"""The band at the top of a conflict copy: one of two texts written apart on two
computers, kept beside the note (its original) that kept the other. It says
where and when this text was written, with [Show the original], [Use this
text] and [Keep both]; too narrow for the three, [Resolve] offers them.

Tab reaches it from the formatted note and goes back there from its last
button, so that the keyboard can deal with the copy as the mouse can (the
note menu offers the three too).
"""

from dataclasses import dataclass
from typing import override

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stickle.app.reminder_bar import ARROW_WIDTH, wide_arrow_style
from stickle.app.times import local_time
from stickle.core.clock import stamp_at


@dataclass(frozen=True)
class CopyOf:
    """A conflict copy as its band shows it."""

    here: bool  # its text was written on this computer
    name: str | None  # else the computer's, as it calls itself (None: not known)
    edited: int  # when, in milliseconds
    original: bool  # its original is here, out of the trash: it can be shown
    swappable: bool  # and its text can go in it (neither is a secret note)


def written(copy: CopyOf) -> str:
    """Where and when the copy's text was written."""
    when = local_time(stamp_at(copy.edited))
    if copy.here:
        line = QCoreApplication.translate("ConflictBar", "Changed separately on this computer · %1")
    elif copy.name is None:
        line = QCoreApplication.translate(
            "ConflictBar", "Changed separately on another computer · %1"
        )
    else:
        line = QCoreApplication.translate("ConflictBar", "Changed separately on %1 · %2")
        return line.replace("%2", when).replace("%1", copy.name)
    return line.replace("%1", when)


def explained(copy: CopyOf) -> str:
    """What a conflict copy is, for its tooltip and screen readers."""
    if copy.here:
        return QCoreApplication.translate(
            "ConflictBar",
            "Conflict copy: the same note was changed on two computers, so the text"
            " from this computer was kept in this copy.",
        )
    if copy.name is None:
        return QCoreApplication.translate(
            "ConflictBar",
            "Conflict copy: the same note was changed on two computers, so the text"
            " from another computer was kept in this copy.",
        )
    return QCoreApplication.translate(
        "ConflictBar",
        "Conflict copy: the same note was changed on two computers, so the text"
        " from %1 was kept in this copy.",
    ).replace("%1", copy.name)


class ConflictBar(QFrame):
    original_requested = Signal()
    use_requested = Signal()
    keep_requested = Signal()
    left = Signal()  # Tab went past its last button (or Shift+Tab before its first)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("conflictBar")
        self.copy: CopyOf | None = None
        self._wanted: dict[QPushButton, bool] = {}  # which buttons apply, by _show_choices
        self.line = QLabel(self)
        self.line.setWordWrap(True)
        self.line.setTextFormat(Qt.TextFormat.PlainText)  # a computer's name, as it gives it
        self.original_button = QPushButton(self)
        self.use_button = QPushButton(self)
        self.keep_button = QPushButton(self)
        # Enter presses them too, as Space does (a note is not a dialog).
        for button in (self.original_button, self.use_button, self.keep_button):
            button.setAutoDefault(True)
        self.original_button.clicked.connect(self.original_requested)
        self.use_button.clicked.connect(self.use_requested)
        self.keep_button.clicked.connect(self.keep_requested)
        # The three in one, where they do not fit side by side.
        self.resolve_button = QToolButton(self)
        self.resolve_button.setStyle(wide_arrow_style())
        self.resolve_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.resolve_menu = QMenu(self)
        self.original_action = self.resolve_menu.addAction("")
        self.original_action.triggered.connect(self.original_requested)
        self.use_action = self.resolve_menu.addAction("")
        self.use_action.triggered.connect(self.use_requested)
        self.keep_action = self.resolve_menu.addAction("")
        self.keep_action.triggered.connect(self.keep_requested)
        self.resolve_button.setMenu(self.resolve_menu)
        self.resolve_button.hide()
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(4)
        for button in (self.original_button, self.use_button, self.keep_button):
            self.buttons.addWidget(button)
        self.buttons.addWidget(self.resolve_button)
        self.buttons.addStretch()
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(8, 3, 6, 4)
        self._outer.setSpacing(3)
        self._outer.addWidget(self.line)
        self._outer.addLayout(self.buttons)
        self.setTabOrder(self.original_button, self.use_button)
        self.setTabOrder(self.use_button, self.keep_button)
        for each in (*self._all_buttons(), self.resolve_button):
            each.installEventFilter(self)
        self.hide()
        self.retranslate()

    def _all_buttons(self) -> tuple[QPushButton, QPushButton, QPushButton]:
        return (self.original_button, self.use_button, self.keep_button)

    def set_colors(self, text: QColor, shade: QColor) -> None:
        """In the note's colours, as a reminder's strip (stickle.app.reminder_bar)."""
        ink = text.name()
        rgb = f"{shade.red()}, {shade.green()}, {shade.blue()}"
        self.setStyleSheet(
            f"ConflictBar {{ background: rgba({rgb}, 0.10); }}"
            f"QLabel {{ color: {ink}; }}"
            "QPushButton, QToolButton {"
            f" color: {ink}; background: rgba({rgb}, 0.08);"
            # Narrower than a reminder's: the three fit in a note of the usual width.
            f" border: 1px solid rgba({rgb}, 0.35); border-radius: 4px; padding: 2px 6px; }}"
            f"QPushButton:hover, QToolButton:hover {{ background: rgba({rgb}, 0.18); }}"
            f"QPushButton:focus, QToolButton:focus {{ border: 2px solid rgba({rgb}, 0.8); }}"
            f"QToolButton {{ padding-right: {ARROW_WIDTH}px; }}"
            "QToolButton::menu-indicator {"
            " subcontrol-origin: padding; subcontrol-position: center right; right: 6px; }"
        )

    def show_copy(self, copy: CopyOf | None) -> None:
        """Shown for a conflict copy, hidden for any other note."""
        self.copy = copy
        self.retranslate()
        self.setVisible(copy is not None)
        self._fit()

    def shown(self) -> list[QAbstractButton]:
        """Its buttons that show, in order."""
        if self.resolve_button.isVisibleTo(self):
            return [self.resolve_button]
        return [each for each in self._all_buttons() if each.isVisibleTo(self)]

    def retranslate(self) -> None:
        copy = self.copy
        line = "" if copy is None else written(copy)
        said = "" if copy is None else explained(copy)
        self.line.setText(line)
        self.setToolTip(said)
        self.setAccessibleName(said)
        self.line.setAccessibleName(line)
        self.line.setAccessibleDescription(said)
        original = QCoreApplication.translate("ConflictBar", "Show the original")
        use = QCoreApplication.translate("ConflictBar", "Use this text")
        keep = QCoreApplication.translate("ConflictBar", "Keep both")
        use_said = QCoreApplication.translate(
            "ConflictBar",
            "Puts this text in the original note. Its current text goes to the trash.",
        )
        for button, action, text in (
            (self.original_button, self.original_action, original),
            (self.use_button, self.use_action, use),
            (self.keep_button, self.keep_action, keep),
        ):
            button.setText(text)
            action.setText(text)
        self.use_button.setToolTip(use_said)
        self.use_button.setAccessibleDescription(use_said)
        self.use_action.setToolTip(use_said)
        self.resolve_menu.setToolTipsVisible(True)
        resolve = QCoreApplication.translate("ConflictBar", "Resolve")
        self.resolve_button.setText(resolve)
        self.resolve_button.setAccessibleName(resolve)
        self.resolve_button.setAccessibleDescription(said)
        self._show_choices()

    def _show_choices(self) -> None:
        """Only what can be done: with its original gone, nothing to show or give
        the text to; with a secret note on either side, nothing to give it to."""
        copy = self.copy
        original = copy is not None and copy.original
        swappable = original and copy is not None and copy.swappable
        self.original_action.setVisible(original)
        self.use_action.setVisible(swappable)
        self._wanted = {
            self.original_button: original,
            self.use_button: swappable,
            self.keep_button: True,
        }
        self._fit()

    def needed_width(self) -> int:
        """How wide it has to be for the buttons that apply side by side."""
        wanted = [button for button, shown in self._wanted.items() if shown]
        margins = self._outer.contentsMargins()
        spacing = self.buttons.spacing() * max(0, len(wanted) - 1)
        buttons = sum(each.sizeHint().width() for each in wanted)
        return margins.left() + buttons + spacing + margins.right()

    def _fit(self) -> None:
        """The buttons side by side where they fit; else [Resolve]."""
        wanted = [button for button, shown in self._wanted.items() if shown]
        narrow = len(wanted) > 1 and self.needed_width() > self.width()
        focused = any(each.hasFocus() for each in (*self._all_buttons(), self.resolve_button))
        for button, shown in self._wanted.items():
            button.setVisible(shown and not narrow)
        self.resolve_button.setVisible(narrow)
        if focused and not any(each.hasFocus() for each in self.shown()):
            self.shown()[0].setFocus(Qt.FocusReason.OtherFocusReason)

    def focus_first(self, back: bool = False) -> None:
        """The keyboard on its first button (back: its last)."""
        shown = self.shown()
        if shown:
            shown[-1 if back else 0].setFocus(Qt.FocusReason.TabFocusReason)

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    @override
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
            and not event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            shown = self.shown()
            back = event.key() == Qt.Key.Key_Backtab
            if watched in shown:
                at = shown.index(watched)  # pyright: ignore[reportArgumentType]
                step = at - 1 if back else at + 1
                if 0 <= step < len(shown):
                    shown[step].setFocus(
                        Qt.FocusReason.BacktabFocusReason if back else Qt.FocusReason.TabFocusReason
                    )
                else:
                    self.left.emit()
                return True
        return super().eventFilter(watched, event)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
