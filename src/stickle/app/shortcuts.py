"""The shortcuts that work whichever application is in front: a new note, the
Stickle window, and every note out of sight for a while.

They do what the command line can ask for (see stickle.platform.instance).
The defaults can be changed, or turned off, on each computer. A shortcut the
system refuses (another application has it) is left unregistered and said
so in the Stickle window for as long as it lasts, never in a pop-up.

Under Wayland the desktop keeps them instead, through its portal: Stickle
offers the defaults once, the desktop asks the user, and they are changed in
the desktop's own settings; Stickle only shows which keys the desktop gave.
"""

import logging
import re
from collections.abc import Callable
from enum import Enum

import apsw
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from stickle.data.settings import SHORTCUT_ACTIONS, SHORTCUTS, Settings
from stickle.platform.hotkeys import Combo, Hotkeys, Portal, system_hotkeys
from stickle.platform.instance import FIND, NEW_NOTE, NEXT_VIEW, SET_ASIDE, SHOW

log = logging.getLogger(__name__)

# Each action is also a request of stickle.platform.instance, under its setting key.
REQUESTS = {
    "new-note": NEW_NOTE,
    "show": SHOW,
    "hide-all": SET_ASIDE,
    "next-category": NEXT_VIEW,
    "find": FIND,
}
# No keys for the next category or finding at first: they would take keys from
# other apps for things not everyone uses.
DEFAULTS = {
    "new-note": "Ctrl+Alt+N",
    "show": "Ctrl+Alt+S",
    "hide-all": "Ctrl+Alt+H",
    "next-category": "",
    "find": "",
}
assert tuple(REQUESTS) == tuple(DEFAULTS) == SHORTCUT_ACTIONS


class State(Enum):
    ON = "on"
    OFF = "off"  # turned off by the user
    TAKEN = "taken"  # another application has it
    UNAVAILABLE = "unavailable"  # no way to register shortcuts here
    WAITING = "waiting"  # asked of the desktop, not answered yet


# GTK's modifier names, as GNOME describes shortcuts, and how Stickle writes them.
GTK_MODIFIERS = {
    "Control": "Ctrl",
    "Primary": "Ctrl",
    "Alt": "Alt",
    "Shift": "Shift",
    "Super": "Super",
    "Meta": "Meta",
    "Logo": "Super",
}


def readable_keys(described: str) -> str:
    """Keys as the desktop described them, written as Stickle writes keys.

    GNOME says "Press <Control><Alt>n" (in the desktop's language: "<Control><Alt>n
    키를 누르십시오" in Korean); that becomes "Ctrl+Alt+N". A description in any
    other form is shown as the desktop wrote it.
    """
    match = re.search(r"((?:<\w+>)+)([^\s<>]+)", described)
    if match is None:
        return described
    names = re.findall(r"<(\w+)>", match.group(1))
    if any(name not in GTK_MODIFIERS for name in names):
        return described
    key = match.group(2)
    return "+".join(
        [*(GTK_MODIFIERS[name] for name in names), key.upper() if len(key) == 1 else key]
    )


def no_portal() -> Portal | None:
    return None


class Refused(Enum):
    """Why a new combination was not taken."""

    NOT_A_SHORTCUT = "not a shortcut"  # without Ctrl, Alt or the Windows key, or not a key we know
    IN_USE_HERE = "in use here"  # another of Stickle's own shortcuts


class GlobalShortcuts(QObject):
    pressed = Signal(bytes)  # the request of stickle.platform.instance
    changed = Signal()  # a shortcut or its state changed

    def __init__(
        self,
        settings: Settings | None,
        make_hotkeys: Callable[[Callable[[int], object]], Hotkeys | None] = system_hotkeys,
        make_portal: Callable[[], Portal | None] = no_portal,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._hotkeys = make_hotkeys(self._heard)
        self._portal = make_portal() if self._hotkeys is None else None
        self._given: dict[str, str] | None = None  # by the desktop, once it answers
        self._states: dict[str, State] = {}
        if self._portal is not None:
            self._portal.activated.connect(self._activated)
            self._portal.bound.connect(self._bound)
            self._portal.failed.connect(self._portal_failed)
        for action in SHORTCUT_ACTIONS:
            self._register(action)

    @property
    def available(self) -> bool:
        return self._hotkeys is not None or self._portal is not None

    @property
    def by_desktop(self) -> bool:
        """Kept by the desktop (its portal): changed in its settings, not here."""
        return self._portal is not None

    def start(self, over: QWidget | None = None) -> None:
        """Offer the shortcuts to the desktop, once Stickle is up: it may ask the
        user in a window of its own, which should not come over Stickle's first start.
        Its question belongs to over when that window is showing, or it could
        open behind it (a window kept on top)."""
        if self._portal is None:
            return
        descriptions = {
            "new-note": self.tr("New note"),
            "show": self.tr("Open Stickle Manager"),
            "hide-all": self.tr("Hide all notes for now, or show them again"),
            "next-category": self.tr("Show only the notes of the next category"),
            "find": self.tr("Find notes on the desktop"),
        }
        wanted: list[tuple[str, str, str]] = []
        for action in SHORTCUT_ACTIONS:
            combo = Combo.parse(self.combo(action))
            wanted.append((action, descriptions[action], combo.portal_trigger if combo else ""))
        x11 = QGuiApplication.platformName() == "xcb"
        showing = over if over is not None and x11 and over.isVisible() else None
        self._portal.bind(wanted, f"x11:{int(showing.winId()):x}" if showing else "")

    def configure(self) -> bool:
        """Open the desktop's page for the shortcuts, where it has one."""
        return self._portal is not None and self._portal.configure()

    def combo(self, action: str) -> str:
        """The action's key combination ("Ctrl+Alt+N"), "" when turned off.

        Kept by the desktop, the keys as the desktop writes them.
        """
        if self._portal is not None and self._given is not None:
            return self._given.get(action, "")
        chosen = self._settings.get(SHORTCUTS) if self._settings else {}
        text = chosen.get(action, DEFAULTS[action])
        if text and Combo.parse(text) is None:
            return DEFAULTS[action]  # no longer understood (edited by hand): the default
        return text

    def state(self, action: str) -> State:
        return self._states[action]

    def change(self, action: str, text: str) -> Refused | None:
        """Use a new combination for the action ("" turns it off); None once done.

        Not where the desktop keeps the shortcuts (see configure).
        """
        if text:
            combo = Combo.parse(text)
            if combo is None or not combo.usable:
                return Refused.NOT_A_SHORTCUT
            if any(
                self.combo(other) == combo.text for other in SHORTCUT_ACTIONS if other != action
            ):
                return Refused.IN_USE_HERE
            text = combo.text
        if self._settings is not None:
            chosen = dict(self._settings.get(SHORTCUTS))
            chosen[action] = text
            try:
                self._settings.set(SHORTCUTS, chosen)
            except apsw.Error as error:
                log.error("could not keep a shortcut: %s", type(error).__name__)
        self._register(action, text)
        self.changed.emit()
        return None

    def _register(self, action: str, text: str | None = None) -> None:
        number = SHORTCUT_ACTIONS.index(action) + 1
        text = self.combo(action) if text is None else text
        if self._portal is not None:
            self._states[action] = State.WAITING
            return
        if self._hotkeys is None:
            self._states[action] = State.UNAVAILABLE
            return
        self._hotkeys.unregister(number)
        combo = Combo.parse(text) if text else None
        if combo is None:
            self._states[action] = State.OFF
        elif self._hotkeys.register(number, combo):
            self._states[action] = State.ON
        else:
            log.info("shortcut for %s is taken by another application", action)
            self._states[action] = State.TAKEN

    def _heard(self, number: int) -> None:
        if 1 <= number <= len(SHORTCUT_ACTIONS):
            self.pressed.emit(REQUESTS[SHORTCUT_ACTIONS[number - 1]])

    # Kept by the desktop

    def _activated(self, action: str) -> None:
        if action in REQUESTS:
            self.pressed.emit(REQUESTS[action])

    def _bound(self, given: dict[str, str]) -> None:
        """The desktop's answer, and again whenever its settings change them."""
        self._given = {action: readable_keys(keys) for action, keys in given.items()}
        for action in SHORTCUT_ACTIONS:
            self._states[action] = State.ON if given.get(action) else State.OFF
        log.info("the desktop gave %d of the shortcuts", sum(map(bool, given.values())))
        self.changed.emit()

    def _portal_failed(self) -> None:
        self._portal = None
        for action in SHORTCUT_ACTIONS:
            self._states[action] = State.UNAVAILABLE
        self.changed.emit()
