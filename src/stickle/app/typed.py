"""Passwords and keys typed into a window, forgotten once it closes: such a
window lives on, kept by the window it belongs to, long after it is used."""

from PySide6.QtWidgets import QLineEdit, QWidget


def forget_typed(window: QWidget, *fields: QLineEdit) -> None:
    """Every password field in the window emptied, and the fields given."""
    for field in window.findChildren(QLineEdit):
        if field.echoMode() != QLineEdit.EchoMode.Normal or field in fields:
            field.clear()
