"""Links in a label, reached one at a time with Tab and Shift+Tab and opened with Enter."""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from stickle.app.link_label import LinkLabel


class Window(QDialog):
    """A button, three links, then the default button, as in About Stickle."""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.first = QPushButton("first", self)
        self.label = LinkLabel(self)
        self.label.setText('<a href="a">One</a> · <a href="b">Two</a> · <a href="c">Three</a>')
        self.close_button = QPushButton("close", self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.close)
        for widget in (self.first, self.label, self.close_button):
            layout.addWidget(widget)
        self.opened: list[str] = []
        self.label.linkActivated.connect(self.opened.append)


def where(window: Window) -> str:
    focused = QApplication.focusWidget()
    if focused is window.label:
        return window.label.selectedText()
    return focused.text() if isinstance(focused, QPushButton) else repr(focused)


def press(qtbot: QtBot, window: Window, keys: int, backwards: bool = False) -> list[str]:
    seen: list[str] = []
    for _ in range(keys):
        focused = QApplication.focusWidget()
        assert isinstance(focused, QWidget)
        if backwards:
            QTest.keyClick(focused, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        else:
            QTest.keyClick(focused, Qt.Key.Key_Tab)
        seen.append(where(window))
    return seen


def shown(qtbot: QtBot) -> Window:
    window = Window()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window)
    return window


def test_tab_goes_from_link_to_link_with_no_stop_between(qtbot: QtBot) -> None:
    # Qt's own label stopped once with no link chosen first.
    window = shown(qtbot)
    window.first.setFocus()

    assert press(qtbot, window, 4) == ["One", "Two", "Three", "close"]


def test_shift_tab_goes_back_through_the_links_and_out(qtbot: QtBot) -> None:
    # Qt's own label kept Shift+Tab at its first link.
    window = shown(qtbot)
    window.close_button.setFocus()

    assert press(qtbot, window, 4, backwards=True) == ["Three", "Two", "One", "first"]


def test_enter_opens_the_link_tab_arrived_at(qtbot: QtBot) -> None:
    # It pressed the default button instead, closing the window.
    window = shown(qtbot)
    window.close_button.setFocus()
    press(qtbot, window, 1, backwards=True)

    QTest.keyClick(window.label, Qt.Key.Key_Return)

    assert window.opened == ["c"]
    assert window.isVisible()
