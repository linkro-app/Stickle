import itertools
import json
import secrets
from pathlib import Path

import pytest

from stickle.crypto.key_files import A, B
from stickle.crypto.keyfile import KeyFile, WrongPasswordError, wrap_with_password, write_key_file
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.platform.credentials import (
    DATABASE_KEY,
    SERVICE,
    CredentialStore,
    CredentialStoreUnavailableError,
)
from stickle.unlock import Blocked, NeedPassword, Unlock, Unlocked

FAST = (1, 8192)
PASSWORD = "correct horse"


class FakeBackend:
    def __init__(self, state: str) -> None:
        self.state = state  # has_key, empty, locked
        self.items: dict[tuple[str, str], str] = {}
        self.writes = 0
        if state == "has_key":
            CredentialStore(self).get_or_create_key()
            self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        if self.state == "locked":
            raise RuntimeError("locked")
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


class Store:
    """Counts how often the credential store is asked for at all."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.backend = FakeBackend("empty" if state == "absent" else state)
        self.opened = 0

    def __call__(self) -> CredentialStore:
        self.opened += 1
        if self.state == "absent":
            raise CredentialStoreUnavailableError("no store on this system")
        return CredentialStore(self.backend)

    def key(self) -> bytes | None:
        value = self.backend.items.get((SERVICE, DATABASE_KEY))
        return None if value is None else CredentialStore(self.backend).read(DATABASE_KEY)


def prepare(folder: Path, database: bool, key_file: str) -> None:
    if database:
        (folder / "notes.db").write_bytes(b"existing notes")
    path = folder / "keys.json"
    if key_file == "password":
        write_key_file(
            path, KeyFile(wrap_with_password(secrets.token_bytes(32), PASSWORD, *FAST), {})
        )
    elif key_file == "other_slots_only":
        path.write_text(json.dumps({"format": 1, "slots": {"future": {"x": 1}}}))
    elif key_file == "damaged":
        path.write_text("{ not json")


def expected(database: bool, key_file: str, store: str) -> object:
    if key_file == "damaged":
        return Blocked("key_file_unreadable")
    if key_file == "password":
        return NeedPassword(create=False)
    if store == "has_key":
        return "unlocked"
    if store == "empty":
        return Blocked("key_missing") if database else "unlocked"
    return Blocked("store_unavailable") if database else NeedPassword(create=True)


CASES = list(
    itertools.product(
        [False, True],
        ["none", "password", "other_slots_only", "damaged"],
        ["has_key", "empty", "locked", "absent"],
    )
)


@pytest.mark.parametrize(("database", "key_file", "store_state"), CASES)
def test_every_start_situation(
    tmp_path: Path, database: bool, key_file: str, store_state: str
) -> None:
    prepare(tmp_path, database, key_file)
    store = Store(store_state)
    key_before = store.key()

    outcome = Unlock(tmp_path, store, FAST).outcome()

    want = expected(database, key_file, store_state)
    if want == "unlocked":
        assert isinstance(outcome, Unlocked) and outcome.source == "credential store"
        if key_before is not None:
            assert outcome.key == key_before
    else:
        assert outcome == want
    # Password mode and an unreadable key file never touch the store (no unlock prompt).
    if key_file in {"password", "damaged"}:
        assert store.opened == 0
    # With a database present, a key is never created or replaced.
    if database:
        assert store.backend.writes == 0
        assert store.key() == key_before


def test_retry_after_unlocking_the_store(tmp_path: Path) -> None:
    prepare(tmp_path, database=True, key_file="none")
    store = Store("has_key")
    store.backend.state = "locked"
    unlock = Unlock(tmp_path, store, FAST)
    assert unlock.outcome() == Blocked("store_unavailable")

    store.backend.state = "has_key"
    unlock.start()

    outcome = unlock.outcome()
    assert isinstance(outcome, Unlocked)


def test_first_start_with_a_password_then_every_later_start(tmp_path: Path) -> None:
    store = Store("absent")
    first = Unlock(tmp_path, store, FAST)
    assert first.outcome() == NeedPassword(create=True)

    key = first.create_password(PASSWORD)
    assert (tmp_path / A).exists() and (tmp_path / "backups" / A).exists()
    connection = open_store(first.database, key)
    note = NoteRepository(connection).create("회의록")
    connection.close()

    later = Unlock(tmp_path, store, FAST)
    assert later.outcome() == NeedPassword(create=False)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(WrongPasswordError):
        later.enter_password("wrong password")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before

    connection = open_store(later.database, later.enter_password(PASSWORD))
    assert NoteRepository(connection).get(note.id) == note
    connection.close()
    assert store.opened == 1  # only the very first start asked for a store


def test_a_store_that_appears_later_does_not_switch_modes(tmp_path: Path) -> None:
    first = Unlock(tmp_path, Store("absent"), FAST)
    first.outcome()
    first.create_password(PASSWORD)

    store = Store("empty")
    assert Unlock(tmp_path, store, FAST).outcome() == NeedPassword(create=False)
    assert store.opened == 0


def test_no_password_is_created_over_an_existing_database(tmp_path: Path) -> None:
    prepare(tmp_path, database=False, key_file="none")
    unlock = Unlock(tmp_path, Store("absent"), FAST)
    assert unlock.outcome() == NeedPassword(create=True)
    (tmp_path / "notes.db").write_bytes(b"appeared meanwhile")

    with pytest.raises(RuntimeError):
        unlock.create_password(PASSWORD)
    assert not (tmp_path / A).exists()


def test_short_passwords_are_refused_and_nothing_is_written(tmp_path: Path) -> None:
    unlock = Unlock(tmp_path, Store("absent"), FAST)
    unlock.outcome()

    with pytest.raises(ValueError, match="short"):
        unlock.create_password("1234567")
    assert not (tmp_path / A).exists()
    assert len(unlock.create_password("12345678")) == 32


def test_creating_a_password_keeps_other_slots(tmp_path: Path) -> None:
    prepare(tmp_path, database=False, key_file="other_slots_only")
    unlock = Unlock(tmp_path, Store("absent"), FAST)
    assert unlock.outcome() == NeedPassword(create=True)

    unlock.create_password(PASSWORD)

    saved = json.loads((tmp_path / A).read_text(encoding="utf-8"))
    assert saved["slots"]["future"] == {"x": 1}
    assert "password" in saved["slots"]


def test_a_recovery_file_lost_from_both_places_comes_back_from_the_notes(tmp_path: Path) -> None:
    first = Unlock(tmp_path, Store("absent"), FAST)
    first.outcome()
    key = first.create_password(PASSWORD)
    connection = open_store(first.database, key)
    first.keep_copies(connection)
    recovery_key = first.make_recovery_key(key)  # kept in the notes as it is made
    connection.close()
    for path in (tmp_path / B, tmp_path / "backups" / B):
        path.unlink()

    later = Unlock(tmp_path, Store("absent"), FAST)
    assert not later.has_recovery_key
    connection = open_store(later.database, later.enter_password(PASSWORD))
    later.keep_copies(connection)
    connection.close()

    assert later.has_recovery_key
    assert later.open_with_recovery_key(recovery_key) == key
