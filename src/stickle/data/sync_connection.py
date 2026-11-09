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


@dataclass(frozen=True)
class Login:
    vault: str
    kind: str
    address: str = field(repr=False)
    user: str = field(repr=False)
    secret: str = field(repr=False)  # the password
    keys: Mapping[int, bytes] = field(repr=False)  # the vault's data keys, by number
    certificate: bytes | None = field(default=None, repr=False)  # the one trusted (DER)


def _keys_text(keys: Mapping[int, bytes]) -> str:
    return json.dumps({str(n): base64.b64encode(key).decode("ascii") for n, key in keys.items()})


def _keys_of(text: str) -> dict[int, bytes]:
    written: dict[str, str] = json.loads(text)
    return {int(number): base64.b64decode(key) for number, key in written.items()}


def current(connection: apsw.Connection) -> Login | None:
    """This device's connection; None if it does not sync."""
    rows = connection.execute(
        "SELECT vault, kind, address, user, secret, keys, certificate FROM sync_connection LIMIT 1"
    ).fetchall()
    if not rows:
        return None
    vault, kind, address, user, secret, keys, certificate = rows[0]
    return Login(
        str(vault),
        str(kind),
        str(address),
        str(user),
        str(secret),
        _keys_of(str(keys)),
        None if certificate is None else bytes(certificate),  # pyright: ignore[reportArgumentType]
    )


def keep(connection: apsw.Connection, login: Login) -> None:
    """This one, in place of any other."""
    with connection:
        connection.execute("DELETE FROM sync_connection")
        connection.execute(
            "INSERT INTO sync_connection (vault, kind, address, user, secret, keys, certificate)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                login.vault,
                login.kind,
                login.address,
                login.user,
                login.secret,
                _keys_text(login.keys),
                login.certificate,
            ),
        )


def no_longer_trusted(connection: apsw.Connection) -> None:
    """The server now shows a public authority's certificate: none to remember."""
    connection.execute("UPDATE sync_connection SET certificate = NULL")


def forget(connection: apsw.Connection) -> None:
    connection.execute("DELETE FROM sync_connection")
