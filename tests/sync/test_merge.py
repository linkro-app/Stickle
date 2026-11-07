"""A note made one from two devices': the later change of each field, both
texts kept when they went apart, the trash and emptying it as the rules say,
and the same result whichever device does it."""

import secrets
from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents, Field
from stickle.sync.format import Json
from stickle.sync.merge import (
    COPY_OF,
    TRASH,
    YEAR,
    Merged,
    as_sent,
    copy_id,
    deletion_due,
    merge,
    trash_due,
)
from stickle.sync.note_file import NoteFile, Step, Version, new_version

KEYS = DataKeys({1: secrets.token_bytes(32)})
DESKTOP = "7c1d9a52-3e4b-4f60-8a1b-2c3d4e5f6a70"
LAPTOP = "0b6f3c2e-8f14-4d52-9a57-2f3c8d9e1a40"
NOTE = "a70a61ea-4ead-4c2a-8970-551edc292be5"
NOW = 1_762_483_200_000
TRASHED = "2026-11-07T09:00:00.000Z"


def field(value: Json, at: int, device: str = DESKTOP, sent: int | None = None) -> Field:
    return Field(value, Hlc(at), device, sent=sent)


def note(body: Version, **fields: Field) -> NoteFile:
    return NoteFile(Contents(fields), body)


def first(text: str = "장보기: 우유") -> Version:
    return new_version(KEYS, [], text, NOW, DESKTOP)


def then(parent: Version, text: str | None, device: str = DESKTOP, edited: int = NOW) -> Version:
    return new_version(KEYS, [parent], text, edited, device)


def agreed(mine: NoteFile, cloud: NoteFile) -> Merged:
    """The merge as this device makes it, checked to be what the other would."""
    here, there = merge(KEYS, NOTE, mine, cloud), merge(KEYS, NOTE, cloud, mine)
    assert here.note.to_json() == there.note.to_json()
    assert (here.copy is None) == (there.copy is None)
    if here.copy is not None and there.copy is not None:
        assert here.copy[0] == there.copy[0]
        assert here.copy[1].to_json() == there.copy[1].to_json()
    return here


def value(merged: NoteFile, name: str) -> Json:
    return merged.contents.fields[name].value


# Fields


def test_each_field_keeps_its_later_change_from_either_side() -> None:
    base = first()
    mine = note(base, color=field("blue", NOW + 5), hidden=field(True, NOW))
    cloud = note(base, color=field("pink", NOW + 1, LAPTOP), hidden=field(False, NOW + 9, LAPTOP))

    merged = agreed(mine, cloud)

    assert merged.step is Step.SAME
    assert (value(merged.note, "color"), value(merged.note, "hidden")) == ("blue", False)


def test_a_field_this_version_does_not_know_is_kept_and_merged_alike() -> None:
    base = first()
    mine = note(base, stars=field(3, NOW + 5))
    cloud = note(base, stars=field(4, NOW + 1, LAPTOP), glow=field("soft", NOW, LAPTOP))

    merged = agreed(mine, cloud)

    assert (value(merged.note, "stars"), value(merged.note, "glow")) == (3, "soft")


def test_one_change_seen_twice_keeps_when_it_first_reached_the_cloud() -> None:
    base = first()
    mine = note(base, color=field("blue", NOW))
    cloud = note(base, color=field("blue", NOW, sent=NOW + 100))

    assert agreed(mine, cloud).note.contents.fields["color"].sent == NOW + 100


# The text


def test_the_text_goes_where_its_versions_say() -> None:
    base = first()
    edited = then(base, "노트북에서 고침", LAPTOP)

    received = agreed(note(base), note(edited))
    sent = merge(KEYS, NOTE, note(edited), note(base))

    assert (received.step, received.note.body) == (Step.RECEIVE, edited)
    assert (sent.step, sent.note.body) == (Step.SEND, edited)


def test_two_texts_gone_apart_keep_the_later_and_copy_the_other() -> None:
    base = first()
    here = then(base, "여기서 고친 글", DESKTOP, NOW + 2000)
    there = then(base, "노트북에서 고친 글", LAPTOP, NOW + 1000)
    cloud = note(there, color=field("pink", NOW + 1500, LAPTOP))

    merged = agreed(note(here), cloud)

    assert merged.step is Step.CONFLICT
    assert merged.note.body.text == "여기서 고친 글"
    assert merged.note.body.parents == tuple(sorted((here.id, there.id)))
    assert merged.copy is not None
    copy_of, copy = merged.copy
    assert copy_of == copy_id(KEYS, NOTE, there.id)
    assert copy.body.text == "노트북에서 고친 글" and copy.body.parents == ()
    assert value(copy, "color") == "pink"  # it follows the note
    assert value(copy, COPY_OF) == {"note": NOTE, "device": LAPTOP, "edited": NOW + 1000}


def test_edited_at_the_same_time_the_higher_version_stays() -> None:
    base = first()
    a, b = then(base, "가"), then(base, "나", LAPTOP)

    merged = agreed(note(a), note(b))

    assert merged.note.body.text == max((a, b), key=lambda v: v.id).text


def test_once_joined_the_same_conflict_does_not_come_again() -> None:
    base = first()
    merged = agreed(note(then(base, "여기서")), note(then(base, "노트북에서", LAPTOP)))

    again = agreed(merged.note, merged.note)
    later = agreed(merged.note, note(then(merged.note.body, "이어서 고침", LAPTOP)))

    assert (again.step, again.copy) == (Step.SAME, None)
    assert (later.step, later.copy) == (Step.RECEIVE, None)


def test_the_same_text_by_two_ways_is_one_note() -> None:
    base = first()
    here = then(then(base, "장보기: 우유, 빵"), "장보기: 우유, 빵, 달걀")
    there = then(base, "장보기: 우유, 빵, 달걀", LAPTOP)

    merged = agreed(note(here), note(there))

    assert merged.copy is None
    assert merged.note.body.text == "장보기: 우유, 빵, 달걀"
    assert merged.note.body.holds(here.id) and merged.note.body.holds(there.id)


def test_a_conflict_in_the_trash_puts_its_copy_in_the_trash_too() -> None:
    base = first()
    trashed = field(TRASHED, NOW + 50, LAPTOP)
    cloud = note(then(base, "노트북에서", LAPTOP, NOW), deleted_at=trashed)

    merged = agreed(note(then(base, "여기서", DESKTOP, NOW + 10)), cloud)

    assert merged.copy is not None and value(merged.copy[1], TRASH) == TRASHED


# The trash and emptying it, as each pair of changes made apart


def test_put_in_the_trash_there_edited_here_it_is_in_the_trash_with_the_edit() -> None:
    base = first()
    trashed = note(base, deleted_at=field(TRASHED, NOW + 50, LAPTOP))
    edited = note(then(base, "고친 글"), deleted_at=field(None, NOW))

    merged = agreed(edited, trashed)

    assert merged.note.body.text == "고친 글" and value(merged.note, TRASH) == TRASHED


def test_put_in_the_trash_there_a_field_changed_here_it_is_in_the_trash_so_changed() -> None:
    base = first()
    trashed = note(base, deleted_at=field(TRASHED, NOW + 50, LAPTOP))
    coloured = note(base, color=field("blue", NOW + 60))

    merged = agreed(coloured, trashed)

    assert (value(merged.note, TRASH), value(merged.note, "color")) == (TRASHED, "blue")


def test_emptied_there_edited_here_the_edit_stays_in_the_trash() -> None:
    base = first()
    emptied = note(then(base, None, LAPTOP), deleted_at=field(TRASHED, NOW + 50, LAPTOP))
    # Here it was taken out of the trash, later, and its text changed.
    edited = note(then(base, "아무도 못 본 새 글"), deleted_at=field(None, NOW + 90))

    merged = agreed(edited, emptied)

    assert merged.step is Step.CONFLICT and merged.copy is None
    assert merged.note.body.text == "아무도 못 본 새 글" and not merged.note.body.deleted
    assert value(merged.note, TRASH) == TRASHED  # back in the trash, not on the desktop
    assert merged.note.contents.fields[TRASH].hlc > Hlc(NOW + 90)


def test_emptied_there_taken_out_or_a_field_changed_here_it_stays_emptied() -> None:
    base = first()
    gone = then(base, None, LAPTOP)
    emptied = note(gone, deleted_at=field(TRASHED, NOW + 50, LAPTOP))
    restored = note(base, deleted_at=field(None, NOW + 90), color=field("blue", NOW + 95))

    merged = agreed(restored, emptied)

    assert merged.note.body == gone
    assert merged.note.contents == emptied.contents  # changes after it was emptied: let go


def test_emptied_on_both_is_one_deletion() -> None:
    base = first()

    same = agreed(note(then(base, None)), note(then(base, None, LAPTOP)))
    apart = agreed(note(then(then(base, "고침"), None)), note(then(base, None, LAPTOP)))

    assert same.step is Step.SAME and same.note.body.deleted
    assert apart.step is Step.CONFLICT and apart.note.body.deleted and apart.copy is None


# Counted from when it reached the cloud


def test_sent_marks_what_was_not_in_the_cloud_yet() -> None:
    base = first()
    put = note(base, color=field("blue", NOW, sent=NOW), hidden=field(True, NOW + 5))

    sent = as_sent(put, NOW + 1000)

    assert sent.contents.fields["color"].sent == NOW
    assert sent.contents.fields["hidden"].sent == NOW + 1000
    assert sent.body.sent == NOW + 1000
    assert as_sent(sent, NOW + 5000) == sent


def test_a_year_is_counted_by_the_clouds_clock_from_when_it_got_there() -> None:
    # Put in the trash and emptied on a computer whose clock said 2010.
    base = first()
    trashed = note(base, deleted_at=field("2010-01-01T00:00:00.000Z", 1_262_304_000_000))
    in_cloud = as_sent(trashed, NOW)
    deletion = as_sent(note(then(base, None, edited=1_262_304_000_000)), NOW)

    assert not trash_due(in_cloud, NOW + YEAR - 1) and trash_due(in_cloud, NOW + YEAR)
    assert not deletion_due(deletion, NOW + YEAR - 1) and deletion_due(deletion, NOW + YEAR)
    assert not trash_due(trashed, NOW + 10 * YEAR)  # not in the cloud yet: not counted
    assert not deletion_due(note(base), NOW + 10 * YEAR)


# Whatever the two sides, both devices make the same


TEXTS = st.sampled_from(["가", "나", "다", None])
FIELD_VALUES = st.sampled_from(["blue", "pink", None, True, False])


@st.composite
def sides(draw: st.DrawFn) -> tuple[NoteFile, NoteFile]:
    versions = [first()]
    for _ in range(draw(st.integers(0, 8))):
        parents = [draw(st.sampled_from(versions))]
        if draw(st.booleans()):
            parents.append(draw(st.sampled_from(versions)))
        text = draw(TEXTS)
        device, edited = draw(st.sampled_from([DESKTOP, LAPTOP])), NOW + draw(st.integers(0, 3))
        versions.append(new_version(KEYS, parents, text, edited, device))

    def one() -> NoteFile:
        names = draw(st.lists(st.sampled_from(["color", TRASH, "hidden"]), unique=True))
        fields = {
            name: field(
                draw(FIELD_VALUES),
                NOW + draw(st.integers(0, 3)),
                draw(st.sampled_from([DESKTOP, LAPTOP])),
                draw(st.none() | st.just(NOW)),
            )
            for name in names
        }
        body = draw(st.sampled_from(versions))
        if draw(st.booleans()):  # in the cloud already, as either side knew it
            body = replace(body, sent=NOW + draw(st.integers(0, 3)))
        unknown: dict[str, Json] = {"later": draw(st.integers(0, 2))} if draw(st.booleans()) else {}
        return NoteFile(Contents(fields, unknown), body)

    return one(), one()


@given(sides())
def test_both_devices_make_the_same_of_any_two(pair: tuple[NoteFile, NoteFile]) -> None:
    mine, cloud = pair

    merged = agreed(mine, cloud)

    if not merged.note.body.deleted:
        for side in (mine, cloud):  # no text is lost
            text = side.body.text
            kept = [merged.note.body.text, merged.copy[1].body.text if merged.copy else None]
            assert text is None or text in kept or merged.note.body.holds(side.body.id)
