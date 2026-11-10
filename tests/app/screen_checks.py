"""What a machine can tell of how a window looks: text cut off, parts on top of
one another or out of the window, and whether the keyboard gets everywhere.

Each check gives what it found wrong, in words; nothing found, an empty list."""

from itertools import combinations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QWidget,
)
from test_accessibility import (
    COMPOSITE,
    names,
    part_of_another,
    reading_order,
    rotated_to,
    tab_chain,
    unnamed,
)

from stickle.app.link_label import LinkLabel
from stickle.app.stickle_window import ElidedButton

SCREEN = QSize(1366, 768)  # a small screen still in use
# Their parts are laid out by them, some on top of one another on purpose.
_OWN_PARTS = (*COMPOSITE, QAbstractScrollArea, QTabWidget, QStackedWidget)
_CONTROLS = (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QKeySequenceEdit,
    QLineEdit,
    QPlainTextEdit,
    QTabBar,
    QTextEdit,
    LinkLabel,
)


def _said(widget: QWidget) -> str:
    text = widget.text() if isinstance(widget, QAbstractButton | QLabel) else ""
    return f"{type(widget).__name__} {(text or widget.accessibleName())[:40]!r}"


def _shown(top: QWidget) -> list[QWidget]:
    """What is in sight in the window, its own windows left out."""
    return [
        widget
        for widget in top.findChildren(QWidget)
        if widget.isVisibleTo(top) and widget.window() is top
    ]


def _room_wanted(widget: QWidget) -> tuple[int, int] | None:
    """The width and height (0: any) its text takes; None: it has none to fit."""
    if isinstance(widget, ElidedButton):  # ends its text with "…" by itself
        return None
    if isinstance(widget, QLabel):
        if not widget.text():
            return None
        if widget.wordWrap():  # as wide as its longest word, as tall as its lines
            return widget.minimumSizeHint().width(), widget.heightForWidth(widget.width())
        wanted = widget.sizeHint()
        return wanted.width(), wanted.height()
    if isinstance(widget, QAbstractButton):
        return (widget.sizeHint().width(), 0) if widget.text() else None
    if isinstance(widget, QComboBox | QTabBar):
        return widget.sizeHint().width(), 0
    return None


def cut_off(top: QWidget) -> list[str]:
    """Text with less room than it takes (a list's lines end in "…" on purpose)."""
    found: list[str] = []
    for widget in _shown(top):
        wanted = _room_wanted(widget)
        if wanted is None:
            continue
        width, height = wanted
        if widget.width() < width or widget.height() < height:
            found.append(f"{_said(widget)}: has {widget.width()}x{widget.height()}, takes {wanted}")
    return found


def overlapping(top: QWidget) -> list[str]:
    """Parts of one parent on top of one another."""
    found: list[str] = []
    for parent in [top, *_shown(top)]:
        if isinstance(parent, _OWN_PARTS):
            continue
        children = [
            child
            for child in parent.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            if child.isVisibleTo(top) and not child.isWindow()
        ]
        for one, other in combinations(children, 2):
            if one.geometry().intersects(other.geometry()):
                found.append(f"{_said(one)} and {_said(other)}")
    return found


def _scrolled(parent: QWidget) -> bool:
    """What is in it goes on past its edge, as scrolling is for."""
    if isinstance(parent, QAbstractScrollArea | QTabBar | QKeySequenceEdit):
        return True
    area = parent.parentWidget()
    return isinstance(area, QAbstractScrollArea) and area.viewport() is parent


def outside(top: QWidget) -> list[str]:
    """Parts past the edge of what they are in, and a window no small screen shows."""
    found: list[str] = []
    for widget in _shown(top):
        parent = widget.parentWidget()
        if parent is None or isinstance(parent, COMPOSITE) or _scrolled(parent):
            continue
        if not QRect(QPoint(0, 0), parent.size()).contains(widget.geometry()):
            found.append(f"{_said(widget)}: at {widget.geometry()}, in {parent.size()}")
    if top.width() > SCREEN.width() or top.height() > SCREEN.height():
        found.append(f"the window: {top.width()}x{top.height()}")
    return found


def unreached(top: QWidget) -> list[str]:
    """Controls in sight and on that Tab does not come to."""
    chain = tab_chain(top)
    return [
        _said(widget)
        for widget in _shown(top)
        if isinstance(widget, _CONTROLS)
        and widget.isEnabled()
        and not part_of_another(widget, top)
        and widget not in chain
        and widget.focusProxy() not in chain
    ]


def out_of_order(top: QWidget) -> list[str]:
    """Tab going otherwise than the window reads: top to bottom, left to right."""
    chain = tab_chain(top)
    if not chain:
        return []
    expected = reading_order(top, chain)
    went, reads = names(rotated_to(chain, expected[0])), names(expected)
    return [] if went == reads else [f"Tab goes {went}, it reads {reads}"]


def problems(top: QWidget, keyboard: bool = True) -> dict[str, list[str]]:
    """All that is wrong with a window in sight, check by check.

    keyboard: False for a window that is not gone through with Tab."""
    checks = {
        "cut off": cut_off(top),
        "overlapping": overlapping(top),
        "outside": outside(top),
    }
    if keyboard:
        checks |= {
            "unnamed": unnamed(top),
            "unreached": unreached(top),
            "out of order": out_of_order(top),
        }
    return {name: found for name, found in checks.items() if found}
