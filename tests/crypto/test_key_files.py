"""The key files: their names, their second copies, and the database's copy."""

import json
import logging
from pathlib import Path

import pytest

from stickle.crypto.key_files import NAMES, A, B, C, KeyFiles
from stickle.crypto.keyfile import WARNING, document_bytes, write_slots

SLOTS = {"password": {"kdf": "argon2id", "opslimit": 1, "memlimit": 8192, "salt": "", "box": ""}}


class Database:
    """The notes' copies, in memory."""

    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}

    def get(self, name: str) -> bytes | None:
        return self.kept.get(name)

    def put(self, name: str, content: bytes) -> None:
        self.kept[name] = content


def files_with(folder: Path, *names: str) -> KeyFiles:
    files = KeyFiles(folder)
    for name in names:
        files.save(name, {name: {"x": 1}})
    return files


def test_the_old_names_become_the_new_ones_as_they_were(tmp_path: Path) -> None:
    write_slots(tmp_path / "keys.json", SLOTS)
    write_slots(tmp_path / "recovery.json", {"recovery": {"x": 2}})
    files = KeyFiles(tmp_path)

    files.prepare()

    assert not (tmp_path / "keys.json").exists() and not (tmp_path / "recovery.json").exists()
    assert files.load(A) == SLOTS
    assert files.load(B) == {"recovery": {"x": 2}}
    assert files.path(A).read_bytes() == files.second_path(A).read_bytes()
    assert not files.path(C).exists()  # made once secret notes are used


def test_each_file_says_not_to_delete_it(tmp_path: Path) -> None:
    write_slots(tmp_path / "keys.json", SLOTS)  # an old one, without the line
    files = KeyFiles(tmp_path)
    files.prepare()

    text = files.path(A).read_text(encoding="utf-8")
    assert json.loads(text)["warning"] == WARNING
    assert text.lstrip("{\n ").startswith('"warning"')  # the first thing seen
    assert "메모를 여는 데 필요합니다" in text  # Korean as it reads, not escaped
    assert not Path(files.path(A)).suffix


def test_a_name_that_cannot_be_changed_is_read_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_slots(tmp_path / "keys.json", SLOTS)

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", refuse)
    files = KeyFiles(tmp_path)
    files.prepare()

    assert (tmp_path / "keys.json").exists()
    assert files.load(A) == SLOTS


def test_a_lost_copy_comes_back_from_the_other(tmp_path: Path) -> None:
    files = files_with(tmp_path, A, B)
    files.path(A).unlink()
    files.second_path(B).unlink()

    files.prepare()

    assert files.load(A) == {A: {"x": 1}}
    assert files.second_path(B).read_bytes() == files.path(B).read_bytes()


def test_a_copy_gone_while_running_comes_back_from_the_other(tmp_path: Path) -> None:
    files = files_with(tmp_path, A, C)
    files.path(A).unlink()
    files.path(C).unlink()

    assert files.load(A) == {A: {"x": 1}}  # never taken for "none"
    assert files.load(C) == {C: {"x": 1}}
    assert files.path(C).read_bytes() == files.second_path(C).read_bytes()  # put back


def test_with_both_files_gone_while_running_the_notes_copy_is_read(tmp_path: Path) -> None:
    files = files_with(tmp_path, C)
    files.keep_in(Database())
    files.path(C).unlink()
    files.second_path(C).unlink()

    assert files.load(C) == {C: {"x": 1}}
    assert files.path(C).exists() and files.second_path(C).exists()


def test_a_damaged_copy_while_running_is_not_put_back_from_the_other(tmp_path: Path) -> None:
    # Reading stays as it was: the damaged file is what is there (set aside at start).
    files = files_with(tmp_path, C)
    files.path(C).write_bytes(b"damaged")

    assert files.raw(C) == b"damaged"
    assert files.second_path(C).read_bytes() != b"damaged"


def test_the_second_copy_follows_the_data_folders(tmp_path: Path) -> None:
    files = files_with(tmp_path, A)
    files.second_path(A).write_bytes(document_bytes({"older": {}}))

    files.prepare()

    assert files.second_path(A).read_bytes() == files.path(A).read_bytes()
    assert files.load(A) == {A: {"x": 1}}


def test_a_damaged_copy_is_set_aside_and_never_copied_over_the_other(tmp_path: Path) -> None:
    files = files_with(tmp_path, A, B)
    files.path(A).write_text("{ damaged")
    files.path(B).write_text("{ damaged")
    files.second_path(B).write_text("{ damaged too")

    files.prepare()

    assert files.load(A) == {A: {"x": 1}}  # put back from its second copy
    assert (tmp_path / f"{A}.damaged").read_text() == "{ damaged"
    # Neither copy of B can be read: both are left as they are.
    assert files.path(B).read_text() == "{ damaged"
    assert files.second_path(B).read_text() == "{ damaged too"


def test_what_this_version_does_not_know_is_kept(tmp_path: Path) -> None:
    files = KeyFiles(tmp_path)
    document = json.loads(document_bytes(SLOTS))
    document["key_check"] = "from a newer version"
    files.path(A).write_text(json.dumps(document))

    files.save(A, {**SLOTS, "other": {"y": 2}})

    saved = json.loads(files.path(A).read_text(encoding="utf-8"))
    assert saved["key_check"] == "from a newer version"
    assert saved["slots"]["other"] == {"y": 2}


def test_the_notes_keep_b_and_c_but_not_a(tmp_path: Path) -> None:
    files = files_with(tmp_path, A, B, C)
    database = Database()

    files.keep_in(database)

    assert sorted(database.kept) == [B, C]
    assert database.kept[B] == files.path(B).read_bytes()
    files.save(B, {"new": {}})  # from now on, as each is written
    assert database.kept[B] == files.path(B).read_bytes()


def test_with_both_files_lost_they_come_back_from_the_notes(tmp_path: Path) -> None:
    files = files_with(tmp_path, B)
    database = Database()
    files.keep_in(database)
    for path in (files.path(B), files.second_path(B)):
        path.unlink()

    KeyFiles(tmp_path).keep_in(database)

    assert files.load(B) == {B: {"x": 1}}
    assert files.second_path(B).read_bytes() == files.path(B).read_bytes()


def test_an_older_copy_in_the_notes_does_not_replace_the_file(tmp_path: Path) -> None:
    # Notes brought back from a backup hold the copy of that day.
    files = files_with(tmp_path, B)
    database = Database()
    database.kept[B] = document_bytes({"older": {}})

    files.keep_in(database)

    assert files.load(B) == {B: {"x": 1}}
    assert database.kept[B] == files.path(B).read_bytes()


def test_the_log_names_no_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    write_slots(tmp_path / "keys.json", SLOTS)
    files = KeyFiles(tmp_path)
    files.prepare()
    files.path(A).unlink()
    files.prepare()

    assert caplog.records
    for name in (*NAMES, "keys.json", str(tmp_path), "backups"):
        assert name not in caplog.text
