"""Notes and key files as the 0.6 preview leaves them, kept to prove later
versions open them.

make() wrote fixtures/preview-0.6.0/ once, with the code of that release (run
from the v0.6.0-preview.1 tag): portable notes, so the key is behind a
password, with a recovery key, secret notes, a reminder and an own colour,
and notes placed on screen in each way that version kept places: a main place
alone, a main and a spare place stored with different numbers of monitors,
two stored with the same number, and a place no version could read.

The files are never changed afterwards: tests work on a copy. To see how they
were made:

    uv run python tests/data/preview_fixture_0_6.py <folder>

Only plain values are kept here, so that this file still imports once the
code that wrote them has changed. The notes are made up; the keys, passwords
and recovery key are for tests.
"""

import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "preview-0.6.0"
SCHEMA_VERSION = 11
PASSWORD = "0.6 시험 암호 password"
RECOVERY_KEY = "XR2W-FFF1-69M0-3XM9-9S59-JHQV-AM07-5E3B"
SECRET_PASSWORD = "비밀 메모 암호 0.6"
# Light key stretching, so the tests open the password's slots quickly (the
# strength is stored with each slot).
STRENGTH = (1, 8192)

_times = iter(f"2026-11-06T09:{minute:02d}:00.000Z" for minute in range(60))


def clock() -> str:
    return next(_times)


OWN_COLOR = "#3a7bd5"
# The notes, in the order made: (body, colour, hidden).
NOTES = [
    ("# 0.6에서 쓴 메모\n- [ ] 우유", "yellow", False),
    ("두 모니터에 둔 메모", "mint", False),
    ("같은 수의 여벌이 있는 메모", "blue", False),
    ("자리를 읽을 수 없는 메모", "pink", False),
    ("알림이 있는 메모", OWN_COLOR, False),
    ("숨긴 메모", "lavender", True),
]
ONE, TWO, SAME, UNREADABLE, REMINDED, HIDDEN = range(len(NOTES))
SECRET_TITLE = "비밀 메모 제목"
SECRET_TEXT = "비밀 메모 본문 secret text"
TRASHED = "휴지통에 넣은 메모"

# Where notes were put, as 0.6 kept them: (slot, monitor number, monitor name,
# rel_x, rel_y, width, height, names of the monitors connected then). 0.6 kept
# one size for both slots.
DELL, LG, BENQ = "DELL U2720Q", "LG HDR 4K", "BenQ GW2480"
PLACES = {
    ONE: [("main", 1, DELL, 0.25, 0.15, 300, 340, (DELL,))],
    TWO: [
        ("main", 2, LG, 0.6, 0.1, 280, 200, (DELL, LG)),
        ("spare", 1, DELL, 0.4, 0.5, 280, 200, (DELL,)),
    ],
    SAME: [
        ("main", 2, LG, 0.3, 0.3, 260, 220, (DELL, LG)),
        ("spare", 1, DELL, 0.7, 0.2, 260, 220, (DELL, BENQ)),
    ],
}
UNREADABLE_ROW = ("main", "not json", 0.5, 0.5, 250, 250)  # monitor_key not JSON

# The reminder of NOTES[REMINDED]: (year, month, day, hour, minute, time zone).
REMINDER = (2027, 1, 15, 9, 0, "Asia/Seoul")
NEWS_SEEN_SET = "0.6.0"
DEFAULT_COLOR_SET = "mint"


def make(folder: Path) -> None:
    """Run with the 0.6 code: write the folder."""
    from datetime import date, time

    from stickle.core.layout import Place
    from stickle.core.reminders import Rule
    from stickle.crypto.recovery import canonical, write_recovery
    from stickle.crypto.secret_notes import seal_text
    from stickle.data.colors import ColorRepository
    from stickle.data.layouts import LayoutRepository
    from stickle.data.notes import NoteRepository
    from stickle.data.reminders import ReminderRepository
    from stickle.data.schema import open_store, schema_version
    from stickle.data.settings import (
        DEFAULT_NOTE_COLOR,
        NEWS_SEEN,
        RECOVERY_KEY_KEPT,
        Settings,
    )
    from stickle.platform.credentials import CredentialStore
    from stickle.unlock import Unlock

    def no_store() -> CredentialStore:
        raise AssertionError("portable notes read no credential store")

    canonical(RECOVERY_KEY)  # a well-formed key
    folder.mkdir(parents=True)
    unlock = Unlock(folder, no_store, STRENGTH, portable=True, clock=clock)
    key = unlock.create_password(PASSWORD)
    write_recovery(unlock.files, key, RECOVERY_KEY, clock())
    secret_key = unlock.turn_on_secret_notes(SECRET_PASSWORD)

    connection = open_store(unlock.database, key, clock)
    assert schema_version(connection) == SCHEMA_VERSION
    unlock.keep_copies(connection)
    notes = NoteRepository(connection, clock)
    layouts = LayoutRepository(connection, clock)
    ColorRepository(connection, clock).add(OWN_COLOR)

    ids: list[str] = []
    for body, color, hidden in NOTES:
        note = notes.create(body, color)
        ids.append(note.id)
        if hidden:
            notes.set_hidden(note.id, True)
    for index, rows in PLACES.items():
        layouts.save(
            ids[index],
            {
                slot: Place(number, name, rel_x, rel_y, width, height, setup)
                for slot, number, name, rel_x, rel_y, width, height, setup in rows
            },
        )
    slot, monitor_key, rel_x, rel_y, width, height = UNREADABLE_ROW
    with connection:
        connection.execute(
            "INSERT INTO note_layouts (note_id, config_key, monitor_key, rel_x, rel_y, width,"
            " height, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ids[UNREADABLE], slot, monitor_key, rel_x, rel_y, width, height, clock()),
        )
    year, month, day, hour, minute, zone = REMINDER
    rule = Rule(date(year, month, day), time(hour, minute), zone)
    ReminderRepository(connection, clock).set(ids[REMINDED], rule)

    secret = notes.create(SECRET_TEXT)
    notes.make_secret(secret.id, SECRET_TITLE, seal_text(secret_key, secret.id, SECRET_TEXT))
    notes.delete(notes.create(TRASHED).id)

    settings = Settings(connection, clock)
    settings.set(NEWS_SEEN, NEWS_SEEN_SET)
    settings.set(RECOVERY_KEY_KEPT, True)
    settings.set(DEFAULT_NOTE_COLOR, DEFAULT_COLOR_SET)
    connection.pragma("wal_checkpoint", "TRUNCATE")
    connection.close()


if __name__ == "__main__":
    make(Path(sys.argv[1]))
