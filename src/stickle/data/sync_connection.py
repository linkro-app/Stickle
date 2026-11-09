"""How this device reaches the cloud it syncs with: where, who it signs in as,
the password or tokens, the certificate the user chose to trust, and the data
keys of the vault there.

These stay here, in the encrypted database. They are not synced, and go in no
export, feedback report or log (repr leaves them out too). A device has one
connection at a time.
"""

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import apsw

WEBDAV = "webdav"
_OFFSET = "cloud_offset"


@dataclass(frozen=True)
class Login:
    vault: str
    kind: str
    address: str = field(repr=False)
    user: str = field(repr=False)
    secret: str = field(repr=False)  # the password
    keys: Mapping[int, bytes] = field(repr=False)  # the vault's data keys, by number
    certificate: bytes | None = field(default=None, repr=False)  # the one trusted (DER)
    stopped: str | None = None  # why sync stopped until the user does something


def _keys_text(keys: Mapping[int, bytes]) -> str:
    return json.dumps({str(n): base64.b64encode(key).decode("ascii") for n, key in keys.items()})


def _keys_of(text: str) -> dict[int, bytes]:
    written: dict[str, str] = json.loads(text)
    return {int(number): base64.b64decode(key) for number, key in written.items()}


def current(connection: apsw.Connection) -> Login | None:
    """This device's connection; None if it does not sync."""
    rows = connection.execute(
        "SELECT vault, kind, address, user, secret, keys, certificate, stopped"
        " FROM sync_connection LIMIT 1"
    ).fetchall()
    if not rows:
        return None
    vault, kind, address, user, secret, keys, certificate, stopped = rows[0]
    return Login(
        str(vault),
        str(kind),
        str(address),
        str(user),
        str(secret),
        _keys_of(str(keys)),
        None if certificate is None else bytes(certificate),  # pyright: ignore[reportArgumentType]
        None if stopped is None else str(stopped),
    )


def keep(connection: apsw.Connection, login: Login) -> None:
    """This one, in place of any other."""
    with connection:
        connection.execute("DELETE FROM sync_connection")
        connection.execute(
            "INSERT INTO sync_connection"
            " (vault, kind, address, user, secret, keys, certificate, stopped)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                login.vault,
                login.kind,
                login.address,
                login.user,
                login.secret,
                _keys_text(login.keys),
                login.certificate,
                login.stopped,
            ),
        )


def no_longer_trusted(connection: apsw.Connection) -> None:
    """The server now shows a public authority's certificate: none to remember."""
    connection.execute("UPDATE sync_connection SET certificate = NULL")


def set_stopped(connection: apsw.Connection, why: str | None) -> None:
    """Why sync stopped until the user does something; None: it did not."""
    connection.execute("UPDATE sync_connection SET stopped = ?", (why,))


def cloud_offset(connection: apsw.Connection) -> int:
    """The cloud's clock less this computer's, in milliseconds, as last known."""
    rows = connection.execute("SELECT value FROM local_counters WHERE key = ?", (_OFFSET,))
    found = rows.fetchall()
    return int(found[0][0]) if found else 0  # pyright: ignore[reportArgumentType]


def keep_cloud_offset(connection: apsw.Connection, offset: int) -> None:
    connection.execute(
        "INSERT INTO local_counters (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_OFFSET, offset),
    )


def forget(connection: apsw.Connection) -> None:
    with connection:
        connection.execute("DELETE FROM sync_connection")
        connection.execute("DELETE FROM local_counters WHERE key = ?", (_OFFSET,))
