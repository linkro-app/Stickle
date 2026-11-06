"""The vault's format: what the Stickle folder holds, and how a file is locked.

    Stickle/
      vault.json         the format number, the vault's id and its kind; not locked
      notes/<id>.json    one note
      records/<id>.json  one record: a category, a label, a colour, a setting...
      devices/<id>.json  one device, written by that device alone
      keys/<id>.json     the data key, sealed for one way of opening it; not
                         locked, as it is what opens the rest

Names are ids alone: lower-case letters, digits and "-". A file named any other
way is none of these; a copy a cloud made under another name is told by what
its envelope says (below).

A file in notes/, records/ or devices/ is an envelope: JSON whose head reads
without a key, and whose box is the item, locked with XChaCha20-Poly1305.

    {"alg":1,"format":1,"id":"<item id>","key":1,"kind":"notes",
     "vault":"<vault id>","box":"<base64: the nonce, then the locked JSON>"}

The whole head is the box's additional data. A head changed, or a file moved
into another vault's or another folder's place, does not open; copied under
another name in its own folder, it opens as the item its head names, never as
another. An envelope is written one way only (the head's names in order, the
box last, base64 as the standard writes it) and read only as written, so a
file with any byte changed does not open. Names a newer version adds to the
head are bound to the box like the rest, and let go when the item is written
again: a head says how a file was locked, not what it holds.

A format number higher than this version's, or a data key this device does
not have, stops sync until the app is updated: neither is taken for a missing
or a damaged file. The names "format" and "vault" keep their place in every
format, so a newer file is told from another vault's.

Integers are written only within what JavaScript reads exactly (2^53); larger
numbers go as text.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn, cast

from stickle.crypto.vault_keys import DataKeys, UnknownDataKeyError

FORMAT = 1
ALG = 1  # XChaCha20-Poly1305, IETF (libsodium's crypto_aead_xchacha20poly1305_ietf)
VAULT_FILE = "vault.json"
NOTES = "notes"
RECORDS = "records"
DEVICES = "devices"
KEYS = "keys"
LOCKED = (NOTES, RECORDS, DEVICES)
FOLDERS = (*LOCKED, KEYS)
PERSONAL = "personal"
VAULT_KINDS = (PERSONAL,)
LARGEST = 2**53 - 1  # the largest integer JavaScript reads exactly
AAD = b"stickle vault item\n"

_ID = re.compile(r"[a-z0-9-]{1,64}")
_HEAD_TEXT = re.compile(r"[ !#-\[\]-~]*")  # printable ASCII, but for " and \
_HEAD_NAMES = ("alg", "format", "id", "key", "kind", "vault")
_VAULT_NAMES = ("format", "kind", "vault")

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type HeadValue = int | str


class FormatError(Exception):
    """A file sync does not take as it is."""


class UnreadableError(FormatError):
    """Not a file this format opens: half written, damaged, or not one at all.
    Read it again later; it is set aside only if it stays so."""


class NewerFormatError(FormatError):
    """Written by a newer version: sync waits until the app is updated."""


class UnknownKeyError(FormatError):
    """Locked with a data key this device does not have (a newer version
    changed keys): sync waits until the app is updated."""


class NotOursError(FormatError):
    """Another vault's file, or one in another folder's place: neither read
    nor written over."""


def is_id(text: str) -> bool:
    return _ID.fullmatch(text) is not None


def item_path(folder: str, item_id: str) -> str:
    if folder not in FOLDERS or not is_id(item_id):
        raise ValueError("not an item's place")
    return f"{folder}/{item_id}.json"


def item_at(path: str) -> tuple[str, str] | None:
    """(folder, id) of a file named as an item is; None for any other name."""
    folder, slash, name = path.partition("/")
    if not slash or folder not in FOLDERS or not name.endswith(".json"):
        return None
    item_id = name.removesuffix(".json")
    return (folder, item_id) if is_id(item_id) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check(value: object) -> None:
    if value is None or isinstance(value, bool | str | float):
        return
    if isinstance(value, int):
        if abs(value) > LARGEST:
            raise ValueError("an integer beyond 2^53 goes as text")
        return
    if isinstance(value, list):
        for each in cast(list[object], value):
            _check(each)
        return
    if isinstance(value, dict):
        for name, each in cast(dict[object, object], value).items():
            if not isinstance(name, str):
                raise ValueError("JSON names are text")
            _check(each)
        return
    raise ValueError(f"not JSON: {type(value).__name__}")


def json_bytes(value: Json) -> bytes:
    """value as UTF-8 JSON, its names in order; ValueError for what JSON, or
    JavaScript, would not read back as it was."""
    _check(value)
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("text that is not Unicode") from error


def _no_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def parse_json(data: bytes) -> Json:
    try:
        return cast(Json, json.loads(data.decode("utf-8"), parse_constant=_no_constant))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise UnreadableError("not JSON") from error


@dataclass(frozen=True)
class VaultInfo:
    """What vault.json says."""

    vault: str
    kind: str = PERSONAL
    others: Mapping[str, Json] = field(default_factory=dict[str, Json])  # kept as they are


def vault_file(info: VaultInfo) -> bytes:
    if not is_id(info.vault) or info.kind not in VAULT_KINDS:
        raise ValueError("not a vault this version makes")
    return json_bytes({**info.others, "format": FORMAT, "kind": info.kind, "vault": info.vault})


def read_vault_file(data: bytes) -> VaultInfo:
    value = parse_json(data)
    if not isinstance(value, dict):
        raise UnreadableError("vault.json is not an object")
    number = value.get("format")
    if not _is_int(number) or cast(int, number) < 1:
        raise UnreadableError("vault.json has no format number")
    if cast(int, number) > FORMAT:
        raise NewerFormatError(f"format {number}")
    vault, kind = value.get("vault"), value.get("kind")
    if not isinstance(vault, str) or not is_id(vault) or not isinstance(kind, str):
        raise UnreadableError("vault.json names no vault")
    if kind not in VAULT_KINDS:
        raise NewerFormatError(f"a kind of vault this version does not know: {kind!r}")
    others = {name: each for name, each in value.items() if name not in _VAULT_NAMES}
    return VaultInfo(vault, kind, others)


@dataclass(frozen=True)
class Head:
    """What an envelope says of itself, readable without a key."""

    vault: str
    kind: str
    item: str
    key: int  # the data key's number
    others: Mapping[str, HeadValue] = field(default_factory=dict[str, HeadValue])


def _head_fields(vault: str, kind: str, item: str, key: int) -> dict[str, HeadValue]:
    return {"alg": ALG, "format": FORMAT, "id": item, "key": key, "kind": kind, "vault": vault}


def _head_bytes(fields: Mapping[str, HeadValue]) -> bytes:
    return json.dumps(dict(sorted(fields.items())), separators=(",", ":")).encode("ascii")


def _envelope(fields: Mapping[str, HeadValue], box: str) -> bytes:
    written = {**dict(sorted(fields.items())), "box": box}
    return json.dumps(written, separators=(",", ":")).encode("ascii")


def seal(keys: DataKeys, vault: str, kind: str, item: str, contents: Json) -> bytes:
    """contents as an envelope for item, locked with the current data key."""
    from nacl import secret

    if kind not in LOCKED or not is_id(item) or not is_id(vault):
        raise ValueError("not an item's place")
    fields = _head_fields(vault, kind, item, keys.current)
    locked = secret.Aead(keys.item_key(keys.current)).encrypt(
        json_bytes(contents), AAD + _head_bytes(fields)
    )
    return _envelope(fields, base64.b64encode(bytes(locked)).decode("ascii"))


def _strict_base64(text: str) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except binascii.Error as error:
        raise UnreadableError("the box is not base64") from error
    if base64.b64encode(data).decode("ascii") != text:
        raise UnreadableError("the box is not written as base64 is")
    return data


def _read(
    data: bytes, vault: str | None, kind: str | None
) -> tuple[Head, dict[str, HeadValue], str]:
    value = parse_json(data)
    if not isinstance(value, dict):
        raise UnreadableError("not an envelope")
    written = dict(value)
    if vault is not None and isinstance(written.get("vault"), str) and written["vault"] != vault:
        raise NotOursError("another vault's file")
    number = written.get("format")
    if not _is_int(number) or cast(int, number) < 1:
        raise UnreadableError("no format number")
    if cast(int, number) > FORMAT:
        raise NewerFormatError(f"format {number}")
    box = written.pop("box", None)
    if not isinstance(box, str):
        raise UnreadableError("no box")
    fields: dict[str, HeadValue] = {}
    for name, each in written.items():
        if _is_int(each) and 0 <= cast(int, each) <= LARGEST:
            fields[name] = cast(int, each)
        elif isinstance(each, str) and _HEAD_TEXT.fullmatch(each):
            fields[name] = each
        else:
            raise UnreadableError("a head value of another shape")
    if _envelope(fields, box) != data:
        raise UnreadableError("not written as an envelope is")
    head_vault, head_kind = fields.get("vault"), fields.get("kind")
    item, key, alg = fields.get("id"), fields.get("key"), fields.get("alg")
    if not isinstance(head_vault, str) or not is_id(head_vault):
        raise UnreadableError("no vault")
    if not isinstance(head_kind, str) or (kind is not None and head_kind != kind):
        raise NotOursError("another folder's file")
    if not isinstance(alg, int):
        raise UnreadableError("no algorithm")
    if alg != ALG:
        if alg > ALG:
            raise NewerFormatError(f"algorithm {alg}")
        raise UnreadableError(f"algorithm {alg}")
    if not isinstance(item, str) or not is_id(item) or not isinstance(key, int) or key < 1:
        raise UnreadableError("no item or key")
    others = {name: each for name, each in fields.items() if name not in _HEAD_NAMES}
    return Head(head_vault, head_kind, item, key, others), fields, box


def read_head(data: bytes) -> Head:
    """The head of an envelope, without opening its box."""
    return _read(data, None, None)[0]


def open_envelope(keys: DataKeys, data: bytes, vault: str, kind: str) -> tuple[Head, Json]:
    """The head and contents of an envelope in vault's folder kind."""
    from nacl import exceptions, secret

    head, fields, box = _read(data, vault, kind)
    try:
        item_key = keys.item_key(head.key)
    except UnknownDataKeyError as error:
        raise UnknownKeyError(f"data key {head.key}") from error
    try:
        contents = secret.Aead(item_key).decrypt(_strict_base64(box), AAD + _head_bytes(fields))
    except exceptions.CryptoError as error:
        raise UnreadableError("the box does not open") from error
    return head, parse_json(contents)
