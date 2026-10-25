"""What a note is shown by in lists, menus and its own title bar.

A secret note is shown by the title typed for it, locked or not: nothing of
its text appears anywhere but in its own open window.
"""

from PySide6.QtCore import QCoreApplication

from stickle.core.markdown import note_title
from stickle.core.note import Note


def secret_name(title: str | None) -> str:
    """A secret note's title as typed, or a name for one with none."""
    return title or QCoreApplication.translate("SecretNote", "Secret note")


def shown_title(note: Note) -> str:
    """Its first line ("" for none); a secret note's typed title instead."""
    return secret_name(note.secret_title) if note.secret else note_title(note.body)
