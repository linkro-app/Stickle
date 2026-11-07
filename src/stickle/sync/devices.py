"""Each device's own file in the vault (devices/<device id>.json): what it is
called, its system, its version of the app, what it is (an app), and when it
last synced, by the cloud's clock (stickle.sync.contents).

    {"fields": {"name": ..., "os": ..., "version": ..., "kind": "app",
                "synced": <milliseconds>}}

A device writes its own file only: when the vault has none of it yet, when
what it says changed, and when it last said it synced a day ago or more, so
that rounds with nothing new wake no other device. The others' files are
read when their tags change, and kept (stickle.data.record_state), for what
each device is called. One that does not open is passed over.
"""

import platform
from collections.abc import Callable
from dataclasses import dataclass

import apsw

from stickle import __version__
from stickle.core.hlc import Hlc
from stickle.data import record_state
from stickle.sync.contents import Contents, Field
from stickle.sync.format import (
    DEVICES,
    FormatError,
    item_at,
    item_path,
    json_bytes,
    open_envelope,
    parse_json,
    seal,
)
from stickle.sync.place import Place, writing
from stickle.sync.targets.base import Absent, AsRead, NotThereError, RefusedError

DAY = 24 * 60 * 60 * 1000
APP = "app"
TRIES = 5
_SAID = ("name", "os", "version", "kind")
_SYSTEMS = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}


@dataclass(frozen=True)
class About:
    """What a device says of itself."""

    name: str
    os: str
    version: str = __version__


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


def sync_devices(
    connection: apsw.Connection, place: Place, cloud_now: Callable[[], int], about: About
) -> None:
    """This device's own file put, if it is to be; the others' read."""
    vault, target, keys = place.vault, place.target, place.keys
    for entry in target.list(DEVICES):
        found = item_at(entry.path)
        if found is None or found[0] != DEVICES or found[1] == place.device:
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
    _put_own(connection, place, cloud_now, about)


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
        if _says(before, about) and recent:
            return
        fields = {} if before is None else dict(before.fields)
        said = {"name": about.name, "os": about.os, "version": about.version, "kind": APP}
        for name, value in {**said, "synced": now}.items():
            fields[name] = Field(value, Hlc(now), device)
        contents = Contents(fields, {} if before is None else before.others)
        condition = Absent() if before is None else AsRead(tag)
        try:
            written = target.put(
                path, seal(keys, vault, DEVICES, device, contents.to_json()), condition
            )
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
        return
