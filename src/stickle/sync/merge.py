"""A note made one from this device's and the cloud's, as every device makes it.

Fields: each keeps the later change (its time, then its device's id). The text
goes by versions (stickle.sync.note_file.decide). Two lines of text that each
have what the other has not are both kept:

- the same text, byte for byte: joined into one version, with both for parents;
- one deletes the note (it was emptied from the trash) and the other changed
  its text: the changed text is kept, in the trash: whoever emptied it did not
  see it, and it comes back only when taken out of the trash;
- both delete it: joined, deleted;
- else the text edited later (at the same time: the higher version id) stays
  the note's, joined with the other, and the other goes to a conflict copy, a
  note of its own whose id comes from the note's and that version's, so that
  two devices seeing the same conflict make one copy. The copy has the note's
  fields (its colour, where it is: in the trash too) and says which note it is
  a copy of.

Emptied on one device while another took it out of the trash or changed a
field, a note stays emptied: there is no new text, so nothing is lost; field
changes after a deletion are let go.

A year in the trash, and a deletion kept a year in the vault, count from when
they first reached the cloud, by the cloud's clock (sent), not from what the
clock said where the change was made.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from stickle.core.clock import stamp_of
from stickle.core.hlc import after
from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json, json_bytes
from stickle.sync.note_file import NoteFile, Step, Version, decide, new_version

TRASH = "deleted_at"  # the field that puts a note in the trash: when, else None
COPY_OF = "conflict_of"  # a conflict copy's: {"note", "device", "edited"} of what it copies
YEAR = 365 * 24 * 60 * 60 * 1000
_COPY = "conflict copy"  # the kind of the ids conflict copies are made with


@dataclass(frozen=True)
class Merged:
    note: NoteFile  # the note, as both end up with it
    step: Step
    copy: tuple[str, NoteFile] | None = None  # a conflict copy made: its id and file


def _order(each: Field) -> tuple[object, ...]:
    return (each.stamp, json_bytes(each.value))


def _later(a: Field, b: Field) -> Field:
    if a.stamp == b.stamp and a.value == b.value:  # one change, seen twice
        sents = [each.sent for each in (a, b) if each.sent is not None]
        return replace(a, sent=min(sents) if sents else None)
    return a if _order(a) > _order(b) else b


def merge_fields(mine: Contents, cloud: Contents) -> Contents:
    """Each field's later change; what either has that the other has not, kept."""
    fields: dict[str, Field] = {}
    for name in sorted(mine.fields.keys() | cloud.fields.keys()):
        a, b = mine.fields.get(name), cloud.fields.get(name)
        if a is None:
            fields[name] = cloud.fields[name]
        elif b is None:
            fields[name] = a
        else:
            fields[name] = _later(a, b)
    others: dict[str, Json] = {}
    for name in sorted(mine.others.keys() | cloud.others.keys()):  # not known: either alike
        found = [side[name] for side in (mine.others, cloud.others) if name in side]
        others[name] = max(found, key=json_bytes)
    return Contents(fields, others)


def copy_id(keys: DataKeys, note_id: str, version_id: str) -> str:
    """The id of the conflict copy of note_id that the version goes to: the same
    on every device."""
    return keys.record_id(_COPY, f"{note_id} {version_id}")


def _join(keys: DataKeys, a: Version, b: Version, text: str | None, author: Version) -> Version:
    return new_version(keys, [a, b], text, author.edited, author.device)


def _later_edited(a: Version, b: Version) -> tuple[Version, Version]:
    """(the one edited later, the other)."""
    return (a, b) if (a.edited, a.id) > (b.edited, b.id) else (b, a)


def _one_of(a: Version, b: Version) -> Version:
    """One version made twice: the one in the cloud first, else either alike."""

    def order(each: Version) -> tuple[bool, int, int, str]:
        return (each.sent is None, each.sent or 0, -each.edited, each.device)

    return min(a, b, key=order)


def _time_text(millis: int) -> str:
    return stamp_of(datetime.fromtimestamp(millis / 1000, UTC))


def _in_trash(fields: Contents, mine: Contents, cloud: Contents, gone: Version) -> Contents:
    """The fields with the note in the trash: as it was where it was emptied, and
    after any change either made to that."""
    trash = fields.fields.get(TRASH)
    if trash is not None and trash.value is not None:
        return fields
    both = [each for each in (mine.fields.get(TRASH), cloud.fields.get(TRASH)) if each is not None]
    stamps = [each.hlc for each in both]
    # At most one had it in the trash, or the later one would have kept it there.
    emptied = [each.value for each in both if each.value is not None]
    value = emptied[0] if emptied else _time_text(gone.edited)
    put = Field(value, after(0, max(stamps)) if stamps else after(gone.edited, None), gone.device)
    return replace(fields, fields={**fields.fields, TRASH: put})


def _copy(keys: DataKeys, note_id: str, fields: Contents, other: Version) -> tuple[str, NoteFile]:
    of: Json = {"note": note_id, "device": other.device, "edited": other.edited}
    marked = Field(of, after(other.edited, None), other.device)
    copy_fields = replace(fields, fields={**fields.fields, COPY_OF: marked})
    first = new_version(keys, [], other.text, other.edited, other.device)
    return copy_id(keys, note_id, other.id), NoteFile(copy_fields, first)


def merge(keys: DataKeys, note_id: str, mine: NoteFile, cloud: NoteFile) -> Merged:
    """mine: the note as this device has it (its text at its head version)."""
    step = decide(mine.body, cloud.body)
    fields = merge_fields(mine.contents, cloud.contents)
    if step is Step.SAME:  # emptied on both, too: the fields merge alike
        return Merged(NoteFile(fields, _one_of(mine.body, cloud.body)), step)
    # Emptied on one side only: the fields as they were when it was emptied.
    if step is Step.RECEIVE:
        return Merged(NoteFile(cloud.contents if cloud.body.deleted else fields, cloud.body), step)
    if step is Step.SEND:
        return Merged(NoteFile(mine.contents if mine.body.deleted else fields, mine.body), step)
    a, b = mine.body, cloud.body
    if a.deleted and b.deleted:
        later, _ = _later_edited(a, b)
        return Merged(NoteFile(fields, _join(keys, a, b, None, later)), step)
    if a.deleted or b.deleted:
        gone, kept = (a, b) if a.deleted else (b, a)
        trashed = _in_trash(fields, mine.contents, cloud.contents, gone)
        return Merged(NoteFile(trashed, _join(keys, gone, kept, kept.text, kept)), step)
    if a.text == b.text:
        later, _ = _later_edited(a, b)
        return Merged(NoteFile(fields, _join(keys, a, b, a.text, later)), step)
    stays, other = _later_edited(a, b)
    joined = _join(keys, stays, other, stays.text, stays)
    return Merged(NoteFile(fields, joined), step, _copy(keys, note_id, fields, other))


def as_sent(note: NoteFile, cloud_now: int) -> NoteFile:
    """The note as it is put in the cloud: what was not there before, sent now."""
    fields = {
        name: each if each.sent is not None else replace(each, sent=cloud_now)
        for name, each in note.contents.fields.items()
    }
    body = note.body if note.body.sent is not None else replace(note.body, sent=cloud_now)
    return NoteFile(replace(note.contents, fields=fields), body)


def deletion_due(note: NoteFile, cloud_now: int) -> bool:
    """A deletion kept a year in the vault since it got there: its file can go."""
    sent = note.body.sent
    return note.body.deleted and sent is not None and cloud_now - sent >= YEAR


def trash_due(note: NoteFile, cloud_now: int) -> bool:
    """In the trash a year since that reached the cloud: to be emptied."""
    trash = note.contents.fields.get(TRASH)
    if note.body.deleted or trash is None or trash.value is None or trash.sent is None:
        return False
    return cloud_now - trash.sent >= YEAR
