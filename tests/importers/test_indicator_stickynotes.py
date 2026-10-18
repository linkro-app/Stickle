"""Indicator Stickynotes' file, as the app writes it (stickynotes/backend.py, gui.py)."""

import json
from pathlib import Path

import pytest

from stickle.importers.base import Rect, SourceError
from stickle.importers.indicator_stickynotes import find, read

DATA: dict[str, object] = {
    "notes": [
        {
            "uuid": "1b2f9b3e-0000-4000-8000-000000000001",
            "body": "회의 준비\n- 자료\n",
            "last_modified": "2026-10-01T09:30:00",
            "properties": {"position": [100, 200], "size": [250, 180], "locked": True},
            "cat": "c-work",
        },
        {
            "uuid": "1b2f9b3e-0000-4000-8000-000000000002",
            "body": "기본 분류",
            "last_modified": "2026-10-02T10:00:00",
            "properties": {},
            "cat": "",
        },
        {"body": "uuid 없는 메모"},
    ],
    "categories": {
        "c-work": {
            "name": "회사",
            "bgcolor_hsv": [210 / 360, 0.25, 1.0],
            "textcolor": [0.1, 0.1, 0.1],
            "font": "Ubuntu 12",
        },
        "c-home": {"name": "집", "bgcolor_hsv": [150 / 360, 0.25, 0.94]},
    },
    "properties": {"default_cat": "c-home", "all_visible": True},
}


def write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return path


def test_notes_come_with_their_category_colour_and_place(tmp_path: Path) -> None:
    result = read(write(tmp_path / "indicator-stickynotes", DATA))

    work, plain = result.notes
    assert work.body == "회의 준비\n- 자료\n"
    assert work.category == "회사"
    assert work.locked
    assert work.place == Rect(100, 200, 250, 180)
    assert work.color in ("blue", "sky")
    assert plain.category is None  # the default category's look, not its name
    assert plain.color == "mint"
    assert plain.place is None
    assert result.unreadable == 1
    assert set(result.dropped) == {"fonts", "text colours"}


def test_with_no_category_at_all_the_apps_own_yellow(tmp_path: Path) -> None:
    data: dict[str, object] = {"notes": [{"uuid": "x", "body": "글", "properties": {}}]}

    (note,) = read(write(tmp_path / "f", data)).notes

    assert note.color in ("yellow", "apricot")


def test_a_damaged_file_is_an_error_not_a_crash(tmp_path: Path) -> None:
    damaged = tmp_path / "indicator-stickynotes"
    damaged.write_text('{"notes": [', "utf-8")

    with pytest.raises(SourceError):
        read(damaged)


def test_odd_values_are_left_out_rather_than_trusted(tmp_path: Path) -> None:
    data: dict[str, object] = {
        "notes": [
            {
                "uuid": "x",
                "body": "글",
                "properties": {"position": "here", "size": [0, 0], "locked": "yes"},
                "cat": 5,
            }
        ],
        "categories": [],
    }

    (note,) = read(write(tmp_path / "f", data)).notes

    assert note.place is None and not note.locked and note.category is None


def test_it_is_found_where_the_app_keeps_it(tmp_path: Path) -> None:
    assert find(tmp_path) is None
    write(tmp_path / ".config" / "indicator-stickynotes", DATA)
    found = find(tmp_path)
    assert found is not None and found.path.name == "indicator-stickynotes"
