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


def _file() -> Path | None:
    named = os.environ.get("STICKLE_TEST_ONEDRIVE")
    return Path(named) if named else None


def account_given() -> bool:
    return _file() is not None


def signed_in(folder: str = "") -> OneDrive:
    """A target for the test account: in folder, in the app's folder."""
    file = _file()
    assert file is not None
    written = cast(dict[str, str], json.loads(file.read_text("utf-8-sig")))

    def renewed(token: str) -> None:
        written["refresh_token"] = token
        fresh = file.with_name(f"{file.name}.new")
        fresh.write_text(json.dumps(written), "utf-8")
        fresh.replace(file)  # whole, or not at all

    return OneDrive(written["client_id"], written["refresh_token"], renewed, folder=folder)


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
