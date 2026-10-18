"""Turning another app's notes into notes here, all at once or not at all."""

from collections.abc import Callable
from dataclasses import dataclass, field

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.layout import MAIN, Place, Rect
from stickle.data.imports import ImportRecords
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.importers.base import ImportResult


@dataclass
class BroughtIn:
    note_ids: list[str] = field(default_factory=list[str])
    already_here: int = 0  # brought in before, unchanged since


def new_notes(source: str, result: ImportResult, connection: apsw.Connection) -> int:
    """How many of the notes have not come in before (for the preview)."""
    records = ImportRecords(connection)
    return sum(not records.brought_in(source, n.source_id, n.body) for n in result.notes)


def bring_in(
    source: str,
    result: ImportResult,
    connection: apsw.Connection,
    place_for: Callable[[Rect], Place | None] = lambda _: None,
    clock: Clock = utc_now,
) -> BroughtIn:
    """Make a note of each that has not come in before, keeping its colour,
    category (made if missing, by name), place, and hidden and locked state."""
    notes = NoteRepository(connection, clock)
    labels = LabelRepository(connection, clock)
    layouts = LayoutRepository(connection)
    records = ImportRecords(connection, clock)
    brought = BroughtIn()
    with connection:  # all or nothing
        categories = {category.name.casefold(): category.id for category in labels.categories()}
        for imported in result.notes:
            if records.brought_in(source, imported.source_id, imported.body):
                brought.already_here += 1
                continue
            note = notes.create(imported.body, imported.color)
            if imported.category:
                key = imported.category.casefold()
                if key not in categories:
                    categories[key] = labels.create_category(imported.category, imported.color).id
                notes.set_category(note.id, categories[key])
            if imported.hidden:
                notes.set_hidden(note.id, True)
            if imported.locked:
                notes.set_locked(note.id, True)
            if imported.always_on_top is not None:
                notes.set_always_on_top(note.id, imported.always_on_top)
            if imported.place is not None and (place := place_for(imported.place)) is not None:
                layouts.save(note.id, {MAIN: place})
            records.record(source, imported.source_id, imported.body, note.id)
            brought.note_ids.append(note.id)
    return brought
