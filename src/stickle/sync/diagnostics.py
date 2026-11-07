"""What a feedback report says of sync with a vault (stickle.app.feedback):
numbers and a time only, never a note's text or title, a path, a cloud's
address, or the vault's or a device's id. A vault is encrypted, so the user
cannot open the cloud to say what is there: these lines are how a problem on
their device is found.

    Sync: format 1, 812 notes, 23 records, 3 devices, last round 2026-11-08 09:12 UTC
    Sync now: 2 conflict copies, 1 round failing, 1 file set aside, 0 notes for a newer version
"""

from datetime import UTC, datetime

import apsw

from stickle.data import sync_left, sync_state
from stickle.sync.format import FORMAT, NOTES
from stickle.sync.left import set_aside
from stickle.sync.merge import COPY_OF
from stickle.sync.place import Place


def _count(connection: apsw.Connection, sql: str, vault: str) -> int:
    return int(connection.execute(sql, (vault,)).fetchall()[0][0])  # pyright: ignore[reportArgumentType]


def lines(connection: apsw.Connection, place: Place, failing: int = 0) -> list[str]:
    """failing: rounds running that failed for now (stickle.sync.outcome)."""
    vault = place.vault
    notes = _count(
        connection, "SELECT count(*) FROM sync_notes WHERE vault = ? AND gone = 0", vault
    )
    records = _count(connection, "SELECT count(*) FROM sync_records WHERE vault = ?", vault)
    devices = _count(connection, "SELECT count(*) FROM sync_devices WHERE vault = ?", vault)
    copies = _count(
        connection,
        "SELECT count(*) FROM sync_notes JOIN notes ON notes.id = sync_notes.note_id"
        " WHERE sync_notes.vault = ? AND notes.deleted_at IS NULL AND json_extract("
        f" coalesce(sync_notes.outbox, sync_notes.seen), '$.fields.{COPY_OF}.value') IS NOT NULL",
        vault,
    )
    synced = sync_state.synced_at(connection, vault)
    last = (
        "never"
        if synced is None
        else datetime.fromtimestamp(synced / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")
    )
    newer = sync_left.count(connection, vault, sync_left.READ_ONLY, NOTES)
    return [
        f"Sync: format {FORMAT}, {notes} notes, {records} records, {devices} devices,"
        f" last round {last}",
        f"Sync now: {copies} conflict copies, {failing} rounds failing,"
        f" {set_aside(place)} files set aside, {newer} notes for a newer version",
    ]
