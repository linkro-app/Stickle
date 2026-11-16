"""A note in the vault: its fields (stickle.sync.contents) and the version its
text is at.

    {"fields": {...},
     "body": {"version": "3-<hash>", "parents": ["2-<hash>"],
              "history": ["2-<hash>", "1-<hash>"],
              "text": "...", "deleted": false,
              "edited": 1762483200000, "device": "<id>"}}

A version's id is its generation and a hash: the generation is one more than
its parents' highest (1 for a first text), the hash is of its parents, its text
and whether it deletes the note, keyed with the vault's lasting key for
versions (stickle.crypto.vault_keys — so a guess of a text cannot be checked
without the key, and keys changing leaves ids as they were). The same edit made
from the same version on two devices is one version, and a file damaged is
told by its id. What the hash is of, in UTF-8:

    stickle version 1\n
    parents <their ids in order, joined by ",">\n
    deleted <1 or 0>\n
    body plain\n
    \n
    <the text, as it is stored: line breaks are LF, nothing changed>

A secret note's text is kept as it was sealed (stickle.crypto.secret_notes),
never in the clear: "text" is null and "secret" holds it,

    "secret": {"sealed": "<base64>", "bound": "<the note it is bound to>",
               "key": "<the number of the key>", "reminders": ["<day> <time>", ...]}

bound to the note it was sealed for (a conflict copy's opens as its
original's), with the secret notes' key of that number (stickle.sync.
secret_key), and when the reminders written in it ring (a day and a time, in
order): a device it is locked on rings them all the same. Its id is made on the
sealed bytes as they are, so that a device that cannot open it makes the same
versions as one that can (the same text sealed twice is not the same version):

    stickle version 1\n
    parents <their ids in order, joined by ",">\n
    deleted 0\n
    body secret\n
    bound <the note id>\n
    key <the key's number>\n
    reminders <them, joined by ",">\n
    \n
    <the sealed bytes>

It is BLAKE2b, 16 bytes, written in lower-case hex. When and on which device a
version was edited are kept beside it, not in its id: two devices making the
same version make the same one.

A version that joins a note made secret on one line with its text edited in
the clear on the other takes the secret one's text, and names the other parent,
whose text it lets go of ("let_go", beside it, not in its id: see stickle.sync.
merge): the device that wrote that text is told.

The history is the ids of the versions this one holds, its parents' and theirs,
the highest generation first (the same, by id), 32 at most: a version made by
joining two lines holds both. It is what tells whether one version came from
another, which decides what a sync does with a note (decide).
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import cast

from stickle.crypto.vault_keys import DataKeys
from stickle.sync.contents import Contents, is_millis
from stickle.sync.format import Json, UnreadableError, is_id

VERSIONS = b"stickle-versions"  # the use of the lasting key that versions' ids are made with
HISTORY = 32
BODY = "body"
_VERSION_NAMES = (
    "deleted",
    "device",
    "edited",
    "history",
    "let_go",
    "parents",
    "secret",
    "sent",
    "text",
    "version",
)
_ID = re.compile(r"([1-9][0-9]{0,14})-([0-9a-f]{32})")
_SECRET_NAMES = ("bound", "key", "reminders", "sealed")
_REMINDER = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-2][0-9]:[0-5][0-9]")


def generation(version_id: str) -> int:
    found = _ID.fullmatch(version_id)
    if found is None:
        raise UnreadableError("not a version's id")
    return int(found.group(1))


@dataclass(frozen=True)
class Secret:
    """A secret note's text, as it was sealed (above)."""

    sealed: bytes
    bound: str  # the id of the note it opens as
    key: str  # the number of the secret notes' key it was sealed with
    reminders: tuple[str, ...] = ()  # "<day> <time>", in order

    def to_json(self) -> dict[str, Json]:
        return {
            "bound": self.bound,
            "key": self.key,
            "reminders": list(self.reminders),
            "sealed": base64.b64encode(self.sealed).decode("ascii"),
        }

    @classmethod
    def from_json(cls, value: Json) -> Secret:
        if not isinstance(value, dict) or tuple(sorted(value)) != _SECRET_NAMES:
            raise UnreadableError("a secret body is not one")
        sealed, bound, key = value["sealed"], value["bound"], value["key"]
        reminders = value["reminders"]
        if not (isinstance(bound, str) and is_id(bound) and isinstance(key, str) and is_id(key)):
            raise UnreadableError("a secret body without what it is sealed for")
        if not isinstance(reminders, list):
            raise UnreadableError("a secret body's reminders are not ones")
        times: list[str] = []
        for each in cast(list[object], reminders):
            if not (isinstance(each, str) and _REMINDER.fullmatch(each)):
                raise UnreadableError("a secret body's reminders are not ones")
            times.append(each)
        if times != sorted(set(times)):
            raise UnreadableError("a secret body's reminders are not in order")
        if not isinstance(sealed, str):
            raise UnreadableError("a secret body without its text")
        try:
            data = base64.b64decode(sealed, validate=True)
        except (binascii.Error, ValueError) as error:
            raise UnreadableError("a secret body's text is not base64") from error
        if not data:
            raise UnreadableError("a secret body without its text")
        return cls(data, bound, key, tuple(times))


def version_id(
    keys: DataKeys, parents: Sequence[str], text: str | Secret | None, deleted: bool
) -> str:
    """The id of a version with these parents, this text (None: deleted), as above."""
    ordered = sorted(parents)
    if isinstance(text, Secret):
        body = (
            b"body secret\nbound "
            + text.bound.encode()
            + b"\nkey "
            + text.key.encode()
            + b"\nreminders "
            + ",".join(text.reminders).encode()
            + b"\n\n"
            + text.sealed
        )
    else:
        body = b"body plain\n\n" + (text or "").encode("utf-8")
    made = (
        b"stickle version 1\n"
        + b"parents "
        + ",".join(ordered).encode()
        + b"\ndeleted "
        + (b"1" if deleted else b"0")
        + b"\n"
        + body
    )
    digest = hashlib.blake2b(made, digest_size=16, key=keys.lasting_key(VERSIONS)).hexdigest()
    return f"{max((generation(p) for p in ordered), default=0) + 1}-{digest}"


@dataclass(frozen=True)
class Version:
    """One version of a note's text (a secret note's as it was sealed); text
    None when it deletes the note."""

    id: str
    parents: tuple[str, ...]
    history: tuple[str, ...]
    text: str | Secret | None
    deleted: bool
    edited: int  # when, in milliseconds, by the device's time (stickle.core.cloud_time)
    device: str
    others: Mapping[str, Json] = field(default_factory=dict[str, Json])  # kept as they are
    # When it first reached the cloud, by the cloud's clock as its sender knew it
    # (stickle.sync.contents): a deletion's year is counted from then.
    sent: int | None = None
    # Of its parents, the one whose text it let go of for the other's, secret
    # (stickle.sync.merge); None: none.
    let_go: str | None = None

    @property
    def generation(self) -> int:
        return generation(self.id)

    def holds(self, other: str) -> bool:
        """Whether this version is other or came from it (as far as its history goes)."""
        return other == self.id or other in self.history

    def to_json(self) -> dict[str, Json]:
        written: dict[str, Json] = {
            **self.others,
            "deleted": self.deleted,
            "device": self.device,
            "edited": self.edited,
            "history": list(self.history),
            "parents": list(self.parents),
            "text": None if isinstance(self.text, Secret) else self.text,
            "version": self.id,
        }
        if isinstance(self.text, Secret):
            written["secret"] = self.text.to_json()
        if self.sent is not None:
            written["sent"] = self.sent
        if self.let_go is not None:
            written["let_go"] = self.let_go
        return written

    @classmethod
    def from_json(cls, keys: DataKeys, value: Json) -> Version:
        """The version as written; UnreadableError if it is not one, or its id is
        not what its parents and text make."""
        return cls._read(value, keys)

    @classmethod
    def kept(cls, value: Json) -> Version:
        """The version as this device keeps it (stickle.data.sync_state): its id
        not made again, and its text maybe taken out (None, not deleted all the
        same), leaving what versions are made on."""
        return cls._read(value, None)

    @classmethod
    def _read(cls, value: Json, keys: DataKeys | None) -> Version:
        if not isinstance(value, dict):
            raise UnreadableError("a body is not an object")
        made, parents, history = value.get("version"), value.get("parents"), value.get("history")
        text, deleted = value.get("text"), value.get("deleted")
        edited, device = value.get("edited"), value.get("device")
        if not (isinstance(made, str) and _ids(parents) and _ids(history)):
            raise UnreadableError("a body without its version")
        if text is not None and (not isinstance(text, str) or "\0" in text):
            raise UnreadableError("a body's text is not one")
        held: str | Secret | None = text
        if "secret" in value:
            if text is not None:
                raise UnreadableError("a body both in the clear and sealed")
            held = Secret.from_json(value["secret"])
        taken_out = keys is None and "text" not in value and "secret" not in value
        if not isinstance(deleted, bool) or ((held is None) != deleted and not taken_out):
            raise UnreadableError("a body neither text nor deleted")
        sent = value.get("sent")
        if not is_millis(edited) or not (sent is None or is_millis(sent)):
            raise UnreadableError("a body without when it was edited")
        if not isinstance(device, str) or not is_id(device):
            raise UnreadableError("a body without its device")
        parents, history = cast(list[str], parents), cast(list[str], history)
        if len(parents) > 2 or len(set(parents)) != len(parents) or made in history:
            raise UnreadableError("a body's versions do not hold together")
        if keys is not None and version_id(keys, parents, held, deleted) != made:
            raise UnreadableError("a body is not what its version says")
        let_go = value.get("let_go")
        if let_go is not None and (not isinstance(let_go, str) or let_go not in parents):
            raise UnreadableError("a body lets go of a version it was not made from")
        others = {name: each for name, each in value.items() if name not in _VERSION_NAMES}
        return cls(
            made,
            tuple(parents),
            tuple(history),
            held,
            deleted,
            cast(int, edited),
            device,
            others,
            cast(int | None, sent),
            let_go,
        )


def _ids(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for each in cast(list[object], value):
        if not isinstance(each, str):
            return False
        generation(each)
    return True


def _history(parents: Sequence[Version]) -> tuple[str, ...]:
    held = {held for parent in parents for held in (parent.id, *parent.history)}
    return tuple(sorted(held, key=lambda each: (-generation(each), each))[:HISTORY])


def new_version(
    keys: DataKeys,
    parents: Sequence[Version],
    text: str | Secret | None,
    edited: int,
    device: str,
) -> Version:
    """The version made from parents (none: a note's first text, two: joining two
    lines) with this text, or deleting the note (None)."""
    if len(parents) > 2:
        raise ValueError("a version has two parents at most")
    ids = tuple(sorted({parent.id for parent in parents}))
    deleted = text is None
    return Version(
        version_id(keys, ids, text, deleted), ids, _history(parents), text, deleted, edited, device
    )


@dataclass(frozen=True)
class NoteFile:
    contents: Contents
    body: Version

    def to_json(self) -> dict[str, Json]:
        return {**self.contents.to_json(), BODY: self.body.to_json()}

    @classmethod
    def from_json(cls, keys: DataKeys, value: Json) -> NoteFile:
        return cls._read(value, keys)

    @classmethod
    def kept(cls, value: Json) -> NoteFile:
        """As this device keeps it (Version.kept)."""
        return cls._read(value, None)

    @classmethod
    def _read(cls, value: Json, keys: DataKeys | None) -> NoteFile:
        if not isinstance(value, dict) or BODY not in value:
            raise UnreadableError("a note without its text")
        contents = Contents.from_json(value)
        others = {name: each for name, each in contents.others.items() if name != BODY}
        body = value[BODY]
        version = Version.kept(body) if keys is None else Version.from_json(keys, body)
        return cls(replace(contents, others=others), version)


class Step(Enum):
    """What a sync does with a note, from this device's version and the cloud's."""

    SAME = "same"  # nothing: both are at one version
    RECEIVE = "receive"  # the cloud's came from this one: take it
    SEND = "send"  # this one came from the cloud's (the vault went back, too): put it
    CONFLICT = "conflict"  # each has what the other has not, or neither tells: keep both


def decide(mine: Version, cloud: Version) -> Step:
    """mine: the version this device's text is at (the one last seen in the cloud
    if it was not changed here, else the one made from that for the change)."""
    if cloud.id == mine.id:
        return Step.SAME
    if cloud.holds(mine.id):
        return Step.RECEIVE
    if mine.holds(cloud.id):
        return Step.SEND
    return Step.CONFLICT
