"""The desktop's own file chooser, through its portal (org.freedesktop.portal.FileChooser).

Qt's own file dialog, which an AppImage falls back on, is not the desktop's:
it looks foreign and some of its words are left untranslated. The portal
shows the desktop's dialog, in the desktop's language.

A call waits, as a dialog does, while the desktop's dialog is open: the
answer comes as a Response signal on the request's own object path. Where
there is no portal (or it fails) PortalUnavailableError is raised, and the caller shows
Qt's dialog instead.
"""

import secrets
from pathlib import Path
from typing import Any, Literal, cast

from jeepney import DBusAddress, HeaderFields, Message, MessageType, new_method_call
from jeepney.bus_messages import MatchRule, message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from PySide6.QtCore import QEventLoop, QSocketNotifier, QUrl

DESKTOP = "org.freedesktop.portal.Desktop"
DESKTOP_PATH = "/org/freedesktop/portal/desktop"
FILE_CHOOSER = DBusAddress(
    DESKTOP_PATH, bus_name=DESKTOP, interface="org.freedesktop.portal.FileChooser"
)
REQUEST = "org.freedesktop.portal.Request"
ACCEPTED, CANCELLED = 0, 1

type Variants = dict[str, tuple[str, Any]]


class PortalUnavailableError(Exception):
    """No file chooser portal here, or it failed: Qt's dialog is shown instead."""


def _request_path(unique_name: str, token: str) -> str:
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{DESKTOP_PATH}/request/{sender}/{token}"


def choose(
    kind: Literal["open", "save", "folder"],
    title: str,
    parent_window: str = "",
    suggested: Path | None = None,
    connection: DBusConnection | None = None,
) -> Path | None:
    """A file to open, a file to save to (suggested: its folder and name) or a
    folder; None if the user cancelled. parent_window: "x11:<window id in hex>" or ""."""
    try:
        bus = connection or open_dbus_connection(bus="SESSION")
    except (OSError, ValueError, KeyError) as error:  # no session bus
        raise PortalUnavailableError(type(error).__name__) from error
    try:
        return _Request(bus).run(kind, title, parent_window, suggested)
    finally:
        if connection is None:
            bus.close()


class _Request:
    def __init__(self, bus: DBusConnection) -> None:
        self._bus = bus
        self._token = f"stickle_{secrets.token_hex(8)}"
        self._path = _request_path(str(bus.unique_name), self._token)
        self._call_serial = 0
        self._result: Path | PortalUnavailableError | None = PortalUnavailableError("no answer")
        self._done = False
        self._loop: QEventLoop | None = None

    def run(self, kind: str, title: str, parent_window: str, suggested: Path | None) -> Path | None:
        # Listened for before asking, so a quick answer is not missed.
        self._send(
            message_bus.AddMatch(MatchRule(type="signal", interface=REQUEST, path=self._path))
        )
        options: Variants = {"handle_token": ("s", self._token), "modal": ("b", True)}
        if kind == "folder":
            options["directory"] = ("b", True)
            method = "OpenFile"
        elif kind == "open":
            method = "OpenFile"
        else:
            method = "SaveFile"
            if suggested is not None:
                options["current_name"] = ("s", suggested.name)
                folder = bytes(suggested.parent) + b"\0"  # a byte string, as file paths are
                options["current_folder"] = ("ay", folder)
        call = new_method_call(FILE_CHOOSER, method, "ssa{sv}", (parent_window, title, options))
        self._call_serial = self._send(call)
        self._drain()
        if not self._done:
            loop = QEventLoop()
            self._loop = loop
            notifier = QSocketNotifier(self._bus.sock.fileno(), QSocketNotifier.Type.Read)
            notifier.activated.connect(self._drain)
            loop.exec()
            notifier.setEnabled(False)
        if isinstance(self._result, PortalUnavailableError):
            raise self._result
        return self._result

    def _send(self, message: Message) -> int:
        serial = next(self._bus.outgoing_serial)
        self._bus.send(message, serial=serial)
        return serial

    def _drain(self) -> None:
        while not self._done:
            try:
                message = self._bus.receive(timeout=0)
            except TimeoutError:
                return
            except OSError as error:
                self._finish(PortalUnavailableError(type(error).__name__))
                return
            self._handle(message)

    def _handle(self, message: Message) -> None:
        header = message.header
        if header.message_type == MessageType.error:
            if header.fields.get(HeaderFields.reply_serial) == self._call_serial:
                name = str(header.fields.get(HeaderFields.error_name, ""))
                self._finish(PortalUnavailableError(name))
        elif (
            header.message_type == MessageType.signal
            and header.fields.get(HeaderFields.member) == "Response"
            and header.fields.get(HeaderFields.path) == self._path
        ):
            code, results = cast(tuple[int, Variants], message.body)
            if code == CANCELLED:
                self._finish(None)
            elif code != ACCEPTED:
                self._finish(PortalUnavailableError(f"response {code}"))
            else:
                uris = cast(list[str], results.get("uris", ("as", []))[1])
                local = QUrl(uris[0]).toLocalFile() if uris else ""
                self._finish(Path(local) if local else PortalUnavailableError("not a local file"))

    def _finish(self, result: Path | PortalUnavailableError | None) -> None:
        self._result = result
        self._done = True
        if self._loop is not None:
            self._loop.quit()
