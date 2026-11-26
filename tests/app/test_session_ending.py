"""A logout or shutdown another program cancels: once it is over, what waits
to go to the cloud gets its time again when Stickle quits."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from stickle.app import application
from stickle.app.application import SessionEnding


@pytest.fixture
def session(qtbot: QtBot) -> Iterator[SessionEnding]:
    del qtbot  # the app made
    app = QGuiApplication.instance()
    assert isinstance(app, QGuiApplication)
    made = SessionEnding(app)
    yield made
    made.setParent(None)
    made.deleteLater()


def used_again() -> None:
    app = QGuiApplication.instance()
    assert isinstance(app, QGuiApplication)
    app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)


def test_asked_to_end_the_session_it_is_ending(session: SessionEnding) -> None:
    assert not session.ending
    session.asked()
    assert session.ending


def test_the_session_going_on_is_over_once_stickle_is_used_again(session: SessionEnding) -> None:
    session.asked()
    used_again()
    assert not session.ending


def test_the_session_going_on_is_over_a_while_after_with_stickle_still_running(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(application, "SESSION_GOES_ON_MS", 20)
    app = QGuiApplication.instance()
    assert isinstance(app, QGuiApplication)
    session = SessionEnding(app)
    try:
        session.asked()
        qtbot.waitUntil(lambda: not session.ending, timeout=2_000)
    finally:
        session.setParent(None)
        session.deleteLater()


def test_closing_for_an_update_is_ending_for_certain(session: SessionEnding) -> None:
    session.ends()
    used_again()
    assert session.ending
