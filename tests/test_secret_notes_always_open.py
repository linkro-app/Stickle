"""Secret notes always open, whatever happens on the way.

A state machine (Hypothesis) does what the user can do with secret notes and
with what opens them, in any order: turning them on, making notes secret,
editing them, making them ordinary again, deleting them and emptying the
trash, changing their password or setting a new one with the recovery key,
making a new recovery key, typing one made by 0.5, setting or changing the
master password, bringing notes in from another app, exporting, taking a
backup and going back to one. In between, copies of the key files are lost
or damaged (each where it is kept: the data folder, its backups folder, the
database), and Stickle is started again. Any step can be stopped at any
write on the way, as if the computer went off, or have that write fail.

After every step, Stickle started again from what is on disk (on a copy, so
that looking changes nothing) opens the notes, each as the user left it, and

- every secret note opens with a password the user has: the one set last,
  or, after a change that stopped or failed on the way, the one before;
- while Stickle says the recovery key was kept, the one the user was shown
  last opens the notes and the secret notes;
- where there is a master password, the user's opens the notes;
- each copy of a key file that can be read holds the same key, and opens
  with what the user has (left the only one, it brings back nothing older);
  a copy in the database whose writing failed may lag behind until the next
  start, as stickle.data.key_copies lets it;
- no record of a note brought in from another app keeps a hash of the text
  of a note made secret since.

Each run does only some of the steps (swarm testing), so that rare orders
come up. Bugs it found are kept below as the steps that showed them.
STICKLE_STATE_EXAMPLES sets how many runs there are (more for a long search).
"""

import base64
import contextlib
import errno
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,  # pyright: ignore[reportUnknownVariableType]
)
from PySide6.QtWidgets import QApplication
from test_unlock import FAST, Store

from stickle import unlock as unlocking
from stickle.app import application
from stickle.app.application import go_back_to, master_passwords, recovery_keys, secret_passwords
from stickle.app.master_password import MasterPasswords
from stickle.app.notes import NoteManager
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.secret_dialogs import SecretPasswords
from stickle.core.colors import DEFAULT_COLOR
from stickle.crypto import key_files, master, recovery
from stickle.crypto.key_files import NAMES, SECOND_FOLDER, B, C
from stickle.crypto.keyfile import (
    KeyFileError,
    WrongPasswordError,
    decode_slot,
    encode_slot,
    parse_document,
    slots_of,
    unwrap_with_password,
    wrap_with_password,
)
from stickle.crypto.recovery import WrongRecoveryKeyError
from stickle.crypto.sealed import SealedSlot, decode_sealed_slot, open_pair, unseal
from stickle.crypto.secret_notes import (
    PASSWORD_SLOT,
    RECOVERY_SLOT,
    NotKeptForRecoveryKeyError,
    SecretNoteError,
    open_text,
    seal_text,
)
from stickle.data.backups import (
    backups,
    cancel_restore,
    finish_restore,
    keep_key_state,
    keep_secret_notes_secret,
    old_secret_texts,
    secret_notes_known,
    stage_restore,
    take,
)
from stickle.data.export import JSON_NAME, SecretExport, export_all
from stickle.data.imports import SECRET_MARK
from stickle.data.key_copies import KeyCopies
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import RECOVERY_KEY_KEPT, Settings
from stickle.importers.base import ImportedNote, ImportResult
from stickle.importers.bring_in import bring_in
from stickle.unlock import DATABASE, Unlock, Unlocked

# 20 runs at each commit (about 40 s); a long search: STICKLE_STATE_EXAMPLES=1000.
EXAMPLES = int(os.environ.get("STICKLE_STATE_EXAMPLES", "20"))

PASSWORDS = st.sampled_from(
    ["first secret", "second secret", "세 번째 비밀 암호", "fourth ✓ secret"]
)
MASTERS = st.sampled_from(["first master password", "second master password"])
# As typed: never NUL, which the database refuses.
TEXTS = st.text(st.characters(exclude_categories=["Cs"], exclude_characters="\0"), max_size=12)
TITLES = st.sampled_from(["", "Bank", "은행"])
PICKS = st.integers(0, 50)
type How = Literal["stop", "fail"]
type Stop = tuple[int, How]
HOW: tuple[How, ...] = ("stop", "fail")
STOPS: st.SearchStrategy[Stop | None] = st.none() | st.tuples(
    st.integers(0, 8), st.sampled_from(HOW)
)
type Where = Literal["data", "second", "database"]
WHERE: tuple[Where, ...] = ("data", "second", "database")


class Stopped(BaseException):
    """Stickle stopped there, as if the computer went off: nothing after it runs."""


class Writes:
    """What Stickle writes, step by step, and at the step chosen either stopped
    there or that one write failing (a full disk, a file held by another program)."""

    def __init__(self, stop: Stop | None) -> None:
        self.stop = stop
        self.done = 0
        self.stopped = False
        self.failed = False
        # Each key file's copy in the database, by name: whether its last writing
        # went (False: it failed, as put lets be, and the copy is behind the file).
        self.copied: dict[str, bool] = {}

    @property
    def clean(self) -> bool:
        """Every write so far went as it should."""
        return not (self.stopped or self.failed)

    def step(self) -> bool:
        """Before a step: Stopped at the one chosen to stop at; True if this is
        the one chosen to fail."""
        stop, self.done = self.stop, self.done + 1
        if stop is None or self.done - 1 != stop[0]:
            return False
        if stop[1] == "stop":
            raise Stopped
        self.failed = True
        return True


@contextlib.contextmanager
def counted(writes: Writes) -> Generator[None]:
    """Each write of a key file, of a key file's copy in the database, and of
    going back to a backup, as a step of writes."""
    real_write, real_put = key_files.write_bytes, KeyCopies.put

    def write_bytes(path: Path, data: bytes) -> None:
        if writes.step():
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(path, data)

    def put(copies: KeyCopies, name: str, content: bytes) -> None:
        if writes.step():  # failing, it is logged and let be, as put does
            writes.copied[name] = False
        else:
            real_put(copies, name, content)
            writes.copied[name] = True

    def stepped[**P, R](real: Callable[P, R]) -> Callable[P, R]:
        def step(*args: P.args, **kwargs: P.kwargs) -> R:
            if writes.step():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*args, **kwargs)

        return step

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(key_files, "write_bytes", write_bytes))
        stack.enter_context(mock.patch.object(KeyCopies, "put", put))
        stack.enter_context(mock.patch.object(application, "take", stepped(take)))
        stack.enter_context(mock.patch.object(application, "stage_restore", stepped(stage_restore)))
        stack.enter_context(
            mock.patch.object(
                application, "keep_secret_notes_secret", stepped(keep_secret_notes_secret)
            )
        )
        stack.enter_context(
            mock.patch.object(application, "keep_key_state", stepped(keep_key_state))
        )
        yield


class Ticking:
    """A clock a second further on each time it is read."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 27, tzinfo=UTC)

    def __call__(self) -> str:
        self.moment += timedelta(seconds=1)
        return self.moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Saved:
    """The notes' windows (none here), all saved."""

    def save_all(self) -> None:
        pass


@dataclass(frozen=True)
class Expected:
    """A note as the user left it."""

    text: str
    secret: bool = False
    title: str = ""
    deleted: bool = False


@dataclass(frozen=True)
class Taken:
    """A backup, and the notes as they were when it was taken."""

    path: Path
    notes: dict[str, Expected]
    once_secret: frozenset[str]


# Each run does only some of these (swarm testing): a run without, say, any step
# that reads the secret notes' key goes from losing its files to going back to a
# backup far more often than one picking among them all.
SWARM = (
    "bring_in_notes",
    "make_secret",
    "edit_secret",
    "make_ordinary",
    "delete_note",
    "restore_note",
    "empty_from_trash",
    "change_password",
    "forget_password",
    "make_recovery_key",
    "type_old_recovery_key",
    "set_master_password",
    "lose_copies",
    "lose_files",
    "take_backup",
    "go_back",
    "export",
)


# Each one on or off by a coin of its own: about half of them in each run.
SWARMS = st.tuples(*(st.booleans() for _ in SWARM)).map(
    lambda on: frozenset(name for name, chosen in zip(SWARM, on, strict=True) if chosen)
)


def swarm(name: str, also: Callable[[Any], bool] | None = None) -> Callable[[Any], bool]:
    """A rule's precondition: the rule is one this run does (and also holds)."""
    return lambda state: name in state.enabled and (also is None or also(state))


def having(secret: bool | None, deleted: bool) -> Callable[[Any], bool]:
    """A rule's precondition: a note of that kind is there to pick."""
    return lambda state: state.picked(0, secret, deleted) is not None


def sound(data: bytes | None) -> bool:
    """Whether a key file's copy can be read (as stickle.crypto.key_files sees it)."""
    if data is None:
        return False
    try:
        slots_of(parse_document(data))
    except KeyFileError, KeyError, TypeError:
        return False
    return True


class SecretNotesMachine(RuleBasedStateMachine):
    unlock: Unlock
    connection: Any  # apsw.Connection
    notes: NoteRepository
    settings: Settings
    recovery: RecoveryKeys
    master: MasterPasswords
    secrets: SecretPasswords

    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="stickle-secret-"))
        self.folder = self.root / "data"
        self.folder.mkdir()
        self.clock = Ticking()
        self.store = Store("has_key")
        # Every recovery key made, shown or not, to read every copy of B with.
        self.made_recovery_keys: list[str] = []
        real_generate = unlocking.generate_recovery_key

        def generate() -> str:
            made = real_generate()
            self.made_recovery_keys.append(made)
            return made

        self.patches = contextlib.ExitStack()
        self.patches.enter_context(mock.patch.object(unlocking, "generate_recovery_key", generate))
        self.patches.enter_context(
            mock.patch.multiple(recovery, OPSLIMIT=FAST[0], MEMLIMIT=FAST[1])
        )
        # What the user knows and expects.
        self.expected: dict[str, Expected] = {}
        self.once_secret: set[str] = set()
        self.passwords: list[str] = []  # the secret notes' password is one of these
        self.typed_passwords: list[str] = []  # every one ever set
        self.recovery_keys: list[str] = []  # shown to the user and kept
        self.masters: list[str] = []
        self.typed_masters: list[str] = []
        self.secret_key: bytes | None = None
        self.old_recovery_key = False
        self.taken: list[Taken] = []
        self.opened: dict[tuple[bytes, str], bytes | None] = {}  # slots tried before
        self.enabled: frozenset[str] = frozenset(SWARM)  # what this run does
        # Key files whose copy in the database may be behind them (its writing
        # failed), until Stickle starts again or writes it.
        self.behind: set[str] = set()
        self.start()
        assert self.unlock.opened_key is not None
        self.database_key = self.unlock.opened_key

    def teardown(self) -> None:
        self.connection.close()
        self.patches.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # Stickle

    def start(self) -> None:
        """Stickle starting, as stickle.__main__ and its start-up do."""
        database = self.folder / DATABASE
        cancel_restore(database)  # one staged by a run that stopped is not put in place
        self.unlock = Unlock(self.folder, self.store, FAST, clock=self.clock, master_strength=FAST)
        outcome = self.unlock.outcome()
        assert isinstance(outcome, Unlocked), outcome
        self.connection = open_store(database, outcome.key, self.clock)
        self.unlock.opened_key = outcome.key
        self.unlock.keep_copies(self.connection)
        self.behind.clear()  # the copies in the database follow the files now
        self.notes = NoteRepository(self.connection, self.clock)
        self.notes.forget_old_text_left()
        self.settings = Settings(self.connection, self.clock)
        found = recovery_keys(self.unlock, self.settings, None, self.fits)
        masters = master_passwords(self.unlock)
        assert found is not None and masters is not None
        passwords = secret_passwords(self.unlock, masters, None, found, self.fits)
        assert passwords is not None
        self.recovery, self.master, self.secrets = found, masters, passwords

    def fits(self, key: bytes) -> bool:
        """As NoteManager.secret_key_fits: whether key opens a stored secret note."""
        stored = next((note for note in self.notes.all() if note.sealed is not None), None)
        if stored is None or stored.sealed is None:
            return True
        try:
            open_text(key, stored.id, stored.sealed)
        except SecretNoteError:
            return False
        return True

    @contextlib.contextmanager
    def writing(self, stop: Stop | None) -> Generator[Writes]:
        """A step of the user's, stopped or with a write failing as stop says;
        stopped, Stickle is started again."""
        writes = Writes(stop)
        try:
            with counted(writes):
                yield writes
        except Stopped:
            writes.stopped = True
            writes.copied.clear()  # started again: the copies follow the files
            self.connection.close()
            self.start()
        finally:
            for name, copied in writes.copied.items():
                (self.behind.discard if copied else self.behind.add)(name)

    # What the user does to get the secret notes' key

    def with_password(self) -> bytes:
        """As OpenSecretDialog: the user tries the passwords they may have, the
        one set last first. One of them opens the secret notes."""
        for password in reversed(self.passwords):
            try:
                return self.secrets.open(password)
            except WrongPasswordError:
                continue
        raise AssertionError("none of the user's passwords opens the secret notes")

    def with_recovery_key(self) -> bytes | None:
        """The secret notes' key through the recovery key the user has; None if
        it does not open them, which only Stickle saying it was not kept allows."""
        for typed in reversed(self.recovery_keys):
            try:
                return self.secrets.open_with_recovery_key(typed)
            except WrongRecoveryKeyError, NotKeptForRecoveryKeyError:
                continue
        assert not self.settings.get(RECOVERY_KEY_KEPT), "the recovery key kept does not open them"
        return None

    def new_password(self, key: bytes, password: str, writes: Writes, reset: bool) -> bool:
        """As NewSecretPasswordDialog; whether it was kept. Until it is kept for
        certain, the user still has the one before."""
        self.typed_passwords.append(password)
        self.passwords.append(password)
        try:
            (self.secrets.reset_password if reset else self.secrets.change_password)(key, password)
        except KeyFileError, OSError:
            return False
        if writes.clean:
            self.passwords = [password]
        return True

    def key(self, forgot: bool, password: str, writes: Writes) -> bytes | None:
        """The secret notes' key: with their password, or, forgotten, with the
        recovery key and a new password set at once (None if it was not)."""
        if not forgot:
            return self.with_password()
        key = self.with_recovery_key()
        if key is None or not self.new_password(key, password, writes, reset=True):
            return None
        return key

    def turn_on(self, password: str) -> bytes | None:
        """As TurnOnDialog: their key, made now; None if it could not be."""
        self.typed_passwords.append(password)
        self.passwords = [password]
        try:
            self.secret_key = self.secrets.turn_on(password)
        except KeyFileError, OSError:
            return None
        return self.secret_key

    def recovery_ready(self) -> bool:
        """As TurnOnDialog: a recovery key made since 0.5, and kept."""
        return self.recovery.exists() and not self.recovery.is_old() and self.recovery.was_kept()

    def picked(self, pick: int, secret: bool | None, deleted: bool) -> str | None:
        found = [
            note_id
            for note_id, note in self.expected.items()
            if note.deleted == deleted and (secret is None or note.secret == secret)
        ]
        return found[pick % len(found)] if found else None

    # The first start

    @initialize(old=st.booleans(), enabled=SWARMS)
    def first_start(self, old: bool, enabled: frozenset[str] = frozenset(SWARM)) -> None:
        """A recovery key made at the first start, and kept; or one made by 0.5."""
        self.enabled = enabled
        if old:
            made = recovery.generate()
            self.made_recovery_keys.append(made)
            slot = wrap_with_password(
                self.database_key,
                recovery.canonical(made),
                *FAST,
                slot_name=recovery.OLD_SLOT,
            )
            self.unlock.files.save(B, {recovery.OLD_SLOT: encode_slot(slot)})
            self.old_recovery_key = True
        else:
            made = self.recovery.make()
        self.recovery.kept()
        self.recovery_keys = [made]

    # Notes

    @rule(text=TEXTS)
    def write_note(self, text: str) -> None:
        note = self.notes.create(text)
        self.expected[note.id] = Expected(text)

    @precondition(swarm("bring_in_notes"))
    @rule(texts=st.lists(TEXTS, min_size=1, max_size=3))
    def bring_in_notes(self, texts: list[str]) -> None:
        """Notes of another app; brought in again, those here already are not."""
        result = ImportResult(
            [ImportedNote(f"note {i}", text, DEFAULT_COLOR) for i, text in enumerate(texts)]
        )
        brought = bring_in("Other app", result, self.connection, clock=self.clock)
        for note_id in brought.note_ids:
            note = self.notes.get(note_id)
            assert note is not None
            self.expected[note_id] = Expected(note.body)

    @precondition(swarm("make_secret", having(secret=False, deleted=False)))
    @rule(pick=PICKS, title=TITLES, forgot=st.booleans(), password=PASSWORDS, stop=STOPS)
    def make_secret(
        self, pick: int, title: str, forgot: bool, password: str, stop: Stop | None
    ) -> None:
        """As NoteManager.make_secret: turned on the first time."""
        note_id = self.picked(pick, secret=False, deleted=False)
        if note_id is None:
            return
        text = self.expected[note_id].text
        with self.writing(stop) as writes:
            stored = any(note.sealed is not None for note in self.notes.all())
            if self.secrets.turned_on() or stored:
                key = self.key(forgot, password, writes)
            elif self.recovery_ready():
                key = self.turn_on(password)
            else:
                return  # turning them on waits for a recovery key, kept
            if key is None:
                return
            self.notes.make_secret(note_id, title, seal_text(key, note_id, text))
            self.expected[note_id] = Expected(text, secret=True, title=title)

    @precondition(swarm("edit_secret", having(secret=True, deleted=False)))
    @rule(pick=PICKS, text=TEXTS, forgot=st.booleans(), password=PASSWORDS, stop=STOPS)
    def edit_secret(
        self, pick: int, text: str, forgot: bool, password: str, stop: Stop | None
    ) -> None:
        note_id = self.picked(pick, secret=True, deleted=False)
        if note_id is None:
            return
        with self.writing(stop) as writes:
            key = self.key(forgot, password, writes)
            if key is None:
                return
            self.notes.update_secret_body(note_id, seal_text(key, note_id, text))
            self.expected[note_id] = replace(self.expected[note_id], text=text)

    @precondition(swarm("make_ordinary", having(secret=True, deleted=False)))
    @rule(pick=PICKS, forgot=st.booleans(), password=PASSWORDS, stop=STOPS)
    def make_ordinary(self, pick: int, forgot: bool, password: str, stop: Stop | None) -> None:
        note_id = self.picked(pick, secret=True, deleted=False)
        if note_id is None:
            return
        with self.writing(stop) as writes:
            key = self.key(forgot, password, writes)
            if key is None:
                return
            note = self.notes.get(note_id)
            assert note is not None and note.sealed is not None
            text = open_text(key, note_id, note.sealed)  # what its window shows
            self.notes.make_ordinary(note_id, text)
            self.expected[note_id] = Expected(text)

    @precondition(swarm("delete_note", having(secret=None, deleted=False)))
    @rule(pick=PICKS)
    def delete_note(self, pick: int) -> None:
        note_id = self.picked(pick, secret=None, deleted=False)
        if note_id is not None:
            self.notes.delete(note_id)
            self.expected[note_id] = replace(self.expected[note_id], deleted=True)

    @precondition(swarm("restore_note", having(secret=None, deleted=True)))
    @rule(pick=PICKS)
    def restore_note(self, pick: int) -> None:
        note_id = self.picked(pick, secret=None, deleted=True)
        if note_id is not None:
            self.notes.restore(note_id)
            self.expected[note_id] = replace(self.expected[note_id], deleted=False)

    @precondition(swarm("empty_from_trash", having(secret=None, deleted=True)))
    @rule(pick=PICKS)
    def empty_from_trash(self, pick: int) -> None:
        note_id = self.picked(pick, secret=None, deleted=True)
        if note_id is not None:
            self.notes.purge(note_id)
            if self.expected.pop(note_id).secret:
                self.once_secret.add(note_id)

    # What opens them

    @precondition(swarm("change_password", lambda self: bool(self.passwords)))
    @rule(password=PASSWORDS, stop=STOPS)
    def change_password(self, password: str, stop: Stop | None) -> None:
        with self.writing(stop) as writes:
            if self.secrets.turned_on():
                self.new_password(self.with_password(), password, writes, reset=False)

    @precondition(swarm("forget_password", lambda self: bool(self.passwords)))
    @rule(password=PASSWORDS, stop=STOPS)
    def forget_password(self, password: str, stop: Stop | None) -> None:
        """Forgotten: the recovery key opens them, and a new password is set."""
        with self.writing(stop) as writes:
            if self.secrets.turned_on():
                self.key(True, password, writes)

    @precondition(swarm("make_recovery_key"))
    @rule(forgot=st.booleans(), password=PASSWORDS, stop=STOPS)
    def make_recovery_key(self, forgot: bool, password: str, stop: Stop | None) -> None:
        """As the settings window: with secret notes, their key opened first."""
        with self.writing(stop) as writes:
            make = self.recovery.make
            if self.secrets.turned_on():
                key = self.key(forgot, password, writes)
                make_with_secret = self.recovery.make_with_secret
                if key is None or make_with_secret is None:
                    return
                make = lambda: make_with_secret(key)  # noqa: E731
            try:
                made = make()
            except OSError, KeyFileError:
                return  # "could not be saved"
            if writes.step():
                return  # its window closed without saying it is kept
            self.old_recovery_key = False
            self.recovery.kept()
            self.recovery_keys = [made] if writes.clean else [*self.recovery_keys, made]

    @precondition(swarm("type_old_recovery_key", lambda self: self.old_recovery_key))
    @rule(stop=STOPS)
    def type_old_recovery_key(self, stop: Stop | None) -> None:
        """One made by 0.5, typed once: it opens secret notes too from then on."""
        with self.writing(stop):
            try:
                self.recovery.upgrade(self.recovery_keys[-1])
            except WrongRecoveryKeyError:
                self.old_recovery_key = False  # made a pair already, before it stopped
                return
            except KeyFileError, OSError:
                return
            self.old_recovery_key = False

    @precondition(swarm("set_master_password"))
    @rule(password=MASTERS, stop=STOPS)
    def set_master_password(self, password: str, stop: Stop | None) -> None:
        with self.writing(stop) as writes:
            self.typed_masters.append(password)
            if self.master.exists():
                current = next((m for m in reversed(self.masters) if self.master.check(m)), None)
                assert current is not None, "none of the user's master passwords is it"
                self.masters.append(password)
                try:
                    self.master.change(current, password)
                except KeyFileError, OSError:
                    return
            else:
                self.masters.append(password)
                try:
                    self.master.set(password)
                except KeyFileError, OSError:
                    return
            if writes.clean:
                self.masters = [password]

    @precondition(swarm("lose_copies"))
    @rule(
        name=st.sampled_from([B, C]),
        where=st.sets(st.sampled_from(WHERE), min_size=1, max_size=2),
        damage=st.booleans(),
    )
    def lose_copies(self, name: str, where: set[Where], damage: bool) -> None:
        """Copies of a key file deleted or damaged, while Stickle runs: never
        every one that can be read (as losing both what opens them)."""
        readable = {place for place in WHERE if sound(self.copy(name, place))}
        if name in self.behind:  # not a copy of what opens them now
            readable.discard("database")
        if not readable - where:
            return
        for place in where:
            if self.copy(name, place) is None:
                continue
            if place == "database":
                if damage:
                    self.connection.execute(
                        "UPDATE key_copies SET content = ? WHERE name = ?", (b"damaged", name)
                    )
                else:
                    self.connection.execute("DELETE FROM key_copies WHERE name = ?", (name,))
            elif damage:
                self.path(name, place).write_bytes(b'{"slots": "damaged')
            else:
                self.path(name, place).unlink()

    @precondition(swarm("lose_files"))
    @rule(name=st.sampled_from([B, C]), damage=st.booleans())
    def lose_files(self, name: str, damage: bool) -> None:
        """Both files of a key file gone (a clean-up tool, the data folder copied
        without its backups folder): the copy in the database is left."""
        self.lose_copies(name, {"data", "second"}, damage)

    @rule(stop=STOPS)
    def start_again(self, stop: Stop | None) -> None:
        self.connection.close()
        with self.writing(stop):
            self.start()

    # Backups and exports

    @precondition(swarm("take_backup"))
    @rule()
    def take_backup(self) -> None:
        path = take(self.connection, self.folder / DATABASE, self.clock)
        self.taken.append(Taken(path, dict(self.expected), frozenset(self.once_secret)))

    @precondition(swarm("go_back", lambda self: bool(self.taken)))
    @rule(pick=PICKS, with_password=st.booleans(), stop=STOPS)
    def go_back(self, pick: int, with_password: bool, stop: Stop | None) -> None:
        """As the restore window: the secret notes' password asked for when the
        backup holds the earlier text of notes secret now; Stickle starts again."""
        taken = self.taken[pick % len(self.taken)]
        database = self.folder / DATABASE
        before = {backup.path for backup in backups(database)}
        now = Taken(Path(), dict(self.expected), frozenset(self.once_secret))
        known = secret_notes_known(self.connection)
        sealed = with_password and old_secret_texts(taken.path, self.database_key, known) > 0
        after = self.gone_back(taken, sealed)
        with self.writing(stop) as writes:
            secret_key = self.with_password() if sealed else None
            with mock.patch.object(application, "_restore_staged", False):
                staged = go_back_to(
                    taken.path,
                    cast(NoteManager, Saved()),
                    self.connection,
                    database,
                    self.database_key,
                    lambda: None,
                    self.clock,
                    secret_key,
                )
            if staged:
                failed = writes.step()  # Stickle has quit: the copy put in place
                self.connection.close()
                if failed:
                    cancel_restore(database)
                elif finish_restore(database):
                    self.expected, self.once_secret = dict(after.notes), set(after.once_secret)
                self.start()
        for backup in backups(database):
            if backup.path not in before:  # the notes as they were, backed up first
                self.taken.append(replace(now, path=backup.path))

    def gone_back(self, taken: Taken, sealed: bool) -> Taken:
        """The notes once a backup is put in place: those secret now, or once and
        gone since, never plain in it (stickle.data.backups.keep_secret_notes_secret)."""
        secret_now = {i: note for i, note in self.expected.items() if note.secret}
        known = set(secret_now) | self.once_secret
        notes: dict[str, Expected] = {}
        once_secret = set(taken.once_secret)
        for note_id, note in taken.notes.items():
            if note_id not in known or note.secret or not note.text:
                notes[note_id] = note
            elif sealed:  # its earlier text, sealed
                title = secret_now[note_id].title if note_id in secret_now else ""
                notes[note_id] = replace(note, secret=True, title=title)
            elif note_id in secret_now:  # its text as it is now
                now = secret_now[note_id]
                notes[note_id] = replace(note, text=now.text, secret=True, title=now.title)
            else:  # gone since: left out
                once_secret.add(note_id)
        once_secret |= known - set(taken.notes)
        return Taken(taken.path, notes, frozenset(once_secret))

    @precondition(swarm("export"))
    @rule(encrypted=st.booleans(), stop=STOPS)
    def export(self, encrypted: bool, stop: Stop | None) -> None:
        """A full export: the secret notes in it open as they do in Stickle."""
        with self.writing(stop):
            key = None if encrypted or not self.secrets.turned_on() else self.with_password()
            result = export_all(
                self.connection,
                self.root / "exports",
                "test",
                self.clock,
                SecretExport(key=key, key_copies=self.unlock.secret_key_copies()),
            )
            document = json.loads((result.folder / JSON_NAME).read_text("utf-8"))
            rows = {str(row["id"]): row for row in document["tables"]["notes"]}
            assert rows.keys() == self.expected.keys()
            for note_id, expected in self.expected.items():
                row = rows[note_id]
                if expected.secret and key is None:
                    sealed = base64.b64decode(row["secret_body"]["base64"])
                    exported = self.password_key(json.dumps(document["secret_notes_key"]).encode())
                    assert exported is not None, "no password the user has opens the export"
                    assert open_text(exported, note_id, sealed) == expected.text
                else:
                    assert row["body"] == expected.text

    # What must hold

    @invariant()
    def started_again_everything_opens(self) -> None:
        copy = Path(tempfile.mkdtemp(dir=self.root))
        try:
            for name in NAMES:
                for place in ("data", "second"):
                    source = self.path(name, place)
                    if source.exists():
                        target = copy / source.relative_to(self.folder)
                        target.parent.mkdir(exist_ok=True)
                        shutil.copyfile(source, target)
            self.connection.execute("VACUUM INTO ?", (str(copy / DATABASE),))
            unlock = Unlock(copy, self.store, FAST, clock=self.clock, master_strength=FAST)
            assert unlock.outcome() == Unlocked(self.database_key, "credential store")
            connection = open_store(copy / DATABASE, self.database_key, self.clock)
            try:
                unlock.opened_key = self.database_key
                unlock.keep_copies(connection)
                self.check(unlock, connection)
            finally:
                connection.close()
        finally:
            shutil.rmtree(copy, ignore_errors=True)

    def check(self, unlock: Unlock, connection: Any) -> None:
        stored = {note.id: note for note in NoteRepository(connection).all()}
        assert stored.keys() == self.expected.keys()
        key = None
        if any(note.secret for note in self.expected.values()):
            assert unlock.has_secret_notes
        if unlock.has_secret_notes:
            key = next(
                (k for p in reversed(self.passwords) if (k := self.opens(unlock, p)) is not None),
                None,
            )
            assert key is not None, "none of the user's passwords opens the secret notes"
            if self.secret_key is None:
                self.secret_key = key
            assert key == self.secret_key
        for note_id, expected in self.expected.items():
            note = stored[note_id]
            assert (note.deleted, note.secret) == (expected.deleted, expected.secret)
            if expected.secret:
                assert key is not None and note.sealed is not None
                assert note.body == ""
                assert note.secret_title == expected.title
                assert open_text(key, note_id, note.sealed) == expected.text
            else:
                assert note.body == expected.text
        once = {str(row[0]) for row in connection.execute("SELECT note_id FROM once_secret")}
        assert once == self.once_secret
        if Settings(connection).get(RECOVERY_KEY_KEPT):
            assert any(
                self.recovery_opens(unlock, typed) == self.database_key
                for typed in self.recovery_keys
            ), "the recovery key kept does not open the notes"
            if unlock.has_secret_notes:
                assert any(
                    self.recovery_opens_secret(unlock, typed) == key for typed in self.recovery_keys
                ), "the recovery key kept does not open the secret notes"
        if unlock.has_master_password:
            assert any(
                self.master_opens(unlock, typed) == self.database_key for typed in self.masters
            ), "none of the user's master passwords opens the notes"
        told = connection.execute(
            "SELECT count(*) FROM import_records WHERE content_hash != ? AND (note_id IN"
            " (SELECT id FROM notes WHERE secret_body IS NOT NULL)"
            " OR note_id IN (SELECT note_id FROM once_secret))",
            (SECRET_MARK,),
        ).fetchall()[0][0]
        assert told == 0, "a record of a note brought in tells a secret note's text"

    @invariant()
    def copies_hold_the_same_key(self) -> None:
        """And each copy alone opens with what the user has: left the only one, it
        brings back no older password or recovery key."""
        pairs: dict[bytes, bytes] = {}  # the recovery key pairs in B's copies: public -> secret
        kept = self.settings.get(RECOVERY_KEY_KEPT)
        for place in WHERE:
            data = self.copy(B, place)
            if not sound(data):
                continue
            assert data is not None
            current = not (place == "database" and B in self.behind)
            slots = slots_of(parse_document(data))
            if recovery.SLOT in slots:
                slot = decode_sealed_slot(slots[recovery.SLOT])
                secret = self.pair_secret(slot, self.made_recovery_keys)
                assert secret is not None, "no recovery key made opens a copy of B"
                pairs[slot.public] = secret
                assert unseal(secret, slot.sealed) == self.database_key
                assert not (kept and current) or self.pair_secret(slot, self.recovery_keys), (
                    f"the {place} copy of B is not for the recovery key kept"
                )
            elif recovery.OLD_SLOT in slots:
                old = decode_slot(slots[recovery.OLD_SLOT])
                opens = [
                    made
                    for made in self.made_recovery_keys
                    if self.try_password(old, recovery.canonical(made), recovery.OLD_SLOT)
                    == self.database_key
                ]
                assert opens
                assert not (kept and current) or set(opens) & set(self.recovery_keys)
            if master.SLOT in slots:
                sealed_slot = decode_sealed_slot(slots[master.SLOT])
                assert any(
                    self.master_key(sealed_slot, typed) == self.database_key
                    for typed in (self.masters if current else self.typed_masters)
                ), f"the {place} copy of B opens with no master password the user has"
        for place in WHERE:
            data = self.copy(C, place)
            if not sound(data):
                continue
            assert data is not None
            key = self.password_key(data, every=True)
            assert key is not None, "no password set opens a copy of C"
            if self.secret_key is None:
                self.secret_key = key
            assert key == self.secret_key
            behind = place == "database" and C in self.behind
            assert behind or self.password_key(data) is not None, (
                f"the {place} copy of C opens only with an older password"
            )
            fields = cast(dict[str, object], slots_of(parse_document(data)).get(RECOVERY_SLOT))
            public = base64.b64decode(cast(str, fields["public"]))
            if public in pairs:
                sealed = base64.b64decode(cast(str, fields["sealed"]))
                assert unseal(pairs[public], sealed) == key

    # Reading what is on disk, changing nothing

    def path(self, name: str, place: Literal["data", "second"]) -> Path:
        return self.folder / name if place == "data" else self.folder / SECOND_FOLDER / name

    def copy(self, name: str, place: Where) -> bytes | None:
        if place == "database":
            rows = self.connection.execute(
                "SELECT content FROM key_copies WHERE name = ?", (name,)
            ).fetchall()
            return bytes(rows[0][0]) if rows else None
        try:
            return self.path(name, place).read_bytes()
        except FileNotFoundError:
            return None

    def try_password(self, slot: Any, typed: str, slot_name: str) -> bytes | None:
        remembered = (repr(slot).encode(), typed)
        if remembered not in self.opened:
            try:
                self.opened[remembered] = unwrap_with_password(slot, typed, slot_name)
            except WrongPasswordError, KeyFileError:
                self.opened[remembered] = None
        return self.opened[remembered]

    def password_key(self, data: bytes, every: bool = False) -> bytes | None:
        """The secret notes' key from a copy of C: with the passwords the user
        may have, or every one ever set."""
        slot = decode_slot(slots_of(parse_document(data))[PASSWORD_SLOT])
        tried = self.typed_passwords if every else self.passwords
        for password in reversed(tried):
            if (key := self.try_password(slot, password, PASSWORD_SLOT)) is not None:
                return key
        return None

    def pair_secret(self, slot: SealedSlot, keys: list[str]) -> bytes | None:
        """The pair's secret key, opened by one of these recovery keys."""
        for made in keys:
            remembered = (repr(slot).encode(), made)
            if remembered not in self.opened:
                try:
                    self.opened[remembered] = open_pair(
                        slot, recovery.canonical(made), recovery.SLOT
                    )
                except WrongPasswordError, KeyFileError:
                    self.opened[remembered] = None
            if self.opened[remembered] is not None:
                return self.opened[remembered]
        return None

    @staticmethod
    def master_key(slot: SealedSlot, typed: str) -> bytes | None:
        try:
            return master.open_with_master(slot, typed)
        except WrongPasswordError, KeyFileError:
            return None

    @staticmethod
    def opens(unlock: Unlock, password: str) -> bytes | None:
        try:
            return unlock.open_secret_notes(password)
        except WrongPasswordError:
            return None

    @staticmethod
    def recovery_opens(unlock: Unlock, typed: str) -> bytes | None:
        try:
            return unlock.open_with_recovery_key(typed)
        except WrongRecoveryKeyError:
            return None

    @staticmethod
    def recovery_opens_secret(unlock: Unlock, typed: str) -> bytes | None:
        try:
            return unlock.open_secret_notes_with_recovery_key(typed)
        except WrongRecoveryKeyError, NotKeptForRecoveryKeyError:
            return None

    @staticmethod
    def master_opens(unlock: Unlock, typed: str) -> bytes | None:
        try:
            return unlock.open_with_master_password(typed)
        except WrongPasswordError:
            return None


def test_secret_notes_always_open(qapp: QApplication) -> None:
    # A Qt application: going back to a backup quits it by a timer.
    assert qapp is not None
    run_state_machine_as_test(
        SecretNotesMachine,
        settings=settings(
            max_examples=EXAMPLES,
            stateful_step_count=40,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )


# What the machine found, kept as it found it.


@contextlib.contextmanager
def machine() -> Generator[SecretNotesMachine]:
    state = SecretNotesMachine()
    try:
        yield state
    finally:
        state.teardown()


def holds(state: SecretNotesMachine) -> None:
    state.started_again_everything_opens()
    state.copies_hold_the_same_key()


def test_a_new_recovery_key_written_but_never_shown_is_not_said_to_be_kept(
    qapp: QApplication,
) -> None:
    with machine() as state:
        state.first_start(old=False)
        state.make_recovery_key(forgot=False, password="first secret", stop=(1, "stop"))
        holds(state)


def test_going_back_brings_back_no_word_of_a_recovery_key_kept(qapp: QApplication) -> None:
    with machine() as state:
        state.first_start(old=False)
        state.write_note("a")
        state.take_backup()
        # The new key's window closed without saying it is kept.
        state.make_recovery_key(forgot=False, password="first secret", stop=(3, "fail"))
        state.go_back(pick=0, with_password=False, stop=None)
        holds(state)


def test_going_back_brings_back_no_older_key_file(qapp: QApplication) -> None:
    with machine() as state:
        state.first_start(old=False)
        state.write_note("a")
        state.make_secret(pick=0, title="", forgot=False, password="first secret", stop=None)
        state.take_backup()
        state.change_password("second secret", stop=None)
        state.lose_copies(C, {"data", "second"}, damage=False)
        state.go_back(pick=0, with_password=False, stop=None)
        holds(state)
