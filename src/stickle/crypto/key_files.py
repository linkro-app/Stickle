"""The key files: what opens the notes, each kept twice, under names left alone.

    A  the database key, wrapped with the password asked for at start where
       there is no credential store, and always for portable notes (once
       keys.json)
    B  the database key, opened by the recovery key, and later by the master
       password (once recovery.json)
    C  the secret notes' key (made once secret notes are used)

The names say nothing of notes or keys and have no extension, so that no one
opens, edits or clears them away as leftovers; each begins with a line saying
not to. Each is kept twice: in the data folder and, the same, in its backups
folder (not one of the dated backups: always the current one). The data
folder's copy is the one that counts. At start a missing copy is put back
from the other and the other is brought in line; a damaged one is set aside
(never deleted) and put back from the other, never copied over it. B and C
are also kept in the database (stickle.data.key_copies): with both files
gone, they come back from there once the notes are open. A is not: it is
needed to open them.

The old names are renamed the first time; while one cannot be, it is read as
it is. File names and folders are never logged.
"""

import logging
from pathlib import Path
from typing import Any, Protocol

from stickle.crypto.keyfile import (
    DOCUMENT_FIELDS,
    KeyFileError,
    document_bytes,
    parse_document,
    slots_of,
    write_bytes,
)

A = "STK_STRUCTURE_INTEGRITY_A"
B = "STK_STRUCTURE_INTEGRITY_B"
C = "STK_STRUCTURE_INTEGRITY_C"
NAMES = (A, B, C)
OLD_NAMES = {A: "keys.json", B: "recovery.json"}
IN_DATABASE = (B, C)
SECOND_FOLDER = "backups"
DAMAGED = ".damaged"
# For the log, which names no file.
WHAT = {A: "password key file", B: "recovery key file", C: "secret notes key file"}

log = logging.getLogger(__name__)


class Copies(Protocol):
    """Where B and C are kept a third time (the database)."""

    def get(self, name: str) -> bytes | None: ...

    def put(self, name: str, content: bytes) -> None: ...


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _sound(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        parse_document(data)
    except KeyFileError:
        return False
    return True


class KeyFiles:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.copies: Copies | None = None  # once the notes are open

    def path(self, name: str) -> Path:
        return self.folder / name

    def second_path(self, name: str) -> Path:
        return self.folder / SECOND_FOLDER / name

    # At start

    def prepare(self) -> None:
        """Old names renamed, each copy put right from the other. Nothing here
        stops Stickle: a file still wrong is reported when it is read."""
        for name in NAMES:
            try:
                self._rename_old(name)
                self._reconcile(name)
            except OSError as error:
                log.warning("could not look after the %s: %s", WHAT[name], type(error).__name__)

    def _rename_old(self, name: str) -> None:
        old_name = OLD_NAMES.get(name)
        if old_name is None:
            return
        old, new = self.folder / old_name, self.path(name)
        if old.exists() and not new.exists():
            old.replace(new)  # at once: never neither, never both
            log.info("%s renamed", WHAT[name])

    def _reconcile(self, name: str) -> None:
        data, second = self.path(name), self.second_path(name)
        first_bytes, second_bytes = _read(data), _read(second)
        if first_bytes is None:
            if second_bytes is not None:
                write_bytes(data, second_bytes)
                log.info("%s put back from its second copy", WHAT[name])
            return
        if not _sound(first_bytes):
            if _sound(second_bytes):
                assert second_bytes is not None
                data.replace(data.with_name(data.name + DAMAGED))
                write_bytes(data, second_bytes)
                log.warning("%s damaged: set aside, put back from its second copy", WHAT[name])
            return  # never copied over the other
        if "warning" not in parse_document(first_bytes):  # from before 0.6
            first_bytes = document_bytes(
                slots_of(parse_document(first_bytes)), parse_document(first_bytes)
            )
            write_bytes(data, first_bytes)
        if second_bytes != first_bytes:
            self._write_second(name, first_bytes)

    def _write_second(self, name: str, content: bytes) -> None:
        self.second_path(name).parent.mkdir(parents=True, exist_ok=True)
        write_bytes(self.second_path(name), content)

    # Reading and writing

    def raw(self, name: str) -> bytes | None:
        """The file as it is, or None when there is none (nor under its old name)."""
        found = _read(self.path(name))
        if found is None and name in OLD_NAMES:
            found = _read(self.folder / OLD_NAMES[name])  # not renamed (yet)
        return found

    def load(self, name: str) -> dict[str, object] | None:
        """The file's slots, None without one, KeyFileError if it cannot be read."""
        data = self.raw(name)
        return None if data is None else slots_of(parse_document(data))

    def save(self, name: str, slots: dict[str, Any]) -> None:
        """Written in the data folder, then its second copy, then the database.
        Fields of the file this version does not know are kept."""
        kept: dict[str, object] = {}
        current = self.raw(name)
        if _sound(current):
            assert current is not None
            kept = {k: v for k, v in parse_document(current).items() if k not in DOCUMENT_FIELDS}
        content = document_bytes(slots, kept)
        write_bytes(self.path(name), content)
        if name in OLD_NAMES:
            # Read before the new name, had it not been renamed: no longer.
            (self.folder / OLD_NAMES[name]).unlink(missing_ok=True)
        self._write_second(name, content)
        if self.copies is not None and name in IN_DATABASE:
            self.copies.put(name, content)

    def set_aside(self, name: str) -> None:
        """A damaged file moved out of the way (both copies), never deleted: it may
        hold what a newer version understands."""
        for path in (self.path(name), self.second_path(name)):
            if path.exists():
                path.replace(path.with_name(path.name + DAMAGED))
        if name in OLD_NAMES:
            old = self.folder / OLD_NAMES[name]
            if old.exists():
                old.replace(old.with_name(old.name + DAMAGED))

    # The database's copies

    def keep_in(self, copies: Copies) -> None:
        """From now on B and C are kept in the database too. With neither file
        left, they come back from it; otherwise its copy follows the file (a
        database brought back from a backup holds older ones)."""
        self.copies = copies
        for name in IN_DATABASE:
            try:
                current = self.raw(name)
                stored = copies.get(name)
                if current is None:
                    if stored is not None and _sound(stored):
                        write_bytes(self.path(name), stored)
                        self._write_second(name, stored)
                        log.info("%s put back from the notes", WHAT[name])
                elif _sound(current) and stored != current:
                    copies.put(name, current)
            except OSError as error:
                log.warning("could not keep the %s: %s", WHAT[name], type(error).__name__)
