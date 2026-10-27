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


def test_the_emoji_font_is_added_only_when_the_alarm_clock_is_not_drawn(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    font = tmp_path / "emoji.ttf"
    font.write_bytes(b"not read")
    added: list[Path] = []

    def add(path: Path) -> str:
        added.append(path)
        return "Noto Emoji"

    monkeypatch.setattr(fonts, "_add_fallback", add)

    monkeypatch.setattr(fonts, "alarm_clock_is_drawable", lambda: True)
    assert fonts.ensure_alarm_clock_font(font) is None
    assert fonts.ensure_alarm_clock_font(tmp_path / "absent.ttf") is None

    monkeypatch.setattr(fonts, "alarm_clock_is_drawable", lambda: False)
    assert fonts.ensure_alarm_clock_font(tmp_path / "absent.ttf") is None
    assert fonts.ensure_alarm_clock_font(font) == "Noto Emoji"
    assert added == [font]


def test_a_glyph_that_draws_nothing_is_not_drawable(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fedora's colour emoji font has the alarm clock in a form Qt draws as nothing.
    def yes(_text: str) -> bool:
        return True

    def no(_text: str) -> bool:
        return False

    monkeypatch.setattr(fonts, "has_glyphs", yes)
    monkeypatch.setattr(fonts, "draws", no)

    assert not fonts.alarm_clock_is_drawable()
