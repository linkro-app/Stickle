"""The tool that signs a test account in for the real Dropbox tests
(sign_in_dropbox.py), against Dropbox in the test."""

import json
import socket
import sys
import threading
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from dropbox_server import ACCOUNT, APP_KEY, DropboxState, dropbox_server
from real_dropbox import VARIABLE
from sign_in_dropbox import main
from test_dropbox_sign_in import back, shown_code, to_allow_page

from stickle.cloud_apps import BUILT_IN, DROPBOX_VARIABLE
from stickle.sync.targets.dropbox import REDIRECT_PORTS


@pytest.fixture
def box() -> Generator[DropboxState]:
    with dropbox_server() as serving:
        yield serving


@pytest.fixture
def file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    named = tmp_path / "accounts" / "dropbox.json"
    monkeypatch.setenv(DROPBOX_VARIABLE, APP_KEY)
    monkeypatch.setenv(VARIABLE, str(named))
    monkeypatch.delitem(sys.modules, BUILT_IN, raising=False)
    return named


def browser(box: DropboxState, opened: list[threading.Thread]) -> Callable[[str], bool]:
    """Opens the address as a browser would, the user allowing Stickle at once."""

    def open_browser(address: str) -> bool:
        going = threading.Thread(target=lambda: back(to_allow_page(box, address)[1]))
        going.start()
        opened.append(going)
        return True

    return open_browser


def test_the_file_is_written_with_the_apps_key_and_the_refresh_token(
    box: DropboxState, file: Path
) -> None:
    box.put_by_hand("vault.json", b"{}")
    said: list[str] = []
    opened: list[threading.Thread] = []
    done = main(
        open_browser=browser(box, opened),
        place=box.place,
        public=box.context,
        say=said.append,
    )
    for each in opened:
        each.join(10)
    assert done == 0
    written = json.loads(file.read_text("utf-8"))
    assert set(written) == {"app_key", "refresh_token"}
    assert written["app_key"] == APP_KEY
    assert written["refresh_token"] in box.refresh_tokens
    assert [each.name for each in file.parent.iterdir()] == ["dropbox.json"]  # no .new left
    assert said[-2:] == ["The account's name: told.", "The app's folder holds 1 file(s)."]
    for secret in (written["refresh_token"], ACCOUNT, APP_KEY, *box.issued):
        assert not any(secret in line for line in said)


def test_with_every_port_taken_the_code_is_entered(box: DropboxState, file: Path) -> None:
    held: list[socket.socket] = []
    for port in REDIRECT_PORTS:
        each = socket.socket()
        try:
            each.bind(("127.0.0.1", port))
            each.listen()
            held.append(each)
        except OSError:
            each.close()
    shown: list[str] = []
    typed = iter([lambda: "wrong-code-here", lambda: shown[0]])  # a wrong one first

    def open_browser(address: str) -> bool:
        shown.append(shown_code(box, address))
        return True

    def enter_code(_prompt: str) -> str:
        return next(typed)()

    said: list[str] = []
    try:
        done = main(
            open_browser=open_browser,
            enter_code=enter_code,
            place=box.place,
            public=box.context,
            say=said.append,
        )
    finally:
        for each in held:
            each.close()
    assert done == 0
    assert "That code was not taken. Try again." in said
    assert json.loads(file.read_text("utf-8"))["refresh_token"] in box.refresh_tokens
    assert not any(shown[0] in line for line in said)


def test_without_what_it_needs_it_does_nothing(
    box: DropboxState, file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VARIABLE)
    said: list[str] = []
    assert main(place=box.place, public=box.context, say=said.append) == 2
    assert said and DROPBOX_VARIABLE in said[0] and VARIABLE in said[0]
    assert box.asked == []


def test_a_sign_in_said_no_to_writes_nothing(box: DropboxState, file: Path) -> None:
    box.declining = True
    said: list[str] = []
    opened: list[threading.Thread] = []
    done = main(
        open_browser=browser(box, opened),
        place=box.place,
        public=box.context,
        say=said.append,
    )
    for each in opened:
        each.join(10)
    assert done == 1 and said[-1] == "Stickle was not allowed."
    assert not file.exists()


def test_no_sign_in_in_time(box: DropboxState, file: Path) -> None:
    said: list[str] = []
    done = main(
        open_browser=lambda address: False,
        place=box.place,
        public=box.context,
        waiting=0.2,
        say=said.append,
    )
    assert done == 1 and said[-1].startswith("No sign-in")
    assert "No browser opened" in said[0]
    assert not file.exists()
