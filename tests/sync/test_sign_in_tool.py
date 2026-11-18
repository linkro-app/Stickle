"""The tool that signs a test account in for the real OneDrive tests
(sign_in_onedrive.py), against Microsoft's sign-in in the test."""

import json
import sys
import threading
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from graph_server import ACCOUNT, CLIENT_ID, Graph, back, graph_server, to_sign_in_page
from real_onedrive import VARIABLE
from sign_in_onedrive import main

from stickle.cloud_apps import BUILT_IN, ONEDRIVE_VARIABLE


@pytest.fixture
def graph() -> Generator[Graph]:
    with graph_server() as serving:
        yield serving


@pytest.fixture
def file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    named = tmp_path / "accounts" / "onedrive.json"
    monkeypatch.setenv(ONEDRIVE_VARIABLE, CLIENT_ID)
    monkeypatch.setenv(VARIABLE, str(named))
    monkeypatch.delitem(sys.modules, BUILT_IN, raising=False)
    return named


def browser(graph: Graph, opened: list[threading.Thread]) -> Callable[[str], bool]:
    """Opens the address as a browser would, the user signing in at once."""

    def open_browser(address: str) -> bool:
        going = threading.Thread(target=lambda: back(to_sign_in_page(graph, address)))
        going.start()
        opened.append(going)
        return True

    return open_browser


def test_the_file_is_written_with_the_apps_id_and_the_refresh_token(
    graph: Graph, file: Path
) -> None:
    said: list[str] = []
    opened: list[threading.Thread] = []
    done = main(
        open_browser=browser(graph, opened),
        place=graph.place,
        public=graph.context,
        say=said.append,
    )
    for each in opened:
        each.join(10)
    assert done == 0
    written = json.loads(file.read_text("utf-8"))
    assert set(written) == {"client_id", "refresh_token"}
    assert written["client_id"] == CLIENT_ID
    assert written["refresh_token"] in graph.refresh_tokens
    assert [each.name for each in file.parent.iterdir()] == ["onedrive.json"]  # no .new left
    for secret in (written["refresh_token"], ACCOUNT, *graph.issued):
        assert not any(secret in line for line in said)


def test_without_what_it_needs_it_does_nothing(
    graph: Graph, file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(VARIABLE)
    said: list[str] = []
    assert main(place=graph.place, public=graph.context, say=said.append) == 2
    assert said and ONEDRIVE_VARIABLE in said[0] and VARIABLE in said[0]
    assert graph.asked == []


def test_a_sign_in_said_no_to_writes_nothing(graph: Graph, file: Path) -> None:
    graph.declining = True
    said: list[str] = []
    opened: list[threading.Thread] = []
    done = main(
        open_browser=browser(graph, opened),
        place=graph.place,
        public=graph.context,
        say=said.append,
    )
    for each in opened:
        each.join(10)
    assert done == 1 and said[-1] == "Stickle was not allowed."
    assert not file.exists()


def test_no_sign_in_in_time(graph: Graph, file: Path) -> None:
    said: list[str] = []
    done = main(
        open_browser=lambda address: False,
        place=graph.place,
        public=graph.context,
        waiting=0.2,
        say=said.append,
    )
    assert done == 1 and said[-1].startswith("No sign-in")
    assert "No browser opened" in said[0]
    assert not file.exists()
