"""A look into a target's Stickle folder for a test that failed: every file, its
tag and size, through the requests any target answers (stickle.sync.targets.base)
— a fake cloud's, or a real one's in the tests run on this computer. A fake
cloud also tells its requests (FakeCloud.describe).

Given the vault's data keys, or a recovery key to open them with from keys/,
it also shows what each file holds: an envelope's head and contents, and for
keys/ which way of opening each file is and whether its check holds."""

import json

from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.vault_keys import (
    RECOVERY_KEY,
    AlteredReceiverError,
    DataKeys,
    Receiver,
    is_trusted,
    open_with_recovery_key,
    read_key_file,
)
from stickle.sync.format import (
    KEYS,
    LOCKED,
    VAULT_FILE,
    FormatError,
    item_at,
    open_envelope,
    read_vault_file,
)
from stickle.sync.targets.base import FOLDERS, NotThereError, Target


def describe_target(
    target: Target, keys: DataKeys | None = None, recovery_key: str | None = None
) -> str:
    lines: list[str] = []
    files: dict[str, bytes] = {}
    try:
        vault = target.get(VAULT_FILE)
        lines.append(f"{VAULT_FILE}  {vault.tag}  {len(vault.data)} bytes")
        files[VAULT_FILE] = vault.data
    except NotThereError:
        lines.append(f"{VAULT_FILE}  (none)")
    listed: dict[str, list[str]] = {}
    for folder in FOLDERS:
        entries = list(target.list(folder))
        lines.append(f"{folder}/  {len(entries)} files")
        lines += [f"  {entry.path}  {entry.tag}  {entry.size} bytes" for entry in entries]
        listed[folder] = [entry.path for entry in entries]
    if keys is None and recovery_key is None:
        return "\n".join(lines)
    lines.append("")
    lines += _opened(target, listed, files, keys, recovery_key)
    return "\n".join(lines)


def _opened(
    target: Target,
    listed: dict[str, list[str]],
    files: dict[str, bytes],
    keys: DataKeys | None,
    recovery_key: str | None,
) -> list[str]:
    lines: list[str] = []
    for path in listed[KEYS]:
        files[path] = target.get(path).data
    receivers: list[Receiver] = []
    for path in listed[KEYS]:
        place = item_at(path)
        try:
            receivers.append(read_key_file(place[1] if place else path, files[path]))
        except AlteredReceiverError as error:
            lines.append(f"{path}: does not hold together ({error})")
    if keys is None and recovery_key is not None:
        for receiver in receivers:
            if receiver.kind == RECOVERY_KEY:
                try:
                    keys = open_with_recovery_key(receiver, recovery_key)
                except (RecoveryKeyTypoError, WrongRecoveryKeyError, AlteredReceiverError) as error:
                    lines.append(f"keys/{receiver.id}.json: {type(error).__name__}")
    if keys is None:
        return [*lines, "(no data key to open the files with)"]
    for receiver in receivers:
        trusted = "trusted" if is_trusted(receiver, keys) else "NOT trusted"
        lines.append(
            f"keys/{receiver.id}.json: {receiver.kind}, data key {receiver.key}, {trusted}"
        )
    vault = read_vault_file(files[VAULT_FILE]).vault if VAULT_FILE in files else ""
    for folder in LOCKED:
        for path in listed[folder]:
            try:
                head, contents = open_envelope(keys, target.get(path).data, vault, folder)
            except FormatError as error:
                lines.append(f"{path}: {type(error).__name__} ({error})")
                continue
            lines.append(f"{path}: {head.kind} {head.item}, data key {head.key}")
            shown = json.dumps(contents, ensure_ascii=False, sort_keys=True, indent=2)
            lines += [f"  {line}" for line in shown.splitlines()]
    return lines
