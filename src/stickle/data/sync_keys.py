"""The recovery key's and the master password's pairs as this device and a
vault it syncs with last agreed on them (stickle.sync.keys): one changed since
on this device is put in the vault, one changed there is taken here, and one
changed on both is the later's.

What is kept is no key: a digest of the pair (stickle.crypto.vault_keys.
pair_digest), when it was set and by which device. This device's alone (in a
full export with the other sync tables: digests, no key); forgotten when the
device joins the vault anew, so that it takes the vault's then.
"""

from dataclasses import dataclass

import apsw


@dataclass(frozen=True)
class Agreed:
    digest: str | None  # None: neither had one of that kind
    set_at: str  # "" if not known
    device: str | None


def agreed(connection: apsw.Connection, vault: str) -> dict[str, Agreed]:
    """By kind."""
    rows = connection.execute(
        "SELECT kind, digest, set_at, device FROM sync_keys WHERE vault = ?", (vault,)
    )
    return {
        str(kind): Agreed(
            None if digest is None else str(digest),
            str(set_at),
            None if device is None else str(device),
        )
        for kind, digest, set_at, device in rows
    }


def agree(connection: apsw.Connection, vault: str, kind: str, agreed: Agreed) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO sync_keys (vault, kind, digest, set_at, device)"
        " VALUES (?, ?, ?, ?, ?)",
        (vault, kind, agreed.digest, agreed.set_at, agreed.device),
    )


def forget(connection: apsw.Connection, vault: str) -> None:
    """Joining the vault anew: what it has is taken, not what this device had."""
    connection.execute("DELETE FROM sync_keys WHERE vault = ?", (vault,))
