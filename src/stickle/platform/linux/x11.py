"""Talking to the X11 window manager where Qt does not say what is needed.

Keeping notes out of the taskbar without tying them together:

Qt turns a tool window into a window transient for the application's group,
and window managers such as GNOME's Mutter lift every such window to the
highest layer any of them is in: one note kept above other windows would keep
all notes above them. So under X11 notes are ordinary windows, and this
module asks the window manager to leave them out of the taskbar, the window
switcher and the pager, as tool windows are, through the window's
_NET_WM_STATE. Set before the window is mapped, the state is read when the
window manager takes the window; Qt keeps states it does not manage itself.

Bringing the Stickle window forward when Stickle is started again: Qt asks as
the application, which window managers refuse when another program was used
last, so it is asked as a taskbar would (see activate).

This talks to the X server through its own libxcb connection (the library Qt
itself uses on X11), so it needs nothing beyond what is already loaded.

Finding other programs' full-screen windows: see X11FullScreen.
"""

import ctypes
import logging
from collections.abc import Collection

from stickle.platform.full_screen import FullScreen

log = logging.getLogger(__name__)

# Predefined atoms naming the types of properties.
ATOM = 4
CARDINAL = 6
STRING = 31
WINDOW = 33
ALL_DESKTOPS = 0xFFFFFFFF  # _NET_WM_DESKTOP of a window shown on every desktop
PROP_MODE_REPLACE = 0
SKIP_STATES = (b"_NET_WM_STATE_SKIP_TASKBAR", b"_NET_WM_STATE_SKIP_PAGER")


class _Cookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _InternAtomReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("pad0", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("atom", ctypes.c_uint32),
    ]


class _GetPropertyReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("format", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytes_after", ctypes.c_uint32),
        ("value_len", ctypes.c_uint32),
        ("pad0", ctypes.c_uint8 * 12),
    ]


class _GetGeometryReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("depth", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("root", ctypes.c_uint32),
        ("x", ctypes.c_int16),
        ("y", ctypes.c_int16),
        ("width", ctypes.c_uint16),
        ("height", ctypes.c_uint16),
        ("border_width", ctypes.c_uint16),
        ("pad0", ctypes.c_uint8 * 2),
    ]


class _TranslateCoordinatesReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("same_screen", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("child", ctypes.c_uint32),
        ("dst_x", ctypes.c_int16),
        ("dst_y", ctypes.c_int16),
    ]


class _ScreenIterator(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint32)),  # xcb_screen_t, whose first field is root
        ("rem", ctypes.c_int),
        ("index", ctypes.c_int),
    ]


class _QueryPointerReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("same_screen", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("root", ctypes.c_uint32),
        ("child", ctypes.c_uint32),
        ("root_x", ctypes.c_int16),
        ("root_y", ctypes.c_int16),
        ("win_x", ctypes.c_int16),
        ("win_y", ctypes.c_int16),
        ("mask", ctypes.c_uint16),
        ("pad0", ctypes.c_uint8 * 2),
    ]


BUTTON_1 = 1 << 8  # in a pointer's mask: the main button is held


class _ClientMessage(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("format", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("window", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("data", ctypes.c_uint32 * 5),
    ]


CLIENT_MESSAGE = 33
STATE_ADD = 1
SOURCE_APPLICATION = 1
SOURCE_PAGER = 2
# SubstructureNotify | SubstructureRedirect: how EWMH requests reach the window manager.
TO_WINDOW_MANAGER = (1 << 19) | (1 << 20)


class _Xcb:
    def __init__(self) -> None:
        xcb = ctypes.CDLL("libxcb.so.1")
        libc = ctypes.CDLL(None)
        pointer, cookie = ctypes.c_void_p, _Cookie
        xcb.xcb_connect.restype = pointer
        xcb.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        xcb.xcb_connection_has_error.restype = ctypes.c_int
        xcb.xcb_connection_has_error.argtypes = [pointer]
        xcb.xcb_intern_atom.restype = cookie
        xcb.xcb_intern_atom.argtypes = [pointer, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_char_p]
        xcb.xcb_intern_atom_reply.restype = ctypes.POINTER(_InternAtomReply)
        xcb.xcb_intern_atom_reply.argtypes = [pointer, cookie, pointer]
        xcb.xcb_get_property.restype = cookie
        xcb.xcb_get_property.argtypes = [
            pointer, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ]  # fmt: skip
        xcb.xcb_get_property_reply.restype = ctypes.POINTER(_GetPropertyReply)
        xcb.xcb_get_property_reply.argtypes = [pointer, cookie, pointer]
        xcb.xcb_get_property_value.restype = ctypes.POINTER(ctypes.c_uint32)
        xcb.xcb_get_property_value.argtypes = [ctypes.POINTER(_GetPropertyReply)]
        xcb.xcb_change_property.restype = cookie
        xcb.xcb_change_property.argtypes = [
            pointer, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint32, pointer,
        ]  # fmt: skip
        xcb.xcb_send_event.restype = cookie
        xcb.xcb_send_event.argtypes = [
            pointer,
            ctypes.c_uint8,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        xcb.xcb_query_pointer.restype = cookie
        xcb.xcb_query_pointer.argtypes = [pointer, ctypes.c_uint32]
        xcb.xcb_query_pointer_reply.restype = ctypes.POINTER(_QueryPointerReply)
        xcb.xcb_query_pointer_reply.argtypes = [pointer, cookie, pointer]
        xcb.xcb_get_geometry.restype = cookie
        xcb.xcb_get_geometry.argtypes = [pointer, ctypes.c_uint32]
        xcb.xcb_get_geometry_reply.restype = ctypes.POINTER(_GetGeometryReply)
        xcb.xcb_get_geometry_reply.argtypes = [pointer, cookie, pointer]
        xcb.xcb_translate_coordinates.restype = cookie
        xcb.xcb_translate_coordinates.argtypes = [
            pointer, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int16, ctypes.c_int16,
        ]  # fmt: skip
        xcb.xcb_translate_coordinates_reply.restype = ctypes.POINTER(_TranslateCoordinatesReply)
        xcb.xcb_translate_coordinates_reply.argtypes = [pointer, cookie, pointer]
        xcb.xcb_get_setup.restype = pointer
        xcb.xcb_get_setup.argtypes = [pointer]
        xcb.xcb_setup_roots_iterator.restype = _ScreenIterator
        xcb.xcb_setup_roots_iterator.argtypes = [pointer]
        xcb.xcb_flush.restype = ctypes.c_int
        xcb.xcb_flush.argtypes = [pointer]
        libc.free.argtypes = [pointer]
        screen = ctypes.c_int(0)
        connection = xcb.xcb_connect(None, ctypes.byref(screen))
        if not connection or xcb.xcb_connection_has_error(connection):
            raise OSError("cannot connect to the X server")
        self._xcb = xcb
        self._free = libc.free
        self._connection = connection
        screens = xcb.xcb_setup_roots_iterator(xcb.xcb_get_setup(connection))
        if screens.rem <= screen.value:
            raise OSError("no such screen")
        # Screens vary in size, so step through them with the library, not by index.
        xcb.xcb_screen_next.restype = None
        xcb.xcb_screen_next.argtypes = [ctypes.POINTER(_ScreenIterator)]
        for _ in range(screen.value):
            xcb.xcb_screen_next(ctypes.byref(screens))
        self.root = int(screens.data[0])
        self.state = self._atom(b"_NET_WM_STATE")
        self.skip = [self._atom(name) for name in SKIP_STATES]

    def _atom(self, name: bytes) -> int:
        cookie = self._xcb.xcb_intern_atom(self._connection, 0, len(name), name)
        reply = self._xcb.xcb_intern_atom_reply(self._connection, cookie, None)
        if not reply:
            raise OSError("the X server did not answer")
        try:
            return int(reply.contents.atom)
        finally:
            self._free(reply)

    def atom(self, name: bytes) -> int:
        return self._atom(name)

    def atoms(self, window: int, prop: int) -> list[int]:
        return self.values(window, prop, ATOM)

    def values(self, window: int, prop: int, kind: int) -> list[int]:
        """A property of 32-bit values of a type (atoms, windows, numbers)."""
        cookie = self._xcb.xcb_get_property(self._connection, 0, window, prop, kind, 0, 1024)
        reply = self._xcb.xcb_get_property_reply(self._connection, cookie, None)
        if not reply:
            return []
        try:
            if reply.contents.format != 32:
                return []
            values = self._xcb.xcb_get_property_value(reply)
            return [int(values[i]) for i in range(reply.contents.value_len)]
        finally:
            self._free(reply)

    def text(self, window: int, prop: int) -> bytes:
        """A property of 8-bit text (as WM_CLASS); empty if it has none."""
        cookie = self._xcb.xcb_get_property(self._connection, 0, window, prop, STRING, 0, 256)
        reply = self._xcb.xcb_get_property_reply(self._connection, cookie, None)
        if not reply:
            return b""
        try:
            if reply.contents.format != 8:
                return b""
            values = self._xcb.xcb_get_property_value(reply)
            return ctypes.string_at(values, reply.contents.value_len)
        finally:
            self._free(reply)

    def place(self, window: int) -> tuple[int, int, int, int] | None:
        """Where a window is on the screen: x, y, width, height."""
        size_cookie = self._xcb.xcb_get_geometry(self._connection, window)
        at_cookie = self._xcb.xcb_translate_coordinates(self._connection, window, self.root, 0, 0)
        size = self._xcb.xcb_get_geometry_reply(self._connection, size_cookie, None)
        at = self._xcb.xcb_translate_coordinates_reply(self._connection, at_cookie, None)
        try:
            if not size or not at:
                return None
            return (
                int(at.contents.dst_x),
                int(at.contents.dst_y),
                int(size.contents.width),
                int(size.contents.height),
            )
        finally:
            if size:
                self._free(size)
            if at:
                self._free(at)

    def set_atoms(self, window: int, prop: int, atoms: list[int]) -> None:
        values = (ctypes.c_uint32 * len(atoms))(*atoms)
        self._xcb.xcb_change_property(
            self._connection, PROP_MODE_REPLACE, window, prop, ATOM, 32, len(atoms), values
        )
        self._xcb.xcb_flush(self._connection)

    def ask_window_manager(self, window: int, atoms: list[int]) -> None:
        """The way to change the state of a window that is already mapped."""
        first, second = [*atoms, 0][:2]  # one message changes at most two states
        self._send(window, self.state, [STATE_ADD, first, second, SOURCE_APPLICATION, 0])

    def activate(self, window: int) -> None:
        """Ask for the window to be brought forward and given the keyboard."""
        # As a taskbar asks, on behalf of the user; an application's own request
        # is refused when another program had the user's attention last.
        self._send(window, self._atom(b"_NET_ACTIVE_WINDOW"), [SOURCE_PAGER, 0, 0, 0, 0])

    def buttons(self) -> int:
        """The pointer's button and modifier mask, as the server has it now."""
        cookie = self._xcb.xcb_query_pointer(self._connection, self.root)
        reply = self._xcb.xcb_query_pointer_reply(self._connection, cookie, None)
        if not reply:
            return 0
        try:
            return reply.contents.mask
        finally:
            self._free(reply)

    def _send(self, window: int, message_type: int, data: list[int]) -> None:
        message = _ClientMessage(CLIENT_MESSAGE, 32, 0, window, message_type)
        message.data[:] = data
        self._xcb.xcb_send_event(
            self._connection, 0, self.root, TO_WINDOW_MANAGER, ctypes.byref(message)
        )
        self._xcb.xcb_flush(self._connection)


_xcb: _Xcb | None = None
_unavailable = False


def keep_off_taskbar(window: int, mapped: bool) -> None:
    """Leave an X11 window out of the taskbar and switchers.

    Called just before the window is mapped (the state is then read with the
    window) and again once it is (the window manager may have read it too
    early, so it is asked outright).
    """
    server = _server()
    if server is None:
        return  # the notes then show in the taskbar: a nuisance, not a failure
    states = server.atoms(window, server.state)
    missing = [atom for atom in server.skip if atom not in states]
    if missing and mapped:
        server.ask_window_manager(window, missing)
    elif missing:
        server.set_atoms(window, server.state, states + missing)


def activate(window: int) -> None:
    """Bring an X11 window forward with the keyboard, as a taskbar would.

    For a window the user asked for through another program (starting Stickle
    again): window managers refuse an application's own request then.
    """
    server = _server()
    if server is not None:
        server.activate(window)


def main_button_down() -> bool | None:
    """Whether the main mouse button is held right now; None if the server cannot
    be asked. Button 1 is the main one, whichever hand the buttons are set for."""
    server = _server()
    return None if server is None else bool(server.buttons() & BUTTON_1)


def _server() -> _Xcb | None:
    """The connection to the X server, or None if there is none to be had."""
    global _xcb, _unavailable
    if _unavailable:
        return None
    try:
        if _xcb is None:
            _xcb = _Xcb()
    except (OSError, AttributeError) as error:
        _unavailable = True
        log.warning("cannot reach the X server directly: %s", type(error).__name__)
        return None
    return _xcb


class X11FullScreen:
    """Other programs' full-screen windows under X11 (see stickle.platform.full_screen):
    those whose state says so, minimized ones and those on another desktop left out."""

    def __init__(self, server: _Xcb) -> None:
        self._server = server
        self._active = server.atom(b"_NET_ACTIVE_WINDOW")
        self._clients = server.atom(b"_NET_CLIENT_LIST")
        self._current_desktop = server.atom(b"_NET_CURRENT_DESKTOP")
        self._desktop = server.atom(b"_NET_WM_DESKTOP")
        self._full_screen = server.atom(b"_NET_WM_STATE_FULLSCREEN")
        self._hidden = server.atom(b"_NET_WM_STATE_HIDDEN")
        self._class = server.atom(b"WM_CLASS")

    @classmethod
    def connect(cls) -> X11FullScreen:
        server = _server()
        if server is None:
            raise OSError("cannot reach the X server")
        return cls(server)

    def find(self, known: Collection[int]) -> list[FullScreen]:
        server = self._server
        active = [w for w in server.values(server.root, self._active, WINDOW) if w]
        if active and self._full_screen in server.atoms(active[0], server.state):
            candidates = server.values(server.root, self._clients, WINDOW)
        elif known:
            candidates = list(known)
        else:
            return []
        current = server.values(server.root, self._current_desktop, CARDINAL)
        found: list[FullScreen] = []
        for window in candidates:
            states = server.atoms(window, server.state)
            if self._full_screen not in states or self._hidden in states:
                continue
            desktop = server.values(window, self._desktop, CARDINAL)
            if desktop and current and desktop[0] not in (current[0], ALL_DESKTOPS):
                continue
            place = server.place(window)
            if place is None:
                continue
            found.append(FullScreen(window, self._app(window), place))
        return found

    def _app(self, window: int) -> str:
        """Its WM_CLASS class (the property holds the instance, then the class)."""
        parts = self._server.text(window, self._class).split(b"\0")
        name = parts[1] if len(parts) > 1 and parts[1] else parts[0]
        return name.decode("utf-8", "replace")
