"""Opening, saving and choosing a folder: the system's dialog, else Qt's, and
never one that writes to the system's settings store."""

import re
import sys
from pathlib import Path
from typing import Literal

import pytest
from PySide6.QtWidgets import QFileDialog, QWidget
from pytestqt.qtbot import QtBot

import stickle
from stickle.app import file_dialogs
from stickle.app.file_dialogs import (
    default_extension,
    existing_folder,
    file_types,
    open_file_name,
    save_file_name,
)
from stickle.app.recovery_key_dialog import RecoveryKeyPanel

RECOVERY_KEY = "ABCD-EFGH-JKMN-PQRS-TVWX-YZ01-2345-6789"
type Answer = Path | Literal["unavailable"] | None


class System:
    """Stands in for the system's dialog: records what was asked, answers as set."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
        self.answer: Answer = answer
        self.asked: list[tuple[str, str, Path | None, str]] = []
        self.open_for: list[QWidget | None] = []
        monkeypatch.setattr(file_dialogs, "from_the_system", self.ask)

    def ask(
        self, _parent: QWidget, kind: str, title: str, suggested: Path | None, filters: str
    ) -> Answer:
        self.asked.append((kind, title, suggested, filters))
        self.open_for.append(file_dialogs.asking_for())
        return self.answer


class Qt:
    """Stands in for Qt's own dialogs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
        self.answer = answer
        self.options: list[QFileDialog.Option] = []

        def file(*_args: object, options: QFileDialog.Option) -> tuple[str, str]:
            self.options.append(options)
            return self.answer, ""

        def folder(*_args: object, options: QFileDialog.Option) -> str:
            self.options.append(options)
            return self.answer

        monkeypatch.setattr(QFileDialog, "getSaveFileName", file)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", file)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", folder)


def test_the_recovery_key_is_saved_where_the_system_dialog_says(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = System(monkeypatch, tmp_path / "key.txt")
    qt = Qt(monkeypatch, "")
    panel = RecoveryKeyPanel(RECOVERY_KEY)
    qtbot.addWidget(panel)

    panel._save()  # pyright: ignore[reportPrivateUsage]

    assert RECOVERY_KEY in (tmp_path / "key.txt").read_text(encoding="utf-8")
    kind, _title, suggested, filters = system.asked[0]
    assert kind == "save" and suggested is not None and suggested.name.endswith(".txt")
    assert "*.txt" in filters
    assert qt.options == []


def test_cancelled_nothing_is_saved(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    System(monkeypatch, None)
    qt = Qt(monkeypatch, str(tmp_path / "never.txt"))
    panel = RecoveryKeyPanel(RECOVERY_KEY)
    qtbot.addWidget(panel)

    panel._save()  # pyright: ignore[reportPrivateUsage]

    assert list(tmp_path.iterdir()) == []
    assert qt.options == []  # cancelled is an answer: Qt's dialog is not asked instead


def test_without_the_systems_dialog_qts_is_shown(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    System(monkeypatch, "unavailable")
    qt = Qt(monkeypatch, str(tmp_path / "chosen"))
    parent = QWidget()
    qtbot.addWidget(parent)

    assert save_file_name(parent, "t", tmp_path / "a.txt", "*.txt") == tmp_path / "chosen"
    assert open_file_name(parent, "t") == tmp_path / "chosen"
    assert existing_folder(parent, "t") == tmp_path / "chosen"
    assert len(qt.options) == 3


def test_on_linux_qts_dialog_is_its_own_not_gtks(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GTK's dialog keeps the folders it showed in GSettings.
    System(monkeypatch, "unavailable")
    qt = Qt(monkeypatch, str(tmp_path / "chosen"))
    parent = QWidget()
    qtbot.addWidget(parent)
    monkeypatch.setattr(file_dialogs.sys, "platform", "linux")

    save_file_name(parent, "t", tmp_path / "a.txt", "*.txt")
    existing_folder(parent, "t")

    assert all(options & QFileDialog.Option.DontUseNativeDialog for options in qt.options)


def test_the_window_asked_for_is_known_while_the_dialog_is_open(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = System(monkeypatch, tmp_path)
    parent = QWidget()
    qtbot.addWidget(parent)

    existing_folder(parent, "t")

    assert system.open_for == [parent]
    assert file_dialogs.asking_for() is None


def test_qts_file_types_become_the_systems() -> None:
    assert file_types("Text files (*.txt)") == [("Text files (*.txt)", "*.txt")]
    assert file_types("Notes (*.md *.json);;All (*)") == [
        ("Notes (*.md *.json)", "*.md;*.json"),
        ("All (*)", "*"),
    ]
    assert file_types("*.txt") == [("*.txt", "*.txt")]
    assert file_types("") == []
    assert default_extension("Text files (*.txt);;Notes (*.md)") == "txt"
    assert default_extension("All (*)") == ""
    assert default_extension("") == ""


def test_elsewhere_qts_dialog_is_the_one(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_dialogs.sys, "platform", "darwin")
    parent = QWidget()
    qtbot.addWidget(parent)

    assert file_dialogs._from_the_system(parent, "save", "t", None, "") == "unavailable"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own dialog")
def test_on_windows_the_systems_dialog_is_asked_for_directly(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":  # also tells the type checker this is Windows
        return
    from stickle.platform.windows import file_dialogs as windows

    asked: list[tuple[object, ...]] = []

    def choose(*args: object) -> Path:
        asked.append(args)
        return tmp_path / "key.txt"

    monkeypatch.setattr(windows, "choose", choose)
    qt = Qt(monkeypatch, "")
    parent = QWidget()
    qtbot.addWidget(parent)

    chosen = save_file_name(parent, "저장", tmp_path / "key.txt", "Text files (*.txt)")

    assert chosen == tmp_path / "key.txt"
    kind, title, _window, suggested, types, extension = asked[0]
    assert (kind, title, suggested) == ("save", "저장", tmp_path / "key.txt")
    assert (types, extension) == ([("Text files (*.txt)", "*.txt")], "txt")
    assert qt.options == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own dialog")
def test_on_windows_a_failing_dialog_chooses_nothing_rather_than_qts(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        return
    from stickle.platform.windows import file_dialogs as windows

    def choose(*_args: object) -> Path:
        raise OSError("no dialog")

    monkeypatch.setattr(windows, "choose", choose)
    qt = Qt(monkeypatch, "C:/never")
    parent = QWidget()
    qtbot.addWidget(parent)

    assert existing_folder(parent, "t") is None
    assert qt.options == []  # Qt's would write to the registry


def test_qts_file_dialog_is_called_on_only_from_the_one_place() -> None:
    # It writes to the system's settings store each time it closes: only the
    # fallback for a Linux desktop without its own dialog may show it.
    source = Path(stickle.__file__).parent
    using = sorted(
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if re.search(r"\bQFileDialog\s*[.(]|import[^\n]*\bQFileDialog\b", path.read_text("utf-8"))
    )
    assert using == ["app/file_dialogs.py"]
