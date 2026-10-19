"""Reading another app's SQLite database without touching it.

The app may be running and writing (its write-ahead log holding the newest
notes), and opening its file in place could write to it (the log is folded
in on opening). So the database and its companion files are copied into a
temporary folder first, and only the copy is opened.
"""

import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import apsw

from stickle.importers.base import SourceError

COMPANIONS = ("-wal", "-shm", "-journal")


@contextmanager
def opened_copy(database: Path) -> Generator[apsw.Connection]:
    if not database.is_file():
        raise SourceError("no database file")
    # A copy Windows still holds open is left for the system to clear, not an error.
    with tempfile.TemporaryDirectory(
        prefix="stickle-import-", ignore_cleanup_errors=True
    ) as folder:
        copy = Path(folder) / database.name
        try:
            shutil.copyfile(database, copy)
            for suffix in COMPANIONS:
                companion = database.with_name(database.name + suffix)
                if companion.is_file():
                    shutil.copyfile(companion, copy.with_name(copy.name + suffix))
        except OSError as error:
            raise SourceError("the database could not be copied") from error
        try:
            connection = apsw.Connection(str(copy))
        except apsw.Error as error:
            raise SourceError("not a database this app can read") from error
        try:
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchall()
        except apsw.Error as error:
            connection.close()
            raise SourceError("not a database this app can read") from error
        try:
            yield connection
        finally:
            connection.close()


def tables(connection: apsw.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }
