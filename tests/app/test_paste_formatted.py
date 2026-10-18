"""Pasting into a note: formatted text keeps what a note can show."""

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from stickle.app.note_window import NoteWindow
from stickle.app.notes import clipboard_text


def copied(html: str | None, plain: str) -> QMimeData:
    data = QMimeData()
    if html is not None:
        data.setHtml(html)
    data.setText(plain)
    return data


def test_a_web_page_pastes_as_markdown(qtbot: QtBot) -> None:
    window = NoteWindow(text="")
    qtbot.addWidget(window)

    window.editor.insertFromMimeData(
        copied("<h2>할 일</h2><ul><li><b>우유</b></li><li>빵</li></ul>", "할 일\n우유\n빵")
    )

    assert window.text == "## 할 일\n\n- **우유**\n- 빵"
    window.release()


def test_one_undo_takes_the_whole_paste_back(qtbot: QtBot) -> None:
    window = NoteWindow(text="앞 ")
    qtbot.addWidget(window)
    cursor = window.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    window.editor.setTextCursor(cursor)

    window.editor.insertFromMimeData(copied("<p><i>기울임</i> 글</p>", "기울임 글"))
    assert window.text == "앞 *기울임* 글"
    window.editor.undo()

    assert window.text == "앞 "
    window.release()


def test_plain_text_pastes_as_it_is_without_nul(qtbot: QtBot) -> None:
    window = NoteWindow(text="")
    qtbot.addWidget(window)

    window.editor.insertFromMimeData(copied(None, "**마크다운** 그대로\x00"))

    assert window.text == "**마크다운** 그대로"
    window.release()


def test_a_note_from_the_clipboard_keeps_formatting(qtbot: QtBot) -> None:
    del qtbot
    QGuiApplication.clipboard().setMimeData(copied("<p><b>굵게</b> 글</p>", "굵게 글"))

    assert clipboard_text() == "**굵게** 글"
