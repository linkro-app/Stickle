"""The colours the user adds to the palette: kept one row each, in their order,
twelve shown at most, and notes of a colour removed left as they are."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.core.colors import Rgb, background_of, is_color, is_hex, needs_moving, note_colors
from stickle.data.colors import MAX_OWN_COLORS, ColorRepository, OwnColorsFullError
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import DEFAULT_NOTE_COLOR, LAST_USED_COLOR, Settings

KEY = secrets.token_bytes(32)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def hexes(colors: ColorRepository) -> list[str]:
    return [color.hex for color in colors.colors()]


def test_added_in_order_moved_and_removed(connection: apsw.Connection) -> None:
    colors = ColorRepository(connection)
    first, second = colors.add("#a3c9f0"), colors.add("#2b2b40")
    third = colors.add("#ffffff")

    assert hexes(colors) == ["#a3c9f0", "#2b2b40", "#ffffff"]
    colors.move(third.id, -1)
    assert hexes(colors) == ["#a3c9f0", "#ffffff", "#2b2b40"]
    colors.move(first.id, -1)  # first already
    assert hexes(colors)[0] == "#a3c9f0"
    colors.remove(second.id)
    assert hexes(colors) == ["#a3c9f0", "#ffffff"]
    row = connection.execute("SELECT deleted_at FROM custom_colors WHERE id = ?", (second.id,))
    assert row.fetchall()[0][0] is not None  # kept, marked: sync learns it went


def test_the_same_colour_is_kept_once(connection: apsw.Connection) -> None:
    colors = ColorRepository(connection)
    added = colors.add("#a3c9f0")

    assert colors.add("#a3c9f0") == added
    # Added on two devices, then brought together: two rows, shown once.
    connection.execute(
        "INSERT INTO custom_colors (id, hex, position, updated_at)"
        " VALUES ('other', '#a3c9f0', 9, '2026-10-26T00:00:00.000Z')"
    )
    assert hexes(colors) == ["#a3c9f0"]


def test_twelve_are_added_at_most(connection: apsw.Connection) -> None:
    colors = ColorRepository(connection)
    for n in range(MAX_OWN_COLORS):
        colors.add(f"#0000{n:02x}")

    with pytest.raises(OwnColorsFullError):
        colors.add("#ffffff")
    assert colors.add("#000003").hex == "#000003"  # one there already: no room needed


def test_more_brought_together_from_two_devices_all_show(connection: apsw.Connection) -> None:
    colors = ColorRepository(connection)
    for n in range(MAX_OWN_COLORS):
        colors.add(f"#0000{n:02x}")
    for n, value in enumerate(("#ffffff", "#eeeeee")):
        connection.execute(
            "INSERT INTO custom_colors (id, hex, position, updated_at)"
            " VALUES (?, ?, ?, '2026-10-26T00:00:00.000Z')",
            (f"other {n}", value, 99 + n),
        )

    shown = hexes(colors)
    assert len(shown) == MAX_OWN_COLORS + 2 and shown[-2:] == ["#ffffff", "#eeeeee"]
    with pytest.raises(OwnColorsFullError):
        colors.add("#dddddd")
    for color in colors.colors()[:3]:  # down to eleven: room for one again
        colors.remove(color.id)
    assert colors.add("#dddddd").hex == "#dddddd"
    with pytest.raises(OwnColorsFullError):
        colors.add("#cccccc")
    order = colors.colors()
    moved = colors.move(order[-1].id, -1)  # the last ones move as the others do
    assert [c.hex for c in moved][-2:] == ["#dddddd", "#eeeeee"]


@pytest.mark.parametrize("value", ["#A3C9F0", "a3c9f0", "#a3c9f", "#a3c9f0 ", "yellow", ""])
def test_only_lower_case_rrggbb_is_a_colour_of_ones_own(
    connection: apsw.Connection, value: str
) -> None:
    assert not is_hex(value)
    with pytest.raises(ValueError):
        ColorRepository(connection).add(value)


def test_a_note_keeps_its_colour_when_the_colour_is_removed(
    connection: apsw.Connection,
) -> None:
    colors, notes = ColorRepository(connection), NoteRepository(connection)
    own = colors.add("#a3c9f0")
    note = notes.create("하늘색 메모", "#a3c9f0")

    colors.remove(own.id)

    stored = notes.get(note.id)
    assert stored is not None and stored.color == "#a3c9f0"
    assert note_colors(stored.color).background == Rgb.from_hex("#a3c9f0")


def test_the_colour_last_chosen_may_be_ones_own(connection: apsw.Connection) -> None:
    settings = Settings(connection)
    settings.set(DEFAULT_NOTE_COLOR, "#a3c9f0")
    settings.set(LAST_USED_COLOR, "#a3c9f0")

    assert settings.get(DEFAULT_NOTE_COLOR) == "#a3c9f0"
    assert settings.get(LAST_USED_COLOR) == "#a3c9f0"


def test_colours_of_ones_own_are_drawn_as_the_palettes_are() -> None:
    assert is_color("yellow") and is_color("#a3c9f0") and not is_color("#A3C9F0")
    assert background_of("#a3c9f0") == Rgb.from_hex("#a3c9f0")
    assert background_of("#zzzzzz") == background_of("yellow")  # unknown: the default
    assert not needs_moving(Rgb.from_hex("#ffec8c"))  # dark text reads on it as it is
    # Mid grey: neither dark nor light text reaches 4.5:1, so it is drawn a
    # little lighter or darker than chosen.
    assert needs_moving(Rgb.from_hex("#808080"))
    assert note_colors("#808080").background != Rgb.from_hex("#808080")
