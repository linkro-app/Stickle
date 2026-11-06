"""The format 1 sample, made once and kept as files, still opens and reads as it
did: while it does, a vault written by this version syncs with later ones."""

import hashlib

import pytest
from vault_sample import (
    DATA_KEYS,
    FILES_SHA256,
    ITEMS,
    MASTER_PASSWORD_TEXT,
    RECOVERY_KEY_TEXT,
    SAMPLE_1,
    SETTING,
    VAULT,
)

from stickle.crypto.vault_keys import (
    MASTER_PASSWORD,
    RECOVERY_KEY,
    is_trusted,
    open_with_master_password,
    open_with_recovery_key,
    read_key_file,
)
from stickle.sync.contents import Contents
from stickle.sync.format import (
    KEYS,
    RECORDS,
    VAULT_FILE,
    VaultInfo,
    item_at,
    open_envelope,
    read_vault_file,
)


def sample_files() -> dict[str, bytes]:
    return {
        path.relative_to(SAMPLE_1).as_posix(): path.read_bytes()
        for path in SAMPLE_1.rglob("*")
        if path.is_file()
    }


def test_the_sample_is_the_one_made_then() -> None:
    files = sample_files()

    assert {path: hashlib.sha256(data).hexdigest() for path, data in files.items()} == FILES_SHA256


def test_its_vault_json_reads() -> None:
    assert read_vault_file(sample_files()[VAULT_FILE]) == VaultInfo(VAULT)


@pytest.mark.parametrize("path", sorted(ITEMS))
def test_its_items_open_and_read_as_written(path: str) -> None:
    kind, item, contents = ITEMS[path]

    head, opened = open_envelope(DATA_KEYS, sample_files()[path], VAULT, kind)

    assert (head.vault, head.kind, head.item, head.key, head.others) == (VAULT, kind, item, 1, {})
    assert opened == contents
    assert Contents.from_json(opened).to_json() == contents


def test_its_keys_open_with_the_recovery_key_and_the_master_password() -> None:
    receivers = {
        receiver.kind: receiver
        for path, data in sample_files().items()
        if (place := item_at(path)) and place[0] == KEYS
        for receiver in [read_key_file(place[1], data)]
    }

    assert set(receivers) == {RECOVERY_KEY, MASTER_PASSWORD}
    assert open_with_recovery_key(receivers[RECOVERY_KEY], RECOVERY_KEY_TEXT) == DATA_KEYS
    assert open_with_master_password(receivers[MASTER_PASSWORD], MASTER_PASSWORD_TEXT) == DATA_KEYS
    assert all(is_trusted(receiver, DATA_KEYS) for receiver in receivers.values())


def test_a_records_id_from_its_name_is_still_the_same() -> None:
    assert f"{RECORDS}/{SETTING}.json" in sample_files()
