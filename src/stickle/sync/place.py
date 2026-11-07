"""Where a round of sync syncs, and as which device; and the transaction a
round writes what it takes in with."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import apsw

from stickle.crypto.vault_keys import DataKeys
from stickle.sync.targets.base import Target


@dataclass(frozen=True)
class Place:
    target: Target
    vault: str
    keys: DataKeys
    device: str
    aside: Path | None = None  # where files set aside are kept (stickle.sync.left); None: none


@contextmanager
def writing(connection: apsw.Connection) -> Generator[None]:
    """One transaction that writes, begun as one: no other may write in between."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
