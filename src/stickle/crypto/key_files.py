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
needed to open them. While Stickle runs it is the same: a copy gone or
damaged is put back from a sound one (the second, then the database's), the
database's then made the same, and set aside only when none can be read. The
data folder's copy is written after the second: a writing that fails leaves
it as it was.

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
        slots = slots_of(parse_document(data))
    except KeyFileError, KeyError, TypeError:
        return False
    return isinstance(slots, dict)  # pyright: ignore[reportUnnecessaryIsInstance]


def _aside(path: Path) -> None:
    """A damaged copy moved out of the way, never deleted: it may hold what a newer
    version understands. One set aside before stays too."""
    target = path.with_name(path.name + DAMAGED)
    number = 1
    while target.exists():
        number += 1
        target = path.with_name(f"{path.name}{DAMAGED}-{number}")
    path.replace(target)


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
                _aside(data)
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
        """The file as it is, or None when there is none (nor under its old name).

        A copy gone while Stickle runs (a clean-up tool, a sync) is put back from
        the second one, or for B and C from the database's: never taken for
        "none", which would let a new key be written over the others. A damaged
        one is set aside and put back the same way, when another copy can be
        read: never taken for one to set aside with the others, which would let
        the slots only those hold be left out of the next writing. With none, it
        is what is there (KeyFileError when read)."""
        found = _read(self.path(name))
        if found is None and name in OLD_NAMES:
            found = _read(self.folder / OLD_NAMES[name])  # not renamed (yet)
            if found is not None:
                return found
        if found is None:
            return self._put_back(name)
        if not _sound(found):
            return self._put_back(name, damaged=True) or found
        return found

    def _put_back(self, name: str, damaged: bool = False) -> bytes | None:
        """A sound copy in place of the data folder's, gone or (damaged) set
        aside first; None, with nothing changed, without one."""
        try:
            stored = self.copies.get(name) if self.copies and name in IN_DATABASE else None
        except OSError as error:
            log.warning("could not read the %s: %s", WHAT[name], type(error).__name__)
            stored = None
        second = _read(self.second_path(name))
        for kept in (second, stored):
            if kept is not None and _sound(kept):
                try:
                    if damaged:
                        _aside(self.path(name))
                    if second is not None and not _sound(second):
                        _aside(self.second_path(name))
                    write_bytes(self.path(name), kept)
                    if _read(self.second_path(name)) is None:
                        self._write_second(name, kept)
                    if self.copies is not None and name in IN_DATABASE and stored != kept:
                        self.copies.put(name, kept)  # the notes' copy follows it too
                    how = "damaged: set aside" if damaged else "gone"
                    log.warning("%s %s while running, put back", WHAT[name], how)
                except OSError as error:
                    log.warning("could not put back the %s: %s", WHAT[name], type(error).__name__)
                return kept
        return None

    def load(self, name: str) -> dict[str, object] | None:
        """The file's slots, None without one, KeyFileError if it cannot be read."""
        data = self.raw(name)
        return None if data is None else slots_of(parse_document(data))

    def save(self, name: str, slots: dict[str, Any]) -> None:
        """Written in the second copy, then the data folder, then the database:
        should a writing fail, the data folder's copy, the one read, is still the
        old one, as the failure says (the second follows it at the next start).
        Fields of the file this version does not know are kept."""
        kept: dict[str, object] = {}
        current = self.raw(name)
        if _sound(current):
            assert current is not None
            kept = {k: v for k, v in parse_document(current).items() if k not in DOCUMENT_FIELDS}
        content = document_bytes(slots, kept)
        self._write_second(name, content)
        write_bytes(self.path(name), content)
        if name in OLD_NAMES:
            # Read before the new name, had it not been renamed: no longer.
            (self.folder / OLD_NAMES[name]).unlink(missing_ok=True)
        if self.copies is not None and name in IN_DATABASE:
            self.copies.put(name, content)

    def set_aside(self, name: str) -> None:
        """A damaged file moved out of the way (both copies), never deleted: it may
        hold what a newer version understands. Only once no copy of it can be read
        (raw puts a sound one back)."""
        paths = [self.path(name), self.second_path(name)]
        if name in OLD_NAMES:
            paths.append(self.folder / OLD_NAMES[name])
        for path in paths:
            if path.exists():
                _aside(path)

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
