"""Find the key that opens the notes database.

The key is random and lives either in the OS credential store or, where there
is none, in the key file A wrapped with the user's password (stickle.crypto.
key_files). Which one is decided from files alone, so the credential store
(and its unlock prompt) is never touched in password mode. Portable notes
(stickle.platform.paths) are always in password mode: they go from computer
to computer.

A new key is created only while no database exists. With a database present,
a key that cannot be found or read is reported, never replaced: a new key
could not open the existing notes.
"""

import logging
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.crypto import secret_notes
from stickle.crypto.key_files import A, C, KeyFiles
from stickle.crypto.keyfile import (
    MEMLIMIT,
    OPSLIMIT,
    KeyFile,
    KeyFileError,
    PasswordSlot,
    WrongPasswordError,
    key_file_of,
    key_file_slots,
    parse_document,
    unwrap_with_password,
    wrap_with_password,
)
from stickle.crypto.master import MEMLIMIT as MASTER_MEMLIMIT
from stickle.crypto.master import OPSLIMIT as MASTER_OPSLIMIT
from stickle.crypto.master import change_master, open_with_master, read_master, set_master
from stickle.crypto.recovery import (
    WrongRecoveryKeyError,
    make_recovery_slot,
    open_recovery_pair,
    pair_for_export,
    read_recovery,
    save_recovery_slot,
)
from stickle.crypto.recovery import generate as generate_recovery_key
from stickle.crypto.recovery import unwrap as unwrap_recovery
from stickle.crypto.sealed import SealedSlot
from stickle.data.key_copies import KeyCopies
from stickle.platform.credentials import (
    DATABASE_KEY,
    KEY_BYTES,
    CredentialStore,
    CredentialStoreUnavailableError,
    KeyMissingError,
    KeyRequest,
)

DATABASE = "notes.db"
MIN_PASSWORD_LENGTH = 8

log = logging.getLogger(__name__)

type Blocker = Literal["store_unavailable", "key_missing", "key_file_unreadable"]


@dataclass(frozen=True)
class Unlocked:
    key: bytes
    source: Literal["credential store", "password"]


@dataclass(frozen=True)
class NeedPassword:
    create: bool  # True: first start without a credential store


@dataclass(frozen=True)
class Blocked:
    problem: Blocker


type Outcome = Unlocked | NeedPassword | Blocked


def password_length(password: str) -> int:
    return len(unicodedata.normalize("NFC", password))


class Unlock:
    """Started before Qt loads, so a credential store lookup overlaps it."""

    def __init__(
        self,
        folder: Path,
        store: Callable[[], CredentialStore] = CredentialStore,
        strength: tuple[int, int] = (OPSLIMIT, MEMLIMIT),  # lowered in tests
        portable: bool = False,
        clock: Clock = utc_now,
        master_strength: tuple[int, int] = (MASTER_OPSLIMIT, MASTER_MEMLIMIT),  # lowered in tests
    ) -> None:
        self.folder = folder
        self.files = KeyFiles(folder)
        self._store = store
        self._strength = strength
        self._master_strength = master_strength
        self._clock = clock
        # Carried from computer to computer: always a password, and the
        # credential store of the computer it is on is never read or written.
        self.portable = portable
        # The key that opened the notes, once they are open (to make a recovery key).
        self.opened_key: bytes | None = None
        # No notes existed when Stickle started: this is its first start here.
        self.first_start = not self.database.exists()
        self.start()

    @property
    def database(self) -> Path:
        return self.folder / DATABASE

    @property
    def key_file(self) -> Path:
        return self.files.path(A)

    def start(self) -> None:
        """(Re)read the files and, unless in password mode, ask the store for the key."""
        self._request: KeyRequest | None = None
        self._key_file: KeyFile | None = None
        self._unreadable = False
        self._database_existed = self.database.exists()
        self.files.prepare()
        try:
            slots = self.files.load(A)
            self._key_file = None if slots is None else key_file_of(slots)
        except KeyFileError, OSError:
            self._unreadable = True
            return
        has_password = self._key_file is not None and self._key_file.has_password
        if not has_password and not self.portable:
            self._request = KeyRequest(store=self._store, create=not self._database_existed)

    def outcome(self) -> Outcome:
        if self._unreadable:
            return Blocked("key_file_unreadable")
        if self._key_file is not None and self._key_file.has_password:
            return NeedPassword(create=False)
        if self.portable:
            # Notes brought here without a password (copied from a computer
            # that kept the key itself) open with the recovery key.
            return Blocked("key_missing") if self._database_existed else NeedPassword(create=True)
        assert self._request is not None
        try:
            return Unlocked(self._request.key(), "credential store")
        except KeyMissingError:
            return Blocked("key_missing")
        except CredentialStoreUnavailableError as error:
            log.info("credential store unavailable: %s", error)
            if self._database_existed:
                return Blocked("store_unavailable")
            return NeedPassword(create=True)

    def enter_password(self, password: str) -> bytes:
        """The key, or WrongPasswordError."""
        assert self._key_file is not None and self._key_file.password is not None
        return unwrap_with_password(self._key_file.password, password)

    def create_password(self, password: str) -> bytes:
        """Make a new key protected by this password, before any database exists."""
        if self.database.exists() or (self._key_file is not None and self._key_file.has_password):
            raise RuntimeError("a key already exists; creating one would lock the notes out")
        if password_length(password) < MIN_PASSWORD_LENGTH:
            raise ValueError("password too short")
        key = secrets.token_bytes(KEY_BYTES)
        others = self._key_file.other_slots if self._key_file is not None else {}
        # Written before the database, so a database never exists without its key.
        self._write_password(KeyFile(self._wrap(key, password), others))
        return key

    def _wrap(self, key: bytes, password: str) -> PasswordSlot:
        return wrap_with_password(key, password, *self._strength, set_at=self._clock())

    def _write_password(self, key_file: KeyFile) -> None:
        self.files.save(A, key_file_slots(key_file))
        slots = self.files.load(A)
        self._key_file = None if slots is None else key_file_of(slots)

    # The recovery key (stickle.crypto.recovery)

    @property
    def has_recovery_key(self) -> bool:
        try:
            return read_recovery(self.files) is not None
        except KeyFileError, OSError:
            return False

    @property
    def recovery_key_made_at(self) -> str | None:
        """When it was made (UTC), None without one or for one made before 0.6."""
        try:
            slot = read_recovery(self.files)
        except KeyFileError, OSError:
            return None
        return None if slot is None else slot.set_at

    def open_with_recovery_key(self, typed: str) -> bytes:
        """The key, or RecoveryKeyTypoError / WrongRecoveryKeyError.

        Nothing is written here: the key is kept (keep_recovered_key or
        set_password) only once it has opened the database.
        """
        slot = read_recovery(self.files)
        if slot is None:
            raise WrongRecoveryKeyError("no recovery key was made for these notes")
        return unwrap_recovery(slot, typed)

    @property
    def uses_password(self) -> bool:
        """Whether the notes open with a password rather than the credential store."""
        return self.portable or (self._key_file is not None and self._key_file.has_password)

    @property
    def recovered_key_needs_password(self) -> bool:
        """Whether a key opened with the recovery key (or the master password)
        needs a new password to be kept: in password mode (the password was
        forgotten) or with its key file damaged."""
        if self._unreadable or self.portable:
            return True
        return self._key_file is not None and self._key_file.has_password

    def keep_recovered_key(self, key: bytes) -> None:
        """Put the key back in the credential store (or CredentialStoreUnavailableError)."""
        if self.portable:
            raise CredentialStoreUnavailableError("portable notes keep no key on this computer")
        store = self._store()
        try:
            store.write(DATABASE_KEY, key)
            if store.read(DATABASE_KEY) != key:
                raise CredentialStoreUnavailableError("the store did not keep the key")
        finally:
            store.close()

    def set_password(self, key: bytes, password: str) -> None:
        """Protect an existing key (known to open the database) with a new password.

        A damaged key file is set aside, never deleted: it may hold something
        a newer version understands.
        """
        if password_length(password) < MIN_PASSWORD_LENGTH:
            raise ValueError("password too short")
        others: dict[str, object] = {}
        if self._unreadable:
            self.files.set_aside(A)
        elif self._key_file is not None:
            others = self._key_file.other_slots
        self._write_password(KeyFile(self._wrap(key, password), others))
        self._unreadable = False

    def make_recovery_key(self, key: bytes, secret_key: bytes | None = None) -> str:
        """A new recovery key for key (known to open the database); the old one
        stops working. With secret notes, their key (known to open them) is
        sealed for it too, first: should Stickle stop in between, the old pair
        is still the one in B, and the secret notes are sealed for it again
        once opened with their password (keep_secret_notes_for_recovery_key)."""
        recovery_key = generate_recovery_key()
        slot = make_recovery_slot(key, recovery_key, self._clock())
        if self.has_secret_notes:
            if secret_key is not None:
                secret_notes.keep_for_recovery_key(self.files, secret_key, slot.public)
            else:
                log.warning("new recovery key made without the secret notes' key")
        save_recovery_slot(self.files, slot)
        return recovery_key

    def _recovery_pair(self) -> SealedSlot | None:
        """The recovery key's pair; None without one, or for one from before 0.6."""
        try:
            slot = read_recovery(self.files)
        except KeyFileError, OSError:
            return None
        return slot if isinstance(slot, SealedSlot) else None

    @property
    def recovery_key_is_old(self) -> bool:
        """Whether the recovery key was made before 0.6: it opens the notes, but
        nothing can be sealed for it until it is typed once (upgrade_recovery_key)."""
        return self.has_recovery_key and self._recovery_pair() is None

    def upgrade_recovery_key(self, typed: str) -> None:
        """An old recovery key, typed, made a pair: the same key opens the notes,
        and now secret notes can be sealed for it. RecoveryKeyTypoError or
        WrongRecoveryKeyError if it is not the one these notes have."""
        slot = read_recovery(self.files)
        if slot is None or isinstance(slot, SealedSlot):
            raise WrongRecoveryKeyError("no recovery key from before 0.6")
        key = unwrap_recovery(slot, typed)
        if self.opened_key is not None and key != self.opened_key:
            raise WrongRecoveryKeyError("it opens other notes")
        # Its date unknown, as before: it was not made now.
        save_recovery_slot(self.files, make_recovery_slot(key, typed, slot.set_at))

    # The master password (stickle.crypto.master)

    def _master(self) -> SealedSlot | None:
        try:
            return read_master(self.files)
        except KeyFileError, OSError:
            return None

    @property
    def has_master_password(self) -> bool:
        return self._master() is not None

    @property
    def master_password_set_at(self) -> str | None:
        """When it was set or last changed (UTC), None without one."""
        slot = self._master()
        return None if slot is None else slot.set_at

    def open_with_master_password(self, typed: str) -> bytes:
        """The key, or WrongPasswordError. Nothing is written, as with the recovery key."""
        slot = self._master()
        if slot is None:
            raise WrongPasswordError("no master password was set for these notes")
        return open_with_master(slot, typed)

    def is_master_password(self, typed: str) -> bool:
        """Whether typed is the master password (checked, nothing changed)."""
        try:
            self.open_with_master_password(typed)
        except WrongPasswordError, KeyFileError:
            return False
        return True

    def opens_with_password(self, typed: str) -> bool:
        """Whether typed is the password that opens the notes (none on a computer
        whose credential store keeps the key)."""
        if self._key_file is None or self._key_file.password is None:
            return False
        try:
            unwrap_with_password(self._key_file.password, typed)
        except WrongPasswordError, KeyFileError:
            return False
        return True

    def set_master_password(self, key: bytes, password: str) -> None:
        """A master password for key (known to open the database), in place of
        one forgotten; the key file's other slots stay."""
        set_master(self.files, key, password, self._clock(), self._master_strength)

    def change_master_password(self, current: str, new: str) -> None:
        """The same master password's pair, opened by new (WrongPasswordError if
        current is not the master password)."""
        change_master(self.files, current, new, self._clock(), self._master_strength)

    # The secret notes' key (stickle.crypto.secret_notes)

    @property
    def has_secret_notes(self) -> bool:
        """Whether their key file is there (in some copy, even damaged). One that
        cannot be read at all counts as there: never offer to turn them on over it."""
        try:
            return secret_notes.has_secret_notes(self.files)
        except OSError:
            return True

    @property
    def secret_password_set_at(self) -> str | None:
        """When the secret notes' password was set or last changed (UTC)."""
        try:
            return secret_notes.password_set_at(self.files) if self.has_secret_notes else None
        except KeyFileError, OSError:
            return None

    def turn_on_secret_notes(self, password: str) -> bytes:
        """The secret notes' key, made now and kept for password and the recovery
        key, which must be a pair first (made, or an old one upgraded); the key."""
        pair = self._recovery_pair()
        if pair is None:
            raise KeyFileError("a recovery key is made before secret notes")
        return secret_notes.create(self.files, password, pair.public, self._clock(), self._strength)

    def open_secret_notes(self, password: str) -> bytes:
        """The secret notes' key; WrongPasswordError if password is not theirs."""
        return secret_notes.open_with_password(self.files, password)

    def open_secret_notes_with_recovery_key(self, typed: str) -> bytes:
        """The secret notes' key through the recovery key: RecoveryKeyTypoError,
        WrongRecoveryKeyError, or NotKeptForRecoveryKeyError if they are not
        sealed for the one these notes have now. Nothing is written."""
        pair = self._recovery_pair()
        if pair is None:
            raise secret_notes.NotKeptForRecoveryKeyError("no recovery key pair")
        return secret_notes.open_with_recovery_key(
            self.files, open_recovery_pair(pair, typed), pair.public
        )

    @property
    def secret_notes_kept_for_recovery_key(self) -> bool:
        """Whether the secret notes open with the recovery key these notes have
        now (True where there is nothing to seal them for: no secret notes, no pair)."""
        pair = self._recovery_pair()
        if pair is None or not self.has_secret_notes:
            return True
        try:
            return secret_notes.recovery_public(self.files) == pair.public
        except KeyFileError, OSError:
            return True  # not to be written over: C is damaged or out of reach

    def keep_secret_notes_for_recovery_key(self, key: bytes) -> None:
        """The secret notes' key (known to open them) sealed for the recovery
        key's pair: after one made without it, or before they were."""
        pair = self._recovery_pair()
        if pair is not None and self.has_secret_notes:
            secret_notes.keep_for_recovery_key(self.files, key, pair.public)

    def opens_secret_notes(self, typed: str) -> bool:
        """Whether typed is the secret notes' password (checked, nothing changed)."""
        if not self.has_secret_notes:
            return False
        try:
            self.open_secret_notes(typed)
        except WrongPasswordError, KeyFileError:
            return False
        return True

    def change_secret_password(self, key: bytes, password: str) -> None:
        secret_notes.change_password(self.files, key, password, self._clock(), self._strength)

    def secret_key_copies(self) -> dict[str, object] | None:
        """The secret notes' key file as it is (their key, locked), to go out with
        them when they are exported encrypted; None without one."""
        try:
            raw = self.files.raw(C)
            return None if raw is None else parse_document(raw)
        except KeyFileError, OSError:
            return None

    def recovery_pair(self) -> dict[str, object] | None:
        """The recovery key's pair as kept, to go out with the secret notes
        exported encrypted (their key is sealed for it); None without one."""
        return pair_for_export(self.files)

    def keep_copies(self, connection: apsw.Connection) -> None:
        """The notes are open: the key files they hold a copy of are kept there too,
        and come back from there if both of their files were lost."""
        self.files.keep_in(KeyCopies(connection))
