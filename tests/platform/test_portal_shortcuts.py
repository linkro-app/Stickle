"""Shortcuts through the desktop portal, played against a stand-in for the bus.

The portal answers each request later, with a Response signal on the
request's own path; these tests answer as it does, message by message.
Every message Stickle sends is also serialised, as the real connection
does, so a body that does not match its signature fails here.
"""

import socket
from collections.abc import Iterator
from itertools import count
from typing import Any

import pytest
from jeepney import (
    DBusAddress,
    HeaderFields,
    Message,
    new_error,
    new_method_return,
    new_signal,
)
from pytestqt.qtbot import QtBot

from stickle.platform.hotkeys import Combo
from stickle.platform.linux.portal_shortcuts import (
    DESKTOP_PATH,
    REQUEST,
    SHORTCUTS,
    PortalShortcuts,
)

SESSION = f"{DESKTOP_PATH}/session/1_42/stickle"
WANTED = [
    ("new-note", "New note", "CTRL+ALT+n"),
    ("show", "Open Stickle Manager", "CTRL+ALT+s"),
    ("hide-all", "Hide all notes for now", ""),
]


class Bus:
    """What Stickle's connection sends, and a way to answer."""

    def __init__(self) -> None:
        self.unique_name = ":1.42"
        self.outgoing_serial: Iterator[int] = count(start=2)  # 1 was Hello
        self.sock, self._other_end = socket.socketpair()
        self.sent: list[tuple[Message, int]] = []

    def send(self, message: Message, serial: int | None = None) -> None:
        assert serial is not None
        message.serialise(serial=serial)  # as the real connection does: types checked
        self.sent.append((message, serial))

    def receive(self, *, timeout: float | None = None) -> Message:
        raise TimeoutError

    def close(self) -> None:
        self.sock.close()
        self._other_end.close()

    def call(self, member: str) -> tuple[Message, int]:
        return next((m, s) for m, s in self.sent if m.header.fields[HeaderFields.member] == member)

    def members(self) -> list[str]:
        return [str(m.header.fields[HeaderFields.member]) for m, _ in self.sent]


def reply(call: tuple[Message, int], signature: str, body: tuple[Any, ...]) -> Message:
    message = new_method_return(call[0], signature, body)
    message.header.fields[HeaderFields.reply_serial] = call[1]
    return message


def error(call: tuple[Message, int]) -> Message:
    message = new_error(call[0], "org.freedesktop.DBus.Error.UnknownMethod")
    message.header.fields[HeaderFields.reply_serial] = call[1]
    return message


def response(call: tuple[Message, int], code: int, results: dict[str, Any]) -> Message:
    """The Response the portal sends where the request asked it to."""
    options = call[0].body[-1]
    token = options["handle_token"][1]
    path = f"{DESKTOP_PATH}/request/1_42/{token}"
    return new_signal(DBusAddress(path, interface=REQUEST), "Response", "ua{sv}", (code, results))


def shortcut_signal(member: str, signature: str, body: tuple[Any, ...]) -> Message:
    return new_signal(DBusAddress(DESKTOP_PATH, interface=SHORTCUTS), member, signature, body)


@pytest.fixture
def bus() -> Iterator[Bus]:
    bus = Bus()
    yield bus
    bus.close()


@pytest.fixture
def portal(qtbot: QtBot, bus: Bus) -> PortalShortcuts:
    return PortalShortcuts("co.linkro.stickle", bus)  # pyright: ignore[reportArgumentType]


def bound_session(portal: PortalShortcuts, bus: Bus, version: int = 1) -> None:
    portal.bind(WANTED)
    portal.handle(reply(bus.call("Get"), "v", (("u", version),)))
    portal.handle(response(bus.call("CreateSession"), 0, {"session_handle": ("o", SESSION)}))


def test_it_listens_for_the_portal_signals(bus: Bus, portal: PortalShortcuts) -> None:
    assert bus.members() == ["AddMatch", "AddMatch", "AddMatch"]


def test_it_names_the_app_opens_a_session_and_offers_the_shortcuts(
    bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus)

    register, _ = bus.call("Register")
    assert register.body[0] == "co.linkro.stickle"
    session, shortcuts, _parent, _options = bus.call("BindShortcuts")[0].body
    assert session == SESSION
    offered = dict(shortcuts)
    assert offered["new-note"]["description"] == ("s", "New note")
    assert offered["new-note"]["preferred_trigger"] == ("s", "CTRL+ALT+n")
    assert "preferred_trigger" not in offered["hide-all"]  # turned off: no suggestion
    assert _parent == ""  # no window of Stickle's to ask over


def test_the_desktops_question_comes_up_over_the_window_named(
    bus: Bus, portal: PortalShortcuts
) -> None:
    portal.bind(WANTED, "x11:4a00007")
    portal.handle(reply(bus.call("Get"), "v", (("u", 1),)))
    portal.handle(response(bus.call("CreateSession"), 0, {"session_handle": ("o", SESSION)}))

    _session, _shortcuts, parent, _options = bus.call("BindShortcuts")[0].body
    assert parent == "x11:4a00007"


def test_the_keys_the_desktop_gave_are_told(
    qtbot: QtBot, bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus)
    given = [
        ("new-note", {"trigger_description": ("s", "Ctrl+Alt+N")}),
        ("show", {"trigger_description": ("s", "")}),
    ]
    told: list[dict[str, str]] = []
    portal.bound.connect(told.append)

    portal.handle(response(bus.call("BindShortcuts"), 0, {"shortcuts": ("a(sa{sv})", given)}))

    assert told == [{"new-note": "Ctrl+Alt+N", "show": "", "hide-all": ""}]


def test_turned_down_in_the_desktops_dialog_none_work(
    qtbot: QtBot, bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus)
    told: list[dict[str, str]] = []
    portal.bound.connect(told.append)

    portal.handle(response(bus.call("BindShortcuts"), 1, {}))

    assert told == [{"new-note": "", "show": "", "hide-all": ""}]


def test_a_press_is_heard_only_for_its_own_session(
    qtbot: QtBot, bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus)
    heard: list[str] = []
    portal.activated.connect(heard.append)

    portal.handle(shortcut_signal("Activated", "osta{sv}", (SESSION, "new-note", 5, {})))
    portal.handle(shortcut_signal("Activated", "osta{sv}", (f"{SESSION}x", "show", 6, {})))

    assert heard == ["new-note"]


def test_keys_changed_in_the_desktops_settings_are_told(
    qtbot: QtBot, bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus)
    changed = [("show", {"trigger_description": ("s", "Super+S")})]
    told: list[dict[str, str]] = []
    portal.bound.connect(told.append)

    portal.handle(shortcut_signal("ShortcutsChanged", "oa(sa{sv})", (SESSION, changed)))

    assert told == [{"new-note": "", "show": "Super+S", "hide-all": ""}]


def test_a_desktop_without_the_portal_is_told_at_once(
    qtbot: QtBot, bus: Bus, portal: PortalShortcuts
) -> None:
    portal.bind(WANTED)
    with qtbot.waitSignal(portal.failed):
        portal.handle(error(bus.call("Get")))

    assert "CreateSession" not in bus.members()


def test_the_desktops_page_opens_only_where_the_portal_has_one(
    bus: Bus, portal: PortalShortcuts
) -> None:
    bound_session(portal, bus, version=1)
    assert not portal.configure()

    newer = PortalShortcuts("co.linkro.stickle", bus)  # pyright: ignore[reportArgumentType]
    bus.sent.clear()
    bound_session(newer, bus, version=2)
    assert newer.configure()
    assert bus.call("ConfigureShortcuts")[0].body[0] == SESSION


def test_combinations_are_suggested_as_the_portal_writes_them() -> None:
    assert Combo("N", ctrl=True, alt=True).portal_trigger == "CTRL+ALT+n"
    assert Combo("F7", shift=True, meta=True).portal_trigger == "SHIFT+LOGO+F7"
