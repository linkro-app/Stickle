"""Text saying something went wrong, in a colour that reads on the window it is in."""

from PySide6.QtWidgets import QWidget

ON_LIGHT = "#b3261e"
ON_DARK = "#f2b8b5"
_DARK_BELOW = 128  # the window's lightness, from 0 to 255


def error_style(widget: QWidget) -> str:
    """The style sheet for a widget saying so, by the window as it is now."""
    dark = widget.palette().window().color().lightness() < _DARK_BELOW
    return f"color: {ON_DARK if dark else ON_LIGHT};"
