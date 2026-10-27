from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from stickle.app import fonts


def test_system_font_comes_first(qtbot: QtBot, tmp_path: Path) -> None:
    fallback = tmp_path / "fallback.otf"
    fallback.write_bytes(b"not used")
    if not fonts.korean_is_drawable():
        pytest.skip("this system has no Korean font")

    assert fonts.ensure_korean_font(fallback) is None


def test_missing_fallback_file_is_harmless(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fonts, "korean_is_drawable", lambda: False)

    assert fonts.ensure_korean_font(tmp_path / "absent.otf") is None
