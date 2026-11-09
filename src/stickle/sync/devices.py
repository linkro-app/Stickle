"""Each device's own file in the vault (devices/<device id>.json): what it is
called, its system, its version of the app, what it is (an app), and when it
last synced, by the cloud's clock (stickle.sync.contents).

    {"fields": {"name": ..., "os": ..., "version": ..., "kind": "app",
                "synced": <milliseconds>, "round": 7, "forgot": 2,
                "recent": [[6, "notes/<id>.json"], [7, "records/<id>.json"]]}}

A device writes its own file only: when the vault has none of it yet, when
what it says changed, when it last said it synced a day ago or more, and when
it has put something it has not said yet, so that rounds with nothing new wake
no other device. The others' files are read when their tags change, and kept
(stickle.data.record_state), for what each device is called and what it put.
One that does not open is passed over.

So a device needs no folder's listing to know what changed: "recent" names the
files another put, the newest last (each once, with the round it was last put
in), and those of the rounds not taken in here are read. A file is noted as
untold before it is put: an answer lost, or the app ended, it is said the next
round. "recent" holds so many; "forgot" is the last round something fell off
it. A device further behind than that, or one finding a device's rounds gone
back, looks at the folders instead. Writes in turn differ in size ("pad"): a
server may make its tags from the second and the size, and a reader would miss
a second write within the second.

Each write of its own file is marked anew ("mark"), the mark kept here before
the file is put (a year's of them). Its file there with a tag other than the
one it was left with (with no tags: on a full look, at start and once a day)
is read: marked as this device marked it, lately or a while ago, it is this
device's write, its answer lost on the way or the vault gone back to an
earlier day. Marked otherwise, another device wrote it under this device's
id: a copy of its disk, or of its virtual machine. The device that sees it
takes a new id, and what was changed here until then keeps the old one.
"""

import platform
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import apsw

from stickle import __version__
from stickle.core.hlc import Hlc
from stickle.data import record_state, sync_state
from stickle.data.settings import DEVICE_ID, Settings
from stickle.sync.contents import Contents, Field
from stickle.sync.format import (
    DEVICES,
    FormatError,
    Json,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    parse_json,
    seal,
)
from stickle.sync.place import Place, writing
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    Entry,
    FullError,
    NotThereError,
    RefusedError,
)

DAY = 24 * 60 * 60 * 1000
APP = "app"
MARK = "mark"
ROUND = "round"
RECENT = "recent"
FORGOT = "forgot"
PAD = "pad"
KEPT = 100  # files a device's own file names
TRIES = 5
_SAID = ("name", "os", "version", "kind")
_SYSTEMS = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}


@dataclass(frozen=True)
class About:
    """What a device says of itself."""

    name: str
    os: str
    version: str = __version__


@dataclass(frozen=True)
class Looked:
    """What a look at the devices' files found."""

    device: str  # the id this device goes on as
    told: frozenset[str]  # files the others put, not taken in here yet
    enough: bool  # False: they put more than they still name: the folders have to be listed
    rounds: Mapping[str, int]  # each one's last round, as taken in once a round is through


@dataclass(frozen=True)
class _Said:
    """What a device's file says it put."""

    round: int = 0
    forgot: int = 0
    recent: tuple[tuple[int, str], ...] = ()


def this_device(name: str | None = None) -> About:
    """This device, called name, or what its system calls it."""
    system = platform.system()
    return About(name or platform.node() or system, _SYSTEMS.get(system, system))


def _contents(text: str | None) -> Contents | None:
    if text is None:
        return None
    try:
        return Contents.from_json(parse_json(text.encode("utf-8")))
    except FormatError:
        return None


def _as_kept(contents: Contents) -> str:
    return json_bytes(contents.to_json()).decode("utf-8")


def _says(contents: Contents | None, about: About) -> bool:
    """Whether the file says what the device says of itself."""
    if contents is None:
        return False
    said = {name: each.value for name, each in contents.fields.items() if name in _SAID}
    return said == {"name": about.name, "os": about.os, "version": about.version, "kind": APP}


def _said(contents: Contents | None) -> _Said:
    if contents is None:
        return _Said()

    def number(name: str) -> int:
        each = contents.fields.get(name)
        value = None if each is None else each.value
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    named = contents.fields.get(RECENT)
    recent: list[tuple[int, str]] = []
    if named is not None and isinstance(named.value, list):
        for each in named.value:
            if isinstance(each, list) and len(each) == 2:
                at, path = each
                if isinstance(at, int) and not isinstance(at, bool) and isinstance(path, str):
                    recent.append((at, path))
    return _Said(number(ROUND), number(FORGOT), tuple(recent))


def _telling(before: Contents | None, untold: list[str]) -> dict[str, Json]:
    """What the device's own file says it put, untold named in a new round."""
    said = _said(before)
    number = said.round + 1 if untold else said.round
    fresh = set(untold)
    recent = [(at, path) for at, path in said.recent if path not in fresh]
    recent += [(number, path) for path in sorted(fresh)]
    forgot = said.forgot
    if len(recent) > KEPT:
        forgot = max(forgot, *(at for at, _path in recent[:-KEPT]))
        recent = recent[-KEPT:]
    pad = None if before is None else before.fields.get(PAD)
    named: list[Json] = [[at, path] for at, path in recent]
    return {
        ROUND: number,
        FORGOT: forgot,
        RECENT: named,
        PAD: "" if pad is not None and pad.value else "xxx",
    }


def sync_devices(
    connection: apsw.Connection,
    place: Place,
    cloud_now: Callable[[], int],
    about: About,
    full: bool = True,
) -> Looked:
    """This device's own file put, if it is to be; the others' read, and what
    they say they put. full: a full look, when a target with no tags has its own
    file read; the id it goes on as is a new one if another wrote its file
    under its id."""
    vault, target, keys = place.vault, place.target, place.keys
    own: Entry | None = None
    for entry in target.list(DEVICES):
        found = item_at(entry.path)
        if found is None or found[0] != DEVICES:
            continue
        if found[1] == place.device:
            own = entry
            continue
        seen, tag = record_state.device(connection, vault, found[1])
        if seen is not None and entry.tag is not None and entry.tag == tag:
            continue
        try:
            read = target.get(entry.path)
            head, value = open_envelope(keys, read.data, vault, DEVICES)
            if head.item != found[1]:
                continue
            contents = Contents.from_json(value)
        except NotThereError, FormatError:
            continue
        with writing(connection):
            record_state.put_device(connection, vault, found[1], _as_kept(contents), read.tag)
    device = place.device
    if own is not None and _copied(connection, place, own, full):
        device = renew(connection, device)
        place = Place(target, vault, keys, device)
    _put_own(connection, place, cloud_now, about)
    return _looked(connection, vault, device)


def _looked(connection: apsw.Connection, vault: str, device: str) -> Looked:
    taken = record_state.taken(connection, vault)
    told: set[str] = set()
    rounds: dict[str, int] = {}
    enough = True
    for other, (seen, _tag) in record_state.devices(connection, vault).items():
        if other == device:
            continue
        said = _said(_contents(seen))
        already = taken.get(other, 0)
        rounds[other] = said.round
        if said.round < already or said.forgot > already:
            enough = False
        told.update(path for at, path in said.recent if at > already)
    return Looked(device, frozenset(told), enough, rounds)


def tell(
    connection: apsw.Connection, place: Place, cloud_now: Callable[[], int], about: About
) -> None:
    """What this device put in the round just through, said in its own file."""
    if sync_state.untold(connection, place.vault):
        _put_own(connection, place, cloud_now, about)


def _copied(connection: apsw.Connection, place: Place, own: Entry, full: bool) -> bool:
    """Whether this device's own file there was written by another device."""
    vault, device = place.vault, place.device
    _, tag = record_state.device(connection, vault, device)
    if (own.tag is not None and own.tag == tag) or (own.tag is None and not full):
        return False
    try:
        read = place.target.get(own.path)
        head, value = open_envelope(place.keys, read.data, vault, DEVICES)
        contents = Contents.from_json(value)
    except NotThereError, FormatError:
        return False  # written over below, as this device's
    if head.item != device:
        return False
    mark = contents.fields.get(MARK)
    ours = sync_state.own_marks(connection, vault)
    if ours and (mark is None or mark.value not in ours):
        return True
    with writing(connection):
        record_state.put_device(connection, vault, device, _as_kept(contents), read.tag)
    return False


def renew(connection: apsw.Connection, old: str) -> str:
    """A new id for this device, another having its old one: what was changed
    here until now keeps the old one."""
    new = str(uuid.uuid4())
    with writing(connection):
        connection.execute("UPDATE note_stamps SET device = ? WHERE device IS NULL", (old,))
        connection.execute("UPDATE record_stamps SET device = ? WHERE device IS NULL", (old,))
        Settings(connection).set(DEVICE_ID, new)
    return new


def _put_own(
    connection: apsw.Connection, place: Place, cloud_now: Callable[[], int], about: About
) -> None:
    vault, target, keys, device = place.vault, place.target, place.keys, place.device
    path = item_path(DEVICES, device)
    for _ in range(TRIES):
        seen, tag = record_state.device(connection, vault, device)
        before = _contents(seen)
        now = cloud_now()
        synced = None if before is None else before.fields.get("synced")
        recent = synced is not None and isinstance(synced.value, int) and now - synced.value < DAY
        untold = sync_state.untold(connection, vault)
        if _says(before, about) and recent and not untold:
            return
        fields = {} if before is None else dict(before.fields)
        said = {"name": about.name, "os": about.os, "version": about.version, "kind": APP}
        mark = secrets.token_hex(16)
        spoken: dict[str, Json] = {**said, "synced": now, MARK: mark}
        for name, value in {**spoken, **_telling(before, untold)}.items():
            fields[name] = Field(value, Hlc(now), device)
        contents = Contents(fields, {} if before is None else before.others)
        condition = Absent() if before is None else AsRead(tag)
        with writing(connection):
            sync_state.add_own_mark(connection, vault, mark)
        try:
            written = target.put(
                path, seal(keys, vault, DEVICES, device, contents.to_json()), condition
            )
        except FullError:
            return  # written again once there is room
        except RefusedError:
            try:
                read = target.get(path)
                _, value = open_envelope(keys, read.data, vault, DEVICES)
                kept, tag = _as_kept(Contents.from_json(value)), read.tag
            except NotThereError, FormatError:
                return  # a later round
            with writing(connection):
                record_state.put_device(connection, vault, device, kept, tag)
            continue
        with writing(connection):
            record_state.put_device(connection, vault, device, _as_kept(contents), written)
            sync_state.told(connection, vault, untold)
        return
