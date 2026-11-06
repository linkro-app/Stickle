"""What a note's places, as versions until 0.6 kept them, become after the upgrade.

Those versions kept a main place and a spare one, each as (monitor number,
its name, rel_x, rel_y, width, height, the names of the monitors connected
then). Each goes under the number of monitors it was stored with (those
named, or at least its monitor's number), the main one first; the size is the
main place's.
"""

from collections.abc import Mapping

from stickle.core.layout import Place, Size

type OldPlace = tuple[int, str, float, float, int, int, tuple[str, ...]]


def upgraded(slots: Mapping[str, OldPlace]) -> tuple[dict[int, Place], Size | None]:
    places: dict[int, Place] = {}
    size: Size | None = None
    for slot in ("main", "spare"):
        if slot not in slots:
            continue
        number, name, rel_x, rel_y, width, height, setup = slots[slot]
        places.setdefault(len(setup) or max(1, number), Place(number, name, rel_x, rel_y, setup))
        size = size or Size(width, height)
    return places, size
