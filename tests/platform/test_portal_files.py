"""The desktop's file dialog through its portal, answered by a stand-in for the bus."""

import socket
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from jeepney import DBusAddress, HeaderFields, Message, new_error, new_signal
from pytestqt.qtbot import QtBot

from stickle.platform.linux.portal_files import (
    DESKTOP_PATH,
    REQUEST,
    PortalUnavailableError,
    choose,
)


class Bus:
    """Answers the file chooser call the way a desktop would, once asked."""

    def __init__(self, answer: str, uri: str = "") -> None:
        self.unique_name = ":1.7"
        self.outgoing_serial = count(start=2)
        self.sock, self._other = socket.socketpair()
        self.answer = answer  # accepted, cancelled, failed, missing
        self.uri = uri
        self.sent: list[Message] = []
        self.inbox: list[Message] = []

    def send(self, message: Message, serial: int | None = None) -> None:
        message.serialise(serial=serial)  # as the real connection does: types checked
        self.sent.append(message)
        if message.header.fields.get(HeaderFields.member) in ("SaveFile", "OpenFile"):
            self.inbox.append(self._reply(message, serial or 0))

    def _reply(self, call: Message, serial: int) -> Message:
        if self.answer == "missing":
            error = new_error(call, "org.freedesktop.DBus.Error.ServiceUnknown")
            error.header.fields[HeaderFields.reply_serial] = serial
            return error
        token = call.body[2]["handle_token"][1]
        path = f"{DESKTOP_PATH}/request/1_7/{token}"
        code = {"accepted": 0, "cancelled": 1, "failed": 2}[self.answer]
        results: dict[str, Any] = {"uris": ("as", [self.uri])} if code == 0 else {}
        return new_signal(
            DBusAddress(path, interface=REQUEST), "Response", "ua{sv}", (code, results)
        )

    def receive(self, *, timeout: float | None = None) -> Message:
        if not self.inbox:
            raise TimeoutError
        return self.inbox.pop(0)

    def close(self) -> None:
        self.sock.close()
        self._other.close()

    def call(self) -> Message:
        return next(m for m in self.sent if m.header.fields.get(HeaderFields.member) != "AddMatch")


@pytest.fixture
def made() -> Iterator[list[Bus]]:
    buses: list[Bus] = []
    yield buses
    for bus in buses:
        bus.close()


def bus(made: list[Bus], answer: str, uri: str = "") -> Bus:
    made.append(Bus(answer, uri))
    return made[-1]


def test_a_file_to_save_to_is_asked_with_the_name_and_folder_suggested(
    qtbot: QtBot, made: list[Bus]
) -> None:
    fake = bus(made, "accepted", "file:///home/me/%ED%82%A4.txt")

    chosen = choose(
        "save",
        "복구 키 저장",
        "x11:3a",
        Path("/home/me/Stickle recovery key.txt"),
        fake,  # pyright: ignore[reportArgumentType]
    )

    assert chosen == Path("/home/me/키.txt")
    call = fake.call()
    assert call.header.fields[HeaderFields.member] == "SaveFile"
    parent, title, options = call.body
    assert (parent, title) == ("x11:3a", "복구 키 저장")
    assert options["current_name"] == ("s", "Stickle recovery key.txt")
    assert options["current_folder"] == ("ay", bytes(Path("/home/me")) + b"\0")


def test_a_folder_is_asked_for_as_a_folder(qtbot: QtBot, made: list[Bus]) -> None:
    fake = bus(made, "accepted", "file:///home/me/backup")

    assert choose("folder", "폴더", "", None, fake) == Path("/home/me/backup")  # pyright: ignore[reportArgumentType]
    call = fake.call()
    assert call.header.fields[HeaderFields.member] == "OpenFile"
    assert call.body[2]["directory"] == ("b", True)


def test_a_file_to_open_is_asked_for_as_a_file(qtbot: QtBot, made: list[Bus]) -> None:
    fake = bus(made, "accepted", "file:///home/me/notes.json")

    assert choose("open", "가져오기", "", None, fake) == Path("/home/me/notes.json")  # pyright: ignore[reportArgumentType]
    call = fake.call()
    assert call.header.fields[HeaderFields.member] == "OpenFile"
    assert "directory" not in call.body[2]


def test_cancelling_chooses_nothing(qtbot: QtBot, made: list[Bus]) -> None:
    assert choose("save", "t", "", None, bus(made, "cancelled")) is None  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("answer", ["missing", "failed"])
def test_no_portal_or_a_failing_one_is_told_so_qt_can_ask_instead(
    qtbot: QtBot, made: list[Bus], answer: str
) -> None:
    with pytest.raises(PortalUnavailableError):
        choose("save", "t", "", None, bus(made, answer))  # pyright: ignore[reportArgumentType]
