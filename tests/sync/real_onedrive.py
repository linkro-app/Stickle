"""The real OneDrive of whoever runs these tests, for those that need one.

STICKLE_TEST_ONEDRIVE names a file kept apart from the code, of the form
{"client_id": ..., "refresh_token": ...}, as a sign-in with a test account left
it. Without it they do not run, as in the commit checks and on CI. Microsoft
gives a new refresh token each time one is used: it takes the old one's place
in the file at once, so that the file keeps working. Each test works in a
folder of its own in the app's folder, removed after it, and nothing they print
says what is in that file.
"""

import json
import os
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from stickle.sync.targets.onedrive import OneDrive

VARIABLE = "STICKLE_TEST_ONEDRIVE"


def account_file() -> Path | None:
    named = os.environ.get(VARIABLE)
    return Path(named) if named else None


def account_given() -> bool:
    return account_file() is not None


def keep_account(file: Path, client_id: str, refresh_token: str) -> None:
    """The file written anew: whole, or not at all."""
    fresh = file.with_name(f"{file.name}.new")
    fresh.write_text(json.dumps({"client_id": client_id, "refresh_token": refresh_token}), "utf-8")
    fresh.replace(file)


def signed_in(folder: str = "") -> OneDrive:
    """A target for the test account: in folder, in the app's folder."""
    file = account_file()
    assert file is not None
    written = cast(dict[str, str], json.loads(file.read_text("utf-8-sig")))
    client_id = written["client_id"]

    def renewed(token: str) -> None:
        keep_account(file, client_id, token)

    return OneDrive(client_id, written["refresh_token"], renewed, folder=folder)


@contextmanager
def folder_of_its_own() -> Generator[OneDrive]:
    """A target whose Stickle folder is a new folder in the app's folder."""
    name = f"stickle-test-{secrets.token_hex(4)}"
    target = signed_in(name)
    try:
        yield target
    finally:
        target._graph("DELETE", target._item(""))  # pyright: ignore[reportPrivateUsage]
        target.close()
