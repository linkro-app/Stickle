"""Stickle's own export brought in again: on a new computer, after a disk failed,
or from one computer to another before notes are synced."""

import json
import secrets
from datetime import date, time
from pathlib import Path
from typing import Any

import apsw
import pytest

from stickle.core.reminders import Rule
from stickle.crypto.key_files import C, KeyFiles
from stickle.crypto.keyfile import WrongPasswordError, parse_document
from stickle.crypto.recovery import (
    WrongRecoveryKeyError,
    generate,
    open_exported_pair,
    pair_for_export,
    read_recovery,
    write_recovery,
)
from stickle.crypto.sealed import SealedSlot
from stickle.crypto.secret_notes import (
    create,
    open_exported_with_pair,
    open_exported_with_password,
    open_text,
    seal_text,
)
from stickle.data.colors import ColorRepository
from stickle.data.export import JSON_NAME, SecretExport, export_all
from stickle.data.labels import LabelRepository
from stickle.data.notes import NoteRepository
from stickle.data.reminders import ReminderRepository
from stickle.data.schema import latest_version, open_store
from stickle.importers import sources
from stickle.importers.stickle_export import (
    NOTE_ID,
    bring_in_export,
    new_in_export,
    note_id_from,
    read,
    read_export,
    recognise,
)

KEY = secrets.token_bytes(32)
FAST = (1, 8192)
NOW = "2026-10-28T09:00:00.000Z"


def computer(tmp_path: Path, name: str) -> apsw.Connection:
    folder = tmp_path / name
    folder.mkdir()
    return open_store(folder / "notes.db", KEY)


def exported(
    connection: apsw.Connection, tmp_path: Path, secret: SecretExport | None = None
) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return export_all(connection, out, "0.6.0", secret=secret).folder


def note_with(connection: apsw.Connection, body: str, color: str = "yellow") -> str:
    return NoteRepository(connection).create(body, color).id


@pytest.fixture
def first(tmp_path: Path) -> apsw.Connection:
    """The computer the notes come from: every kind of thing a note has."""
    connection = computer(tmp_path, "first")
    notes, labels = NoteRepository(connection), LabelRepository(connection)
    work = labels.create_category("회사", "blue", "briefcase")
    call = labels.create_mark("전화", "phone")
    meeting = note_with(connection, "# 회의\n- [ ] 자료", "blue")
    notes.set_category(meeting, work.id)
    notes.set_mark(meeting, "todo", True)
    notes.set_mark(meeting, call.id, True)
    ReminderRepository(connection).set(meeting, Rule(date(2099, 1, 2), time(9, 30), "Asia/Seoul"))
    quiet = note_with(connection, "숨긴 메모", "green")
    notes.set_hidden(quiet, True)
    notes.set_locked(quiet, True)
    notes.set_opacity(quiet, 0.7)
    folded = note_with(connection, "접힌 메모", "#123456")
    notes.set_collapsed(folded, True)
    notes.set_always_on_top(folded, False)
    gone = note_with(connection, "지운 메모")
    notes.delete(gone)
    colors = ColorRepository(connection)
    colors.add("#123456")
    colors.add("#abcdef")
    return connection


def by_id(connection: apsw.Connection) -> dict[str, object]:
    return {note.id: note for note in NoteRepository(connection).all()}


def test_everything_a_note_has_comes_along(tmp_path: Path, first: apsw.Connection) -> None:
    folder = exported(first, tmp_path)
    second = computer(tmp_path, "second")
    here = LabelRepository(second).create_category("회사", "pink")  # by name: put there
    ColorRepository(second).add("#123456")  # shown once
    export = read_export(folder)

    brought = bring_in_export(export, second)

    before = {note.id: note for note in NoteRepository(first).all()}
    after = {note.id: note for note in NoteRepository(second).all()}
    assert after.keys() == before.keys()  # the same ids
    for note_id, note in before.items():
        moved = after[note_id]
        assert (moved.body, moved.color, moved.hidden, moved.locked) == (
            note.body, note.color, note.hidden, note.locked
        )  # fmt: skip
        assert (moved.opacity, moved.collapsed, moved.always_on_top, moved.deleted) == (
            note.opacity, note.collapsed, note.always_on_top, note.deleted
        )  # fmt: skip
    meeting = next(n for n in after.values() if n.body.startswith("# 회의"))
    assert meeting.label == here.id
    assert [c.name for c in LabelRepository(second).categories()] == ["회사"]
    names = {m.id: m.name for m in LabelRepository(second).marks()}
    assert {names.get(m) or m for m in meeting.marks} == {"todo", "전화"}
    assert [c.hex for c in ColorRepository(second).colors()] == ["#123456", "#abcdef"]
    reminder = ReminderRepository(second).of_note(meeting.id)
    assert reminder is not None and reminder.rule.start == date(2099, 1, 2)
    assert (len(brought.note_ids), brought.trashed, brought.already_here) == (3, 1, 0)


def test_earlier_versions_stay_with_the_computer_they_were_kept_on(
    tmp_path: Path, first: apsw.Connection
) -> None:
    notes = NoteRepository(first)
    note_id = note_with(first, "처음 글")
    notes.update_body(note_id, "고친 글", keep_earlier=True)
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")

    bring_in_export(export, second)

    assert NoteRepository(second).get(note_id) is not None
    assert second.execute("SELECT count(*) FROM note_versions").fetchall() == [(0,)]


def test_brought_in_twice_nothing_comes_again(tmp_path: Path, first: apsw.Connection) -> None:
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")
    bring_in_export(export, second)

    again = bring_in_export(export, second)

    assert (again.note_ids, again.already_here) == ([], 4)
    assert new_in_export(export, second) == 0
    assert len(NoteRepository(second).all()) == 4


def test_its_own_export_brought_back_changes_nothing(
    tmp_path: Path, first: apsw.Connection
) -> None:
    export = read_export(exported(first, tmp_path))
    assert new_in_export(export, first) == 0
    assert bring_in_export(export, first).already_here == 4
    assert len(NoteRepository(first).all()) == 4


def test_a_note_deleted_for_good_after_it_came_does_not_come_back(
    tmp_path: Path, first: apsw.Connection
) -> None:
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")
    bring_in_export(export, second)
    notes = NoteRepository(second)
    some = next(n for n in notes.all() if n.deleted)
    notes.purge(some.id)

    assert bring_in_export(export, second).note_ids == []


def test_an_id_that_cannot_name_a_file_everywhere_gives_way_to_one_made_from_it(
    tmp_path: Path,
) -> None:
    # An export edited by hand: a note's id becomes its file's name once synced,
    # where letter case, other letters and slashes do not hold.
    old_key, new_key = secrets.token_bytes(32), secrets.token_bytes(32)
    first = computer(tmp_path, "first")
    notes = NoteRepository(first)
    odd = ("Upper-Case", "메모-1", "../x/y", "a" * 65)
    for odd_id in odd:
        notes.create(f"글 {odd_id}", "yellow", note_id=odd_id)
    ReminderRepository(first).set("메모-1", Rule(date(2099, 1, 2), time(9, 30), "Asia/Seoul"))
    notes.create("", "yellow", note_id="Secret")
    notes.make_secret("Secret", "금고", seal_text(old_key, "Secret", "금고 번호 1234"))
    plain = note_with(first, "보통 메모")
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")

    brought = bring_in_export(export, second, old_key, new_key)

    here = NoteRepository(second)
    for odd_id in (*odd, "Secret"):
        made = note_id_from(odd_id)
        assert made != odd_id and NOTE_ID.fullmatch(made)
        assert here.get(odd_id) is None and here.get(made) is not None
    assert note_id_from("Upper-Case") == note_id_from("Upper-Case")  # the same each time
    assert here.get(plain) is not None and note_id_from(plain) == plain  # as it was
    reminder = ReminderRepository(second).of_note(note_id_from("메모-1"))
    assert reminder is not None and reminder.rule.start == date(2099, 1, 2)
    secret = here.get(note_id_from("Secret"))
    assert secret is not None and secret.sealed is not None
    assert open_text(new_key, secret.id, secret.sealed) == "금고 번호 1234"
    assert (len(brought.note_ids), brought.secret) == (6, 1)

    again = bring_in_export(export, second, old_key, new_key)
    assert (again.note_ids, again.already_here, new_in_export(export, second)) == ([], 6, 0)
    assert len(here.all()) == 6


def test_a_note_brought_in_with_its_odd_id_by_an_earlier_version_does_not_come_twice(
    tmp_path: Path,
) -> None:
    first = computer(tmp_path, "first")
    NoteRepository(first).create("손으로 고친 메모", "yellow", note_id="Upper-Case")
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")
    NoteRepository(second).create("손으로 고친 메모", "yellow", note_id="Upper-Case")

    assert new_in_export(export, second) == 0
    assert bring_in_export(export, second).already_here == 1
    assert len(NoteRepository(second).all()) == 1


# Secret notes


def secret_note(connection: apsw.Connection, key: bytes, text: str, title: str) -> str:
    note_id = note_with(connection, "")
    NoteRepository(connection).make_secret(note_id, title, seal_text(key, note_id, text))
    return note_id


def test_a_secret_note_exported_locked_is_locked_again_with_the_key_here(tmp_path: Path) -> None:
    old_key, new_key = secrets.token_bytes(32), secrets.token_bytes(32)
    first = computer(tmp_path, "first")
    note_id = secret_note(first, old_key, "금고 번호 1234", "금고")
    export = read_export(exported(first, tmp_path))
    second = computer(tmp_path, "second")

    left = bring_in_export(export, second)  # its key of the day not given: another time
    assert (left.secret_left, NoteRepository(second).get(note_id)) == (1, None)
    assert new_in_export(export, second) == 1

    brought = bring_in_export(export, second, old_key, new_key)
    note = NoteRepository(second).get(note_id)
    assert note is not None and note.secret and note.secret_title == "금고"
    assert note.sealed is not None and open_text(new_key, note_id, note.sealed) == (
        "금고 번호 1234"
    )
    assert brought.secret == 1
    assert "1234" not in json.dumps(
        [list(map(str, row)) for row in second.execute("SELECT * FROM notes")]
    )


def test_a_secret_note_exported_in_the_clear_comes_as_an_ordinary_note(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    first = computer(tmp_path, "first")
    note_id = secret_note(first, key, "열린 글", "제목")
    export = read_export(exported(first, tmp_path, SecretExport(key=key)))
    second = computer(tmp_path, "second")

    brought = bring_in_export(export, second)

    note = NoteRepository(second).get(note_id)
    assert note is not None and not note.secret and note.body == "열린 글"
    assert brought.was_secret == 1


def test_the_key_of_the_day_opens_with_its_password_or_its_recovery_key(tmp_path: Path) -> None:
    recovery_key, password = generate(), "secret notes 1"
    files = KeyFiles(tmp_path / "keys")
    (tmp_path / "keys").mkdir()
    write_recovery(files, secrets.token_bytes(32), recovery_key, NOW)
    slot = read_recovery(files)
    assert isinstance(slot, SealedSlot)
    key = create(files, password, slot.public, NOW, FAST)
    raw = files.raw(C)
    assert raw is not None
    pair = pair_for_export(files)
    assert pair is not None and "sealed" not in pair  # not the notes database's key
    first = computer(tmp_path, "first")
    secret_note(first, key, "글", "제목")
    copies = SecretExport(key_copies=parse_document(raw), recovery_pair=pair)
    export = read_export(exported(first, tmp_path, copies))

    assert open_exported_with_password(export.secret_key, password) == key
    secret, public = open_exported_pair(export.recovery_pair, recovery_key)
    assert open_exported_with_pair(export.secret_key, secret, public) == key
    with pytest.raises(WrongPasswordError):
        open_exported_with_password(export.secret_key, "not it at all")
    with pytest.raises(WrongRecoveryKeyError):
        open_exported_pair(export.recovery_pair, generate())


# Exports of other versions, and what is not an export


def document_of(folder: Path) -> dict[str, Any]:
    return json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))


def test_a_later_versions_export_is_read_for_what_is_known(
    tmp_path: Path, first: apsw.Connection
) -> None:
    folder = exported(first, tmp_path)
    document = document_of(folder)
    document["format"] = 2
    document["schema_version"] = latest_version() + 1
    tables: dict[str, list[dict[str, Any]]] = document["tables"]
    tables["stickers"] = [{"id": "x", "shape": "star"}]
    for row in tables["notes"]:
        row["glow"] = 3
        row["color"] = "ultraviolet"
    (folder / JSON_NAME).write_text(json.dumps(document), encoding="utf-8")

    export = read_export(folder)
    second = computer(tmp_path, "second")
    bring_in_export(export, second)

    assert export.newer
    assert {n.color for n in NoteRepository(second).all()} == {"yellow"}
    assert len(NoteRepository(second).all()) == 4


def test_an_early_export_with_few_columns_still_brings_its_notes(tmp_path: Path) -> None:
    folder = tmp_path / "Stickle export"
    folder.mkdir()
    rows = [
        {"id": "a1", "body": "첫 메모", "color": "pink", "deleted_at": None},
        {"id": "a2", "body": "휴지통", "color": "blue", "deleted_at": "2026-10-01T00:00:00Z"},
        {"body": "id 없는 줄"},
    ]
    document = {"format": 1, "app": "Stickle", "schema_version": 5, "tables": {"notes": rows}}
    (folder / JSON_NAME).write_text(json.dumps(document), encoding="utf-8")
    second = computer(tmp_path, "second")

    export = read_export(folder)
    brought = bring_in_export(export, second)

    assert export.unreadable == 1
    assert (brought.note_ids, brought.trashed) == (["a1"], 1)


def test_an_export_is_known_by_its_folder_or_its_file(
    tmp_path: Path, first: apsw.Connection
) -> None:
    folder = exported(first, tmp_path)
    assert recognise(folder) == folder / JSON_NAME
    assert recognise(folder / JSON_NAME) == folder / JSON_NAME
    found = sources.recognise(folder)
    assert found is not None and found[0].name == "stickle"
    other = tmp_path / "indicator.json"
    other.write_text(json.dumps({"notes": [], "properties": {}}), encoding="utf-8")
    assert recognise(other) is None
    assert recognise(tmp_path) is None


def test_the_preview_shows_a_locked_secret_note_by_its_title(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    first = computer(tmp_path, "first")
    secret_note(first, key, "숨은 글", "은행")
    result = read(exported(first, tmp_path))
    assert [n.body for n in result.notes] == ["# 은행"]
    assert result.export is not None and result.export.locked_secrets == 1
