"""A vault in format 1, as this version writes it, kept as files (vault_sample_1/):
every later version must open them and read what is below. They were made once,
with write_sample(), and are never made again: made by a later version, they
would only show that it reads what it writes. A later format piece gets a sample
of its own beside this one. The keys are this sample's alone.

    uv run python tests/sync/vault_sample.py <empty folder>
"""

import sys
from pathlib import Path

from stickle.crypto import master, recovery
from stickle.crypto.sealed import make_sealed_slot
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY, DataKeys, key_file
from stickle.sync.format import (
    DEVICES,
    KEYS,
    NOTES,
    RECORDS,
    VAULT_FILE,
    Json,
    VaultInfo,
    item_path,
    seal,
    vault_file,
)

SAMPLE_1 = Path(__file__).parent / "vault_sample_1"
DATA_KEY = bytes.fromhex("1186fd05c98daa29f8f6b7f894fde379b57618daef20d080f9e02bc67a37679c")
DATA_KEYS = DataKeys({1: DATA_KEY})
RECOVERY_KEY_TEXT = "J13Q-G4KJ-83R8-YAM0-NH4A-4YV6-N8E2-RKJG"
MASTER_PASSWORD_TEXT = "the sample's master password"
DATABASE_KEY = bytes.fromhex("3c26a2fff911c3650fb531513d391ce39c8ed6b6256a624b5aa387440a63036d")
SET_AT = "2026-11-07T09:00:00Z"
VAULT = "d1f07623-269c-4067-8952-f174de4f7f15"
NOTE = "a70a61ea-4ead-4c2a-8970-551edc292be5"
CATEGORY = "75ad22f2-de27-4f88-b431-0b4c837b0386"
DEVICE = "1e9011e8-64e4-4334-ac48-fdc179fb0926"
SETTING = DATA_KEYS.record_id("setting", "reminder_snooze_minutes")


def _field(value: Json, hlc: str = "1762506000000-0000") -> Json:
    return {"device": DEVICE, "hlc": hlc, "value": value}


ITEMS: dict[str, tuple[str, str, Json]] = {
    item_path(NOTES, NOTE): (
        NOTES,
        NOTE,
        {
            "fields": {
                "text": _field("A note in format 1\n한글 메모, 그대로 ✓", "1762506000000-0002"),
                "colour": _field("#FFF2A8"),
                "category": _field(CATEGORY, "1762506000000-0001"),
            }
        },
    ),
    item_path(RECORDS, CATEGORY): (
        RECORDS,
        CATEGORY,
        {"type": "category", "fields": {"name": _field("Work · 일")}},
    ),
    item_path(RECORDS, SETTING): (
        RECORDS,
        SETTING,
        {"type": "setting", "fields": {"value": _field(10)}},
    ),
    item_path(DEVICES, DEVICE): (
        DEVICES,
        DEVICE,
        {"fields": {"name": _field("Sample PC"), "round": _field(str(2**60))}},
    ),
}

# What the files are, so that one made again does not pass for the sample.
FILES_SHA256 = {
    "devices/1e9011e8-64e4-4334-ac48-fdc179fb0926.json": (
        "fcda2d2d6bfc3b86d1590b5feb32f5587601789bbb219382a88761023179176b"
    ),
    "keys/76e43236-5b00-4a06-8193-1f8f5555d0ed.json": (
        "ccc73a58093482185e1ca8de9fdbfc654f97902c435bda2669a15a3e6e3a3e9b"
    ),
    "keys/7bb8eb5d-126a-414a-8ac3-848ece721ad4.json": (
        "1d653f4330a09a595238c67a586686c3675496b041a085529e1a3dabd6d0ba64"
    ),
    "notes/a70a61ea-4ead-4c2a-8970-551edc292be5.json": (
        "b00fb6a7b70ce10d03eeb7dc75e6422f6f3d7f57101a1c6e38f78c57dfcb196b"
    ),
    "records/30efea08-0a6e-478d-9c32-2278cf5baaa4.json": (
        "a54144b1ac338e2854867971f621bf13486c9803c054f1fcfcd7416c809a7a8a"
    ),
    "records/75ad22f2-de27-4f88-b431-0b4c837b0386.json": (
        "da86f2a06c03ccb82f79aa12d41d2849b7b262f923307e01b59db6ba13cca302"
    ),
    "vault.json": "581eff12f7148d7c2ef9895706e92f97054a4b5de5508e3a20b63a847c0fe233",
}


def write_sample(folder: Path) -> dict[str, bytes]:
    recovery_pair = recovery.make_recovery_slot(DATABASE_KEY, RECOVERY_KEY_TEXT, SET_AT)
    master_pair = make_sealed_slot(DATABASE_KEY, MASTER_PASSWORD_TEXT, 1, 8192, master.SLOT, SET_AT)
    files = {VAULT_FILE: vault_file(VaultInfo(VAULT))}
    for kind, pair in ((RECOVERY_KEY, recovery_pair), (MASTER_PASSWORD, master_pair)):
        file_id, data = key_file(DATA_KEYS, kind, pair)
        files[item_path(KEYS, file_id)] = data
    for path, (kind, item, contents) in ITEMS.items():
        files[path] = seal(DATA_KEYS, VAULT, kind, item, contents)
    for path, data in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1])
    if target.exists() and any(target.iterdir()):
        sys.exit("the folder is not empty")
    import hashlib

    for path, data in sorted(write_sample(target).items()):
        print(f'    "{path}": "{hashlib.sha256(data).hexdigest()}",')
