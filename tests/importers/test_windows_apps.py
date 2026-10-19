"""Notes from Windows apps, read from databases those apps wrote (in a test VM)."""

import shutil
from pathlib import Path

import pytest

from stickle.core.colors import Rgb
from stickle.core.layout import Rect
from stickle.importers import sticky_notes, zhorn
from stickle.importers.base import ImportResult, SourceError, nearest_color
from stickle.importers.rtf import rtf_to_markdown
from stickle.importers.sources import recognise

FIXTURES = Path(__file__).parent / "fixtures"
PLUM = FIXTURES / "sticky-notes-6.1.4" / "plum.sqlite"
ZHORN = FIXTURES / "zhorn-stickies-10.2a" / "stickies.db"


def copied(source: Path, tmp_path: Path) -> Path:
    """The fixture and its companion files, in a folder of its own."""
    for file in source.parent.iterdir():
        shutil.copyfile(file, tmp_path / file.name)
    return tmp_path / source.name


def snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}


def test_sticky_notes_with_their_formatting_colours_and_places(tmp_path: Path) -> None:
    result = sticky_notes.read(copied(PLUM, tmp_path))

    shopping, todo, signs = result.notes
    assert shopping.body == "장보기 목록\n**Milk**and\n*slanted*end"
    assert (shopping.color, shopping.place) == ("yellow", Rect(969, 256, 320, 320))
    assert todo.body == "Todo\n- one\n- two\n\nunder\nstrike"
    assert todo.color == "pink"
    # Characters that would be Markdown stay what they were: plain text.
    assert signs.body == r"back\\slash {brace} \*star\* \[x\] =\=hi=\= # hash"
    assert signs.color == "blue"
    assert set(result.dropped) == {"underline", "strikethrough"}
    assert not any(note.hidden for note in result.notes)


def test_the_newest_notes_are_read_from_the_apps_log(tmp_path: Path) -> None:
    """The app was running: its newest notes were only in plum.sqlite-wal."""
    path = copied(PLUM, tmp_path)
    assert path.with_name("plum.sqlite-wal").stat().st_size > path.stat().st_size

    assert len(sticky_notes.read(path).notes) == 3


def test_reading_changes_none_of_the_apps_files(tmp_path: Path) -> None:
    for fixture in (PLUM, ZHORN):
        folder = tmp_path / fixture.parent.name
        folder.mkdir()
        path = copied(fixture, folder)
        before = snapshot(folder)

        (sticky_notes if fixture == PLUM else zhorn).read(path)

        assert snapshot(folder) == before


def test_zhorn_notes_with_title_korean_and_their_state(tmp_path: Path) -> None:
    result = zhorn.read(copied(ZHORN, tmp_path))

    welcome, meeting = result.notes
    assert welcome.hidden  # closed in Stickies
    assert welcome.body.startswith("Welcome to Stickies v10.2a!\nI hope you find it useful.")
    assert meeting.body == "# Meeting\n회의 메모 back\\\\slash {x}\n**Bold**plain\n*Italic*"
    assert meeting.always_on_top and not meeting.hidden
    assert meeting.color == "yellow"
    assert meeting.source_color == Rgb(255, 255, 180)
    assert meeting.place == Rect(610, 414, 410, zhorn.AUTO_HEIGHT)


def test_other_files_are_errors_not_crashes(tmp_path: Path) -> None:
    text = tmp_path / "plum.sqlite"
    text.write_text("not a database", "utf-8")

    with pytest.raises(SourceError):
        sticky_notes.read(text)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SourceError):
        zhorn.read(copied(PLUM, other))  # a database, but not Stickies'


def test_they_are_found_where_the_apps_keep_them(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")}
    assert sticky_notes.find(tmp_path, env) is None and zhorn.find(tmp_path, env) is None

    state = tmp_path / "local" / "Packages" / sticky_notes.PACKAGE / "LocalState"
    state.mkdir(parents=True)
    copied(PLUM, state)
    (tmp_path / "roaming" / "stickies").mkdir(parents=True)
    copied(ZHORN, tmp_path / "roaming" / "stickies")

    assert sticky_notes.find(tmp_path, env) is not None
    assert zhorn.find(tmp_path, env) is not None


def test_their_files_are_recognised_when_chosen(tmp_path: Path) -> None:
    plum = copied(PLUM, tmp_path)
    picked = recognise(plum.with_name("plum.sqlite-wal"))
    assert picked is not None and picked[0].name == sticky_notes.SOURCE
    assert picked[1].path == plum

    folder = tmp_path / "z"
    folder.mkdir()
    picked = recognise(copied(ZHORN, folder))
    assert picked is not None and picked[0].name == zhorn.SOURCE


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        (Rgb(255, 255, 180), "yellow"),  # Stickies' default
        (Rgb(255, 228, 241), "pink"),  # pale, once taken for grey
        (Rgb(226, 241, 255), "blue"),
        (Rgb(192, 240, 220), "mint"),
        (Rgb(255, 255, 255), "white"),
        (Rgb(105, 105, 105), "gray"),
    ],
)
def test_colours_are_matched_by_hue(color: Rgb, expected: str) -> None:
    assert nearest_color(color) == expected


U = "\\" + "u"  # RTF's \uN, written in two pieces so no tool reads it as an escape


def test_rtf_lists_unicode_and_code_pages() -> None:
    rtf = (
        r"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0\fswiss Arial;}{\f1\fnil\fcharset129 Gulim;}"
        r"{\f2\fnil\fcharset2 Symbol;}}{\*\generator Riched20}\uc1 "
        r"\pard{\pntext\f2\'B7\tab}{\*\pn\pnlvlblt\pnf2{\pntxtb\'B7}}\f0 first\par"
        r"{\pntext\f2\'B7\tab}second \b bold\b0\par"
        r"\pard{\pntext\f0 1.\tab}one\par"
        rf"\pard\f1\'c7\'d1\f0  and {U}54620?{U}44544? \ul under\ulnone"
        r"{\pict\pngblip 89504e}\par}"
    )
    result = ImportResult()

    assert rtf_to_markdown(rtf, result) == "- first\n- second **bold**\n1. one\n한 and 한글 under"
    assert set(result.dropped) == {"underline", "pictures"}
