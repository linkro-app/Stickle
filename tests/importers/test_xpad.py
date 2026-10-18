"""xpad's notes, written as xpad writes them (src/xpad-pad.c, src/fio.c)."""

from pathlib import Path

import pytest

from stickle.core.colors import Rgb
from stickle.importers.base import Rect, SourceError
from stickle.importers.xpad import find, read

T = ""


def pad(folder: Path, number: str, info: str, content: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"info-{number}").write_text(info + f"\ncontent content-{number}\n", "utf-8")
    (folder / f"content-{number}").write_text(content, "utf-8")


XPAD5 = """width 200
height 180
x 640
y 120
follow_font 1
follow_color 0
sticky 0
hidden 0
back rgb(255,192,203)
text rgb(0,0,0)
fontname Sans 10"""

XPAD4 = """width 160
height 140
x 10
y 20
follow_font 0
follow_color 0
sticky 1
hidden 1
back_red 49152
back_green 61440
back_blue 56320
text_red 0
text_green 0
text_blue 0
fontname Serif Bold 12"""


def test_notes_come_with_their_place_colour_and_formatting(tmp_path: Path) -> None:
    folder = tmp_path / ".config" / "xpad"
    pad(
        folder,
        "AAAAAA",
        XPAD5,
        f"장보기\n{T}bold{T}우유{T}/bold{T} 그리고 {T}italic{T}빵{T}/italic{T}\n",
    )
    pad(folder, "BBBBBB", XPAD4, f"{T}underline{T}밑줄{T}/underline{T} 끝")

    result = read(folder)

    first, second = result.notes
    assert first.source_id == "info-AAAAAA"
    assert first.body == "장보기\n**우유** 그리고 *빵*"
    assert first.source_color == Rgb(255, 192, 203)
    assert first.color == "pink"
    assert first.place == Rect(640, 120, 200, 180)
    assert not first.hidden
    assert second.body == "밑줄 끝"
    assert second.hidden
    assert second.source_color == Rgb(192, 240, 220)
    assert second.color == "mint"
    assert set(result.dropped) == {"underline", "fonts", "text colours"}


def test_following_the_theme_means_the_default_yellow(tmp_path: Path) -> None:
    folder = tmp_path / "xpad"
    pad(folder, "CCCCCC", XPAD5.replace("follow_color 0", "follow_color 1"), "글")

    (note,) = read(folder).notes

    assert note.color == "yellow"
    assert note.source_color is None


def test_a_pad_without_its_content_is_counted_not_fatal(tmp_path: Path) -> None:
    folder = tmp_path / "xpad"
    pad(folder, "DDDDDD", XPAD5, "남은 메모")
    (folder / "info-EEEEEE").write_text(XPAD5 + "\ncontent content-EEEEEE\n", "utf-8")
    (folder / "info-FFFFFF").write_text(XPAD5 + "\ncontent ../../etc/passwd\n", "utf-8")

    result = read(folder)

    assert [note.body for note in result.notes] == ["남은 메모"]
    assert result.unreadable == 2


def test_reading_changes_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "xpad"
    pad(folder, "AAAAAA", XPAD5, "그대로")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}

    read(folder)

    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()} == before


def test_it_is_found_where_xpad_keeps_it(tmp_path: Path) -> None:
    assert find(tmp_path, {}) is None
    pad(tmp_path / ".xpad", "AAAAAA", XPAD4, "옛 폴더")
    found = find(tmp_path, {})
    assert found is not None and found.path == tmp_path / ".xpad"
    pad(tmp_path / "cfg" / "xpad", "AAAAAA", XPAD5, "새 폴더")
    found = find(tmp_path, {"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    assert found is not None and found.path == tmp_path / "cfg" / "xpad"


def test_no_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        read(tmp_path / "missing")
