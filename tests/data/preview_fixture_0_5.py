"""Notes and key files as the 0.5.1 preview leaves them, kept to prove later
versions open them.

make() wrote fixtures/preview-0.5.1/ once, with the code of that release (run
from the v0.5.1-preview.1 tag), in the two ways a computer keeps the key:

    password/  notes.db, keys.json (the password's slot), recovery.json
    store/     notes.db, recovery.json (the key was in the credential store)

The files are never changed afterwards: tests work on a copy. To see how they
were made:

    uv run python tests/data/preview_fixture_0_5.py <folder>

The notes are made up; the keys, password and recovery key are for tests.
"""

import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "preview-0.5.1"
SCHEMA_VERSION = 6
PASSWORD = "0.5 시험 암호 password"
RECOVERY_KEY = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q0"
STORE_KEY = bytes(range(96, 128))  # the key the credential store held, in store/
NOTES = ["# 0.5에서 쓴 메모\n- [ ] 우유", "Second note, **bold**"]
# Light key stretching, so the tests open the password's slot quickly (the
# strength is stored with the slot).
STRENGTH = (1, 8192)


def make(folder: Path) -> None:
    """Run with the 0.5.1 code: write both folders."""
    from stickle.crypto.recovery import canonical, write_recovery
    from stickle.data.notes import NoteRepository
    from stickle.data.schema import open_store, schema_version
    from stickle.platform.credentials import CredentialStore
    from stickle.unlock import Unlock

    def write_notes(path: Path, key: bytes) -> None:
        connection = open_store(path, key)
        assert schema_version(connection) == SCHEMA_VERSION
        notes = NoteRepository(connection)
        for body in NOTES:
            notes.create(body)
        connection.pragma("wal_checkpoint", "TRUNCATE")
        connection.close()

    def no_store() -> CredentialStore:
        raise AssertionError("password mode reads no credential store")

    canonical(RECOVERY_KEY)  # a well-formed key
    password = folder / "password"
    password.mkdir(parents=True)
    key = Unlock(password, no_store, STRENGTH, portable=True).create_password(PASSWORD)
    # As 0.5.1 called it (a folder, no time); this version's takes other arguments.
    write_recovery(password, key, RECOVERY_KEY)  # pyright: ignore[reportCallIssue, reportArgumentType]
    write_notes(password / "notes.db", key)

    store = folder / "store"
    store.mkdir()
    write_recovery(store, STORE_KEY, RECOVERY_KEY)  # pyright: ignore[reportCallIssue, reportArgumentType]
    write_notes(store / "notes.db", STORE_KEY)


if __name__ == "__main__":
    make(Path(sys.argv[1]))
