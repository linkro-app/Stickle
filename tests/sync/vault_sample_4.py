"""A note's places on screen and its size, and a setting that follows the user,
in a vault of format 1, as this version writes them, kept as files
(vault_sample_4/): every later version must read them as below and take them.
Made once with write_sample() and never again; the keys are this sample's alone.

    uv run python tests/sync/vault_sample_4.py <empty folder>
"""

import hashlib
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from stickle.core.layout import Place, Size
from stickle.crypto.vault_keys import DataKeys
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.record_stamps import SETTING
from stickle.data.schema import open_store
from stickle.data.settings import MAGNET, MORNING, Settings
from stickle.sync import local, local_records
from stickle.sync.format import NOTES, RECORDS, VAULT_FILE, VaultInfo, item_path, seal, vault_file
from stickle.sync.merge import as_sent
from stickle.sync.record_file import RecordFile, record_id

SAMPLE_4 = Path(__file__).parent / "vault_sample_4"
DATA_KEY = bytes.fromhex("cb85d7af2f17c35953c3fcda7ad28d744161e803aa91b40618e493633e0a3cf3")
DATA_KEYS = DataKeys({1: DATA_KEY})
DATABASE_KEY = bytes.fromhex("961305aca8bbcb6cb683f8a1005ba9272468bbec18b7a1f97e1e6a3cf9258998")
DEVICE = "f8bd595d-67c8-492f-8938-03fa354b1942"
VAULT = "471b4c62-72dc-4821-978e-d1d8a9d3a0fd"
NOTE = "85df851b-6cc7-4604-b0dc-4bcd11ca788c"
NOTE_TEXT = "the sample's note, placed"
MADE_AT = "2026-11-17T09:00:00.000Z"
MOVED_AT = 1763370060000  # when it was moved and resized, by the device's time
SENT = 1763370120000  # when they reached the cloud
LAPTOP = Place(1, "eDP-1", 0.125, 0.25, ("eDP-1",))
DESK = Place(2, "DELL U2720Q", 0.5, 0.375, ("eDP-1", "DELL U2720Q"))
SIZE = Size(320, 240)
SETTINGS = {MAGNET.key: False, MORNING.key: "07:30"}

# What the files are, so that one made again does not pass for the sample.
FILES_SHA256 = {
    "notes/85df851b-6cc7-4604-b0dc-4bcd11ca788c.json": (
        "3969bc174435352e2c37d825730718b61b4c066d8f966080be6d383ca525d665"
    ),
    "records/8a3fb79e-b434-42f6-82bc-0e032dadb5cb.json": (
        "506f504684bcc086ab75b010505a4b887a71a8fa8eac730d056f4daddda65fa8"
    ),
    "records/bd808bb4-6182-4d53-882f-67e29ee83498.json": (
        "25e668b8cbdbdf248b011690157c1ddc1fdb53006ec6860080e9721ec2c0b41e"
    ),
    "vault.json": "c8fd59b1292f84ea4992dbfa930d7662de1be17c233a338ed584aa8e68cefb72",
}


def write_sample(folder: Path) -> dict[str, bytes]:
    files = {VAULT_FILE: vault_file(VaultInfo(VAULT))}
    with tempfile.TemporaryDirectory() as made:
        db = open_store(Path(made) / "notes.db", DATABASE_KEY)
        try:
            notes = NoteRepository(db, clock=lambda: MADE_AT, millis=lambda: MOVED_AT)
            notes.create(NOTE_TEXT, note_id=NOTE)
            layouts = LayoutRepository(db, clock=lambda: MADE_AT, millis=lambda: MOVED_AT)
            layouts.save(NOTE, SIZE, (1, LAPTOP))
            layouts.save(NOTE, SIZE, (2, DESK))
            mine = local.mine(DATA_KEYS, DEVICE, local.here(db, notes, NOTE), None)
            assert mine is not None
            files[item_path(NOTES, NOTE)] = seal(
                DATA_KEYS, VAULT, NOTES, NOTE, as_sent(mine, SENT).to_json()
            )
            settings = Settings(db, clock=lambda: MADE_AT, millis=lambda: MOVED_AT)
            settings.set(MAGNET, False)
            settings.set(MORNING, "07:30")
            for key in SETTINGS:
                at = local_records.here(db, SETTING, key)
                record = local_records.mine(DEVICE, at, SETTING, key, None)
                assert record is not None
                fields = {
                    name: replace(each, sent=SENT) for name, each in record.contents.fields.items()
                }
                sent = RecordFile(SETTING, key, replace(record.contents, fields=fields))
                record_file = record_id(DATA_KEYS, SETTING, key)
                files[item_path(RECORDS, record_file)] = seal(
                    DATA_KEYS, VAULT, RECORDS, record_file, sent.to_json()
                )
        finally:
            db.close()
    for path, written in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(written)
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1])
    if target.exists() and any(target.iterdir()):
        sys.exit("the folder is not empty")
    for path, data in sorted(write_sample(target).items()):
        print(f'    "{path}": "{hashlib.sha256(data).hexdigest()}",')
