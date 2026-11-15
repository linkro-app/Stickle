"""The secret notes' key as a record of a vault in format 1 (stickle.sync.
secret_key), as this version writes it, kept as files (vault_sample_3/): every
later version must open them and read what is below, its password and the
recovery key opening the same key. Made once with write_sample() and never again
(vault_sample_1 is the container's sample). The keys are this sample's alone.

    uv run python tests/sync/vault_sample_3.py <empty folder>
"""

import base64
import hashlib
import sys
from pathlib import Path

from stickle.core.hlc import Hlc
from stickle.crypto import recovery
from stickle.crypto.keyfile import encode_slot, wrap_with_password
from stickle.crypto.sealed import seal as seal_key
from stickle.crypto.secret_notes import HOW, PASSWORD_SLOT, RESET
from stickle.crypto.vault_keys import RECOVERY_KEY, DataKeys, key_file
from stickle.sync.contents import Contents
from stickle.sync.format import KEYS, RECORDS, VAULT_FILE, VaultInfo, item_path, seal, vault_file
from stickle.sync.record_file import RecordFile
from stickle.sync.secret_key import CURRENT, KIND, PASSWORD, RECOVERY, record_of

SAMPLE_3 = Path(__file__).parent / "vault_sample_3"
DATA_KEY = bytes.fromhex("7c1e5a90b3d24f6e8a17c0d9e2f4b6a83159d7e0c2a4f6b8d0e1c3a5b7d9f102")
DATA_KEYS = DataKeys({1: DATA_KEY})
RECOVERY_KEY_TEXT = "YRKS-X39P-5WY2-7NWP-WPT2-1W49-SPDQ-BNFX"
DATABASE_KEY = bytes.fromhex("19f3c5a7e9b1d3f5072a4c6e8b0d2f4163857a9cbedf0123456789abcdef0123")
SECRET_KEY = bytes.fromhex("e4c2a08f6d4b2907e5c3a18f6d4b2907e5c3a18f6d4b2907e5c3a18f6d4b2907")
PASSWORD_TEXT = "the sample's secret notes password"
NUMBER = "5f0c9d2e-8a41-4b7e-93c6-2d1e0f7a8b94"
DEVICE = "2b7e4c19-0d5a-4f83-a6e2-9c1b3d5f7a08"
SET_AT = "2026-11-15T09:00:00.000Z"
VAULT = "c3e8a1f4-6b2d-4e90-8c75-1a9d3f6b0e27"
RECORD = record_of(DATA_KEYS)

# What the files are, so that one made again does not pass for the sample.
FILES_SHA256 = {
    "keys/0b8abbac-87ae-4c60-96ab-89657cddd335.json": (
        "18305713e15b32315c09ed85b87c11bf570381a401b6733cd8a561d869a64704"
    ),
    "records/551ee930-694e-49e2-b570-e51aca3ba7e6.json": (
        "713f0ce407d55c9766920618518a3d51fb37e82433d5426b4b32ae5c7bb65cf7"
    ),
    "vault.json": "a67c2a7b1ef26a362a13a74c407af42d54f77bf7cf0b308e6c0d7f53200044f4",
}


def write_sample(folder: Path) -> dict[str, bytes]:
    pair = recovery.make_recovery_slot(DATABASE_KEY, RECOVERY_KEY_TEXT, SET_AT)
    files = {VAULT_FILE: vault_file(VaultInfo(VAULT))}
    file_id, data = key_file(DATA_KEYS, RECOVERY_KEY, pair, DEVICE)
    files[item_path(KEYS, file_id)] = data
    password = wrap_with_password(SECRET_KEY, PASSWORD_TEXT, 1, 8192, PASSWORD_SLOT, SET_AT)
    when = Hlc(1763197200000)
    contents = (
        Contents()
        .changed(CURRENT, NUMBER, when, DEVICE)
        .changed(PASSWORD + NUMBER, {**encode_slot(password), HOW: RESET}, when, DEVICE)
        .changed(
            RECOVERY + NUMBER,
            {
                "public": _b64(pair.public),
                "sealed": _b64(seal_key(pair.public, SECRET_KEY)),
            },
            when,
            DEVICE,
        )
    )
    files[item_path(RECORDS, RECORD)] = seal(
        DATA_KEYS, VAULT, RECORDS, RECORD, RecordFile(KIND, "", contents).to_json()
    )
    for path, written in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(written)
    return files


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


if __name__ == "__main__":
    target = Path(sys.argv[1])
    if target.exists() and any(target.iterdir()):
        sys.exit("the folder is not empty")
    for path, data in sorted(write_sample(target).items()):
        print(f'    "{path}": "{hashlib.sha256(data).hexdigest()}",')
