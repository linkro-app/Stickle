"""Where a note is on screen and its size, as fields of its file (stickle.sync.
local), and those fields taken in (stickle.data.layouts keeps them here).

- "place:<n>", for n monitors: {"monitor": its number, "name": its name,
  "setup": the names of the monitors connected then, "x": , "y": where the
  note sits on it, as fractions of it} (stickle.core.layout.Place).
- "size": {"width": , "height": } in pixels (stickle.core.layout.Size).

Each changes on its own: moving a note on two monitors changes the place for
two alone, and resizing it the size alone, so that a laptop's moves and a
desk's two monitors do not take each other's place. A place this device only
gave a note never placed (stickle.data.layouts, arranged) goes nowhere: never
changed here, any other device's place for as many monitors comes in its
stead. A value of another shape (a newer version's) is left in the file and
not taken here.
"""

from collections.abc import Mapping

import apsw

from stickle.core.layout import Place, Size
from stickle.data import stamps
from stickle.data.layouts import PLACE, SIZE, LayoutRepository, count_of
from stickle.data.stamps import Stamp
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json

_PLACE_KEYS = ("monitor", "name", "setup", "x", "y")
_SIZE_KEYS = ("height", "width")


def is_layout_field(name: str) -> bool:
    return name == SIZE or (name.startswith(PLACE) and count_of(name) is not None)


def _place_json(place: Place) -> Json:
    return {
        "monitor": place.monitor,
        "name": place.name,
        "setup": list(place.setup),
        "x": place.rel_x,
        "y": place.rel_y,
    }


def values(connection: apsw.Connection, note_id: str) -> dict[str, Json]:
    """The note's places and size here, as their fields' values."""
    layouts = LayoutRepository(connection)
    found: dict[str, Json] = {
        f"{PLACE}{count}": _place_json(place) for count, place in layouts.places(note_id).items()
    }
    size = layouts.size(note_id)
    if size is not None:
        found[SIZE] = {"width": size.width, "height": size.height}
    return found


def fields(
    device: str,
    held: Mapping[str, Json],
    stamped: Mapping[str, Stamp],
    sent: Mapping[str, int],
    before: Contents,
) -> dict[str, Field]:
    """The note's places and size as fields (held: their values here), each as
    this device has it once it changed here or was taken in; else as before
    (one only arranged here goes nowhere)."""
    found: dict[str, Field] = {}
    for name, value in sorted(held.items()):
        stamp = stamped.get(name)
        if stamp is None:
            continue  # never changed here: as the file has it, if it has it
        when, by = stamp
        old = before.fields.get(name)
        if old is None:
            found[name] = Field(value, when, by or device, sent=sent.get(name))
        else:
            found[name] = Field(value, when, by or device, old.readonly, old.others, sent.get(name))
    return found


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def place_of(value: Json) -> Place | None:
    """A place field's value as a place, or None for one of another shape."""
    if not isinstance(value, dict) or tuple(sorted(value)) != _PLACE_KEYS:
        return None
    monitor, name, setup, x, y = (value[key] for key in _PLACE_KEYS)
    if not (_int(monitor) and isinstance(name, str) and isinstance(setup, list)):
        return None
    if not (_number(x) and _number(y)) or not all(isinstance(each, str) for each in setup):
        return None
    place = Place(
        monitor=int(monitor),  # pyright: ignore[reportArgumentType]
        name=name,
        rel_x=float(x),  # pyright: ignore[reportArgumentType]
        rel_y=float(y),  # pyright: ignore[reportArgumentType]
        setup=tuple(str(each) for each in setup),
    )
    return place if place.is_valid() else None


def size_of(value: Json) -> Size | None:
    """A size field's value as a size, or None for one of another shape."""
    if not isinstance(value, dict) or tuple(sorted(value)) != _SIZE_KEYS:
        return None
    width, height = value["width"], value["height"]
    if not (_int(width) and _int(height)):
        return None
    size = Size(int(width), int(height))  # pyright: ignore[reportArgumentType]
    return size if size.is_valid() else None


def take(
    connection: apsw.Connection,
    device: str,
    note_id: str,
    contents: Contents,
    held: Mapping[str, Json],
    stamped: Mapping[str, Stamp],
    sent: Mapping[str, int],
) -> bool:
    """The note's places and size as the file has them, here: what is not here
    already, of what this version can take. Whether any was."""
    layouts = LayoutRepository(connection)
    taken = False
    for name, each in sorted(contents.fields.items()):
        if not is_layout_field(name):
            continue
        stamp: Stamp = (each.hlc, None if each.device == device else each.device)
        if (
            held.get(name) == each.value
            and stamped.get(name) == stamp
            and sent.get(name) == each.sent
        ):
            continue
        if name == SIZE:
            size = size_of(each.value)
            if size is None:
                continue
            layouts.take_size(note_id, size)
        else:
            count = count_of(name)
            place = place_of(each.value)
            if count is None or place is None:
                continue
            layouts.take_place(note_id, count, place)
        stamps.put(connection, note_id, name, stamp, each.sent)
        taken = True
    return taken
