"""A note file's versions: the same edit from the same version is one version
on any device, a damaged one is told by its id, and which came from which
decides what a sync does."""

import hashlib
import secrets
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stickle.core.hlc import Hlc
from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents
from stickle.sync.format import Json, UnreadableError, json_bytes, parse_json
from stickle.sync.note_file import (
    HISTORY,
    VERSIONS,
    NoteFile,
    Step,
    Version,
    decide,
    new_version,
    version_id,
)

KEYS = DataKeys({1: secrets.token_bytes(32)})
DESKTOP = "7c1d9a52-3e4b-4f60-8a1b-2c3d4e5f6a70"
LAPTOP = "0b6f3c2e-8f14-4d52-9a57-2f3c8d9e1a40"
NOW = 1_762_483_200_000


def first(text: str = "장보기: 우유", keys: DataKeys = KEYS) -> Version:
    return new_version(keys, [], text, NOW, DESKTOP)


def then(parent: Version, text: str | None, device: str = DESKTOP, edited: int = NOW) -> Version:
    return new_version(KEYS, [parent], text, edited, device)


def read(version: Version, keys: DataKeys = KEYS) -> Version:
    return Version.from_json(keys, parse_json(json_bytes(version.to_json())))


def test_the_same_edit_from_the_same_version_is_one_version_anywhere() -> None:
    start = first()

    here = then(start, "장보기: 우유, 빵", DESKTOP, NOW + 5)
    there = then(start, "장보기: 우유, 빵", LAPTOP, NOW + 9_000)

    assert here.id == there.id  # when and where are beside it, not in it
    assert (here.edited, here.device) != (there.edited, there.device)


def test_a_different_text_parent_or_deletion_is_another_version() -> None:
    start = first()
    edited = then(start, "장보기: 우유, 빵")

    ids = {
        edited.id,
        then(start, "장보기: 우유, 빵 ").id,
        then(first("다른 메모"), "장보기: 우유, 빵").id,
        then(start, None).id,
        then(start, "").id,
    }

    assert len(ids) == 5
    assert then(start, None).deleted and then(start, None).text is None


def test_its_generation_is_one_more_than_its_parents_highest() -> None:
    a = first()
    b = then(then(a, "둘"), "셋")
    joined = new_version(KEYS, [then(a, "하나 더"), b], "합친 글", NOW, DESKTOP)

    assert (a.generation, b.generation, joined.generation) == (1, 3, 4)


def test_its_id_is_the_hash_the_format_says() -> None:
    start = first("첫 줄\n둘째 줄")
    made = b"stickle version 1\nparents \ndeleted 0\nbody plain\n\n" + "첫 줄\n둘째 줄".encode()
    digest = hashlib.blake2b(made, digest_size=16, key=KEYS.lasting_key(VERSIONS)).hexdigest()

    assert start.id == f"1-{digest}"
    joined = version_id(KEYS, [then(start, "b").id, then(start, "a").id], "c", False)
    assert joined == version_id(KEYS, [then(start, "a").id, then(start, "b").id], "c", False)


def test_keys_changing_leaves_ids_and_another_vaults_key_makes_others() -> None:
    changed_keys = DataKeys({**KEYS.keys, 2: secrets.token_bytes(32)})

    assert first(keys=changed_keys).id == first().id
    assert first(keys=DataKeys({1: secrets.token_bytes(32)})).id != first().id


def test_its_history_holds_where_it_came_from_newest_first_and_32_at_most() -> None:
    line = [first()]
    for number in range(40):
        line.append(then(line[-1], f"고침 {number}"))
    other = then(line[10], "다른 갈래")
    joined = new_version(KEYS, [line[12], other], "합친 글", NOW, DESKTOP)

    assert line[-1].history == tuple(v.id for v in reversed(line[-1 - HISTORY : -1]))
    assert line[-1].holds(line[-HISTORY - 1].id) and not line[-1].holds(line[-HISTORY - 2].id)
    assert joined.holds(other.id) and joined.holds(line[12].id) and joined.holds(line[0].id)


def test_a_version_reads_back_as_it_was_and_keeps_what_it_does_not_know() -> None:
    version = then(first(), "다음 글", LAPTOP)
    unknown: dict[str, Json] = {"merged_lines": [1, 2]}  # as a newer version might add
    written: dict[str, Json] = {**version.to_json(), **unknown}

    again = Version.from_json(KEYS, written)

    assert again == replace(version, others=unknown)
    assert again.to_json() == written


def test_a_note_file_holds_its_fields_and_its_text() -> None:
    contents = Contents().changed("color", "blue", Hlc(NOW), LAPTOP)
    note = NoteFile(contents, then(first(), "다음 글"))
    written: dict[str, Json] = {**note.to_json(), "attachments": []}

    again = NoteFile.from_json(KEYS, written)

    assert again.body == note.body and again.contents.fields == contents.fields
    assert again.to_json() == written


def damaged(written: Version, /, **names: Json) -> Json:
    return {**written.to_json(), **names}


@pytest.mark.parametrize(
    "written",
    [
        damaged(then(first(), "다음 글"), text="다음 글!"),  # the text changed under its id
        damaged(then(first(), "다음 글"), version="1-" + "0" * 32),
        damaged(then(first(), "다음 글"), version="02-" + "0" * 32),
        damaged(then(first(), "다음 글"), parents=[]),
        damaged(then(first(), None), text=""),  # deleted, yet a text
        damaged(then(first(), "다음 글"), deleted=True),
        damaged(first("글\0"), text="글\0"),
        damaged(then(first(), "다음 글"), device="Laptop"),
        damaged(then(first(), "다음 글"), edited="1762483200000"),
        damaged(then(first(), "다음 글"), history=["not a version"]),
        {"version": first().id},
        "a body",
    ],
)
def test_a_damaged_version_is_not_read(written: Json) -> None:
    with pytest.raises(UnreadableError):
        Version.from_json(KEYS, written)


def test_a_note_file_without_its_text_is_not_read() -> None:
    with pytest.raises(UnreadableError):
        NoteFile.from_json(KEYS, Contents().to_json())


def test_as_a_device_keeps_it_its_text_may_be_taken_out() -> None:
    """What a device keeps of a note emptied or made secret since: the versions
    a next one is made on, not the text. Never so from the cloud."""
    version = then(first(), "다음 글")
    written: dict[str, Json] = {
        name: each for name, each in version.to_json().items() if name != "text"
    }
    note: dict[str, Json] = {**Contents().to_json(), "body": written}

    kept = NoteFile.kept(note).body

    assert (kept.id, kept.history, kept.text, kept.deleted) == (
        version.id,
        version.history,
        None,
        False,
    )
    assert then(kept, None).parents == (version.id,)
    with pytest.raises(UnreadableError):
        Version.from_json(KEYS, written)
    with pytest.raises(UnreadableError):
        Version.kept(damaged(version, parents=["not a version"]))


def test_read_with_another_vaults_key_it_does_not_hold_together() -> None:
    with pytest.raises(UnreadableError):
        read(then(first(), "다음 글"), DataKeys({1: secrets.token_bytes(32)}))


# What a sync does: from which came from which


def test_at_one_version_nothing_is_done() -> None:
    start = first()

    assert decide(start, read(start)) == Step.SAME


def test_the_clouds_came_from_mine_it_is_received() -> None:
    mine = first()
    on_another_device = then(then(mine, "노트북에서"), "또 노트북에서", LAPTOP)

    assert decide(mine, on_another_device) == Step.RECEIVE


def test_mine_got_there_and_was_built_on_it_is_received() -> None:
    # Sent, the answer lost; another device went on from it.
    base = first()
    sent = then(base, "보냈는데 답을 못 받은 글")

    assert decide(sent, then(sent, "노트북에서 이어 씀", LAPTOP)) == Step.RECEIVE


def test_mine_came_from_the_clouds_it_is_sent() -> None:
    base = first()

    assert decide(then(base, "여기서 고침"), base) == Step.SEND


def test_the_vault_gone_back_gets_mine_again() -> None:
    old = first()
    mine = then(then(old, "둘"), "셋")  # the cloud was put back to old since

    assert decide(mine, old) == Step.SEND


def test_each_changed_from_the_same_one_is_a_conflict() -> None:
    base = first()

    assert decide(then(base, "여기서"), then(base, "노트북에서", LAPTOP)) == Step.CONFLICT
    assert decide(first("이 메모"), first("저 메모")) == Step.CONFLICT  # not told: kept both


def test_too_far_apart_to_tell_is_a_conflict() -> None:
    line = [first()]
    for number in range(HISTORY + 1):
        line.append(then(line[-1], f"고침 {number}", LAPTOP))

    assert decide(line[0], line[-1]) == Step.CONFLICT
    assert decide(line[1], line[-1]) == Step.RECEIVE


MIRROR = {Step.SAME: Step.SAME, Step.RECEIVE: Step.SEND, Step.SEND: Step.RECEIVE}


@given(
    plan=st.lists(st.tuples(st.integers(0, 30), st.integers(0, 30), st.booleans()), max_size=25),
    a=st.integers(0, 30),
    b=st.integers(0, 30),
)
def test_two_devices_decide_alike(plan: list[tuple[int, int, bool]], a: int, b: int) -> None:
    """Whatever the tree of versions, each device sees the other's side of it."""
    versions = [first()]
    for number, (one, other, join) in enumerate(plan):
        parents = [versions[one % len(versions)]]
        if join:
            parents.append(versions[other % len(versions)])
        versions.append(new_version(KEYS, parents, f"판 {number}", NOW, DESKTOP))
    mine, cloud = versions[a % len(versions)], versions[b % len(versions)]

    seen, seen_back = decide(mine, cloud), decide(cloud, mine)

    assert seen_back == MIRROR.get(seen, Step.CONFLICT)
    if seen == Step.CONFLICT:
        assert not (mine.holds(cloud.id) or cloud.holds(mine.id))
