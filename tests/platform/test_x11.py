"""Keeping notes out of the taskbar under X11.

What the window manager makes of it was checked against Mutter on Xvfb; here,
only that a missing X server costs nothing but the effect.
"""

import logging

import pytest

from stickle.platform.linux import x11


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x11, "_xcb", None)
    monkeypatch.setattr(x11, "_unavailable", False)


def test_without_an_x_server_nothing_fails_and_it_is_tried_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    def no_server() -> x11._Xcb:  # pyright: ignore[reportPrivateUsage]
        attempts.append(1)
        raise OSError("cannot connect to the X server")

    monkeypatch.setattr(x11, "_Xcb", no_server)

    with caplog.at_level(logging.WARNING):
        x11.keep_off_taskbar(0x400001, mapped=False)
        x11.keep_off_taskbar(0x400001, mapped=True)

    assert attempts == [1]
    assert "cannot reach the X server" in caplog.text


def test_activating_without_an_x_server_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_server() -> x11._Xcb:  # pyright: ignore[reportPrivateUsage]
        raise OSError("cannot connect to the X server")

    monkeypatch.setattr(x11, "_Xcb", no_server)

    x11.activate(0x400001)  # no exception


def test_activation_is_asked_for_as_a_taskbar_would(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int, list[int]]] = []

    def atom(name: bytes) -> int:
        return 500 if name == b"_NET_ACTIVE_WINDOW" else 0

    def send(window: int, kind: int, data: list[int]) -> None:
        sent.append((window, kind, data))

    server = x11._Xcb.__new__(x11._Xcb)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(server, "_atom", atom)
    monkeypatch.setattr(server, "_send", send)
    monkeypatch.setattr(x11, "_xcb", server)

    x11.activate(0x400001)

    assert sent == [(0x400001, 500, [x11.SOURCE_PAGER, 0, 0, 0, 0])]


class FakeServer:
    def __init__(self, states: list[int]) -> None:
        self.state = 300
        self.skip = [301, 302]
        self.states = {0x400001: states}
        self.asked: list[list[int]] = []

    def atoms(self, window: int, prop: int) -> list[int]:
        assert prop == self.state
        return list(self.states.get(window, []))

    def set_atoms(self, window: int, prop: int, atoms: list[int]) -> None:
        self.states[window] = atoms

    def ask_window_manager(self, window: int, atoms: list[int]) -> None:
        self.asked.append(atoms)


def test_before_mapping_the_states_are_added_to_those_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(states=[299])
    monkeypatch.setattr(x11, "_xcb", server)

    x11.keep_off_taskbar(0x400001, mapped=False)

    assert server.states[0x400001] == [299, 301, 302]
    assert server.asked == []


def test_once_mapped_the_window_manager_is_asked_for_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServer(states=[301])
    monkeypatch.setattr(x11, "_xcb", server)

    x11.keep_off_taskbar(0x400001, mapped=True)

    assert server.asked == [[302]]
    assert server.states[0x400001] == [301]  # a mapped window's state is the WM's


def test_nothing_is_sent_when_the_states_are_there(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(states=[302, 301])
    monkeypatch.setattr(x11, "_xcb", server)

    x11.keep_off_taskbar(0x400001, mapped=True)
    x11.keep_off_taskbar(0x400001, mapped=False)

    assert server.asked == []
    assert server.states[0x400001] == [302, 301]


class Recording:
    """libxcb as the calls made to it: what each returns, None (no reply)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        def call(*_args: object) -> None:
            self.calls.append(name)

        return call


def test_the_state_written_before_mapping_is_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # Qt reads the state on its own connection as it maps the window, and writes
    # it back merged: read before the server had these, they were lost.
    server = x11._Xcb.__new__(x11._Xcb)  # pyright: ignore[reportPrivateUsage]
    library = Recording()
    monkeypatch.setattr(server, "_xcb", library, raising=False)
    monkeypatch.setattr(server, "_connection", None, raising=False)

    server.set_atoms(0x400001, 300, [301, 302])

    assert library.calls == [
        "xcb_change_property",
        "xcb_flush",
        "xcb_get_property",
        "xcb_get_property_reply",  # answered: the change before it was handled
    ]


def test_states_asked_for_only_once_mapped_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(states=[301])
    monkeypatch.setattr(x11, "_xcb", server)

    with caplog.at_level(logging.INFO):
        x11.keep_off_taskbar(0x400001, mapped=True)
        x11.keep_off_taskbar(0x400001, mapped=False)  # before mapping: as it should be

    assert caplog.text.count("only once mapped") == 1
