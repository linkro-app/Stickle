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


def test_the_emoji_font_is_registered_only_where_the_alarm_clock_would_not_show(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    font = tmp_path / "emoji.ttf"
    font.write_bytes(b"not a font")
    monkeypatch.setattr(fonts, "_alarm_clock_family", None)

    monkeypatch.setattr(fonts, "alarm_clock_is_drawable", lambda: True)
    assert fonts.ensure_alarm_clock_font(font) is None
    monkeypatch.setattr(fonts, "alarm_clock_is_drawable", lambda: False)
    assert fonts.ensure_alarm_clock_font(tmp_path / "absent.ttf") is None
    assert fonts.ensure_alarm_clock_font(font) is None  # not a font: nothing to use
    assert fonts.alarm_clock_family() is None


def test_a_glyph_that_draws_nothing_does_not_show(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fedora's colour emoji font has the alarm clock in a form Qt draws as nothing.
    def yes(_text: str, _font: object = None) -> bool:
        return True

    def no(_text: str, _font: object = None) -> bool:
        return False

    monkeypatch.setattr(fonts, "has_glyphs", yes)
    monkeypatch.setattr(fonts, "draws", no)
    assert not fonts.alarm_clock_is_drawable()
