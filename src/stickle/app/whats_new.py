"""After an update, a note on what changed: once per version, never at first start.

The note is an ordinary note (hide it, delete it, it is backed up and exported
like any other). Its id comes from the version, so the same note made on two
devices is one note once sync exists. Which version was last given is a shared
setting: seen on one device, it does not come up again on another.
"""

import logging
import uuid

import apsw
from PySide6.QtCore import QCoreApplication

from stickle import __version__
from stickle.app.support import site_page
from stickle.core.updates import version_key
from stickle.data.notes import NoteRepository
from stickle.data.settings import NEWS_SEEN, Settings

log = logging.getLogger(__name__)

COLOR = "sky"
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "stickle.linkro.co")


def news_note_id(version: str) -> str:
    """The same id for a version's note on every device."""
    return str(uuid.uuid5(_NAMESPACE, f"whats-new/{release_line(version)}"))


def release_line(version: str) -> str:
    """0.5.2 and 0.5.0-preview.1 are both 0.5: one note for each minor version."""
    key = version_key(version)
    return f"{key[0]}.{key[1]}" if key else version


def news(version: str) -> str | None:
    """What changed in that version, in the interface language, as Markdown.
    None for a version with nothing to tell (a fix release)."""
    if release_line(version) == "0.5":
        text = QCoreApplication.translate(
            "WhatsNew",
            "# What's new in Stickle 0.5\n"
            "- **Daily backups**: a copy of your notes each day, kept for a week. "
            "Go back to one from Data → Restore from a backup…\n"
            "- **Export**: every note as Markdown, or everything as JSON "
            "(Data → Export all notes…)\n"
            "- **Bring notes in** from Microsoft Sticky Notes, Zhorn Stickies, xpad "
            "and Indicator Stickynotes (Data → Bring notes in from another app…)\n"
            "- **Paste with formatting**: bold, lists and links copied from a web page "
            "or a document stay as they were\n"
            "- **Lining up when resizing**: let a note go near the edge of the one "
            "beside it and the edges meet. 'Line notes up like a magnet' turns lining "
            "up on or off\n"
            "- **Report a problem or idea** from the Stickle icon's menu\n"
            "- A double click on the Stickle icon makes a new note, where the system "
            "allows it\n"
            "\n"
            "Data is in Stickle Manager and in the Stickle icon's menu. "
            "[Everything Stickle does](%1)",
        )
        return text.replace("%1", site_page("guide"))
    if release_line(version) == "0.6":
        text = QCoreApplication.translate(
            "WhatsNew",
            "# What's new in Stickle 0.6\n"
            "- **Stickle Manager** is the new name of the window with the list of "
            "notes, and settings have a window of their own (Settings… there and in "
            "the Stickle icon's menu)\n"
            "- **Reminders**: Ctrl+R in a note sets its reminder, once or repeating; "
            "when it rings, 'Remind me again' puts it off. Ctrl+Shift+R while editing "
            "writes a reminder into that line (⏰). Reminders that came while Stickle "
            "was off are shown as missed reminders when it starts\n"
            "- **Other calendars** for reminders: the lunar calendar, the Chinese lunar, "
            "Hebrew, Islamic, Persian, Ethiopian, Julian, Indian national and Bengali "
            "calendars\n"
            "- **Secret notes**: 'Make it a secret note…' in the note menu. Its text is "
            "kept encrypted with the secret notes' password and stays covered until you "
            "type it. Forgot the password? The recovery key sets a new one\n"
            "- **Protection** in Settings: make a new recovery key, and set a spare "
            "password to remember in case you forget where you put the key\n"
            "- **Your own colours**: up to 12 more colours, a random colour for new "
            "notes if you like, and Ctrl+Shift+] and Ctrl+Shift+[ to step through a "
            "note's colours\n"
            "- **Out of the way of full screen**: while a slide show, a video or a game "
            "fills a screen, its notes step aside, and come back when it ends "
            "(Windows and X11)\n"
            "- **Stickle's own export** can be brought in again, secret notes too "
            "(Data → Bring notes in from another app… → Stickle export…)\n"
            "\n"
            "Reminders ring while Stickle is running. [Everything Stickle does](%1)",
        )
        return text.replace("%1", site_page("guide"))
    return None


def give_news(
    notes: NoteRepository, settings: Settings, first_start: bool, version: str = __version__
) -> str | None:
    """The note for this version if it is new to this user; its id, or None.
    At first start the welcome note says it all: only remembered."""
    try:
        seen = settings.get(NEWS_SEEN)
        if first_start:
            settings.set(NEWS_SEEN, version)
            return None
        if seen is not None and release_line(seen) == release_line(version):
            return None
        older, now = version_key(seen) if seen else None, version_key(version)
        if older is not None and now is not None and older > now:
            return None  # an older Stickle on notes a newer one used: nothing to tell
        settings.set(NEWS_SEEN, version)
        text = news(version)
        note_id = news_note_id(version)
        if text is None or notes.get(note_id) is not None:
            return None
        notes.create(text, COLOR, note_id)
        log.info("what's new note for %s", release_line(version))
        return note_id
    except apsw.Error as error:
        # Never in the way of starting: the note is only a courtesy.
        log.error("no what's new note: %s", type(error).__name__)
        return None
