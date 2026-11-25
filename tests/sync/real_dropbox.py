"""The real Dropbox of whoever runs these tests, for those that need one.

STICKLE_TEST_DROPBOX names a file kept apart from the code, of the form
{"app_key": ..., "refresh_token": ...}, as a sign-in with a test account left
it (sign_in_dropbox.py). Without it they do not run, as in the commit checks
and on CI. Dropbox's refresh token lasts until revoked; should it ever give a
new one, that takes the old one's place in the file at once. Each test works
in a folder of its own in the app's folder, removed after it, and nothing they
print says what is in that file.
"""

import json
import os
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from stickle.sync.targets.dropbox import (
    PAGE,
    Dropbox,
    _rpc,  # pyright: ignore[reportPrivateUsage]
)

VARIABLE = "STICKLE_TEST_DROPBOX"


def account_file() -> Path | None:
    named = os.environ.get(VARIABLE)
    return Path(named) if named else None


def account_given() -> bool:
    return account_file() is not None


def keep_account(file: Path, app_key: str, refresh_token: str) -> None:
    """The file written anew: whole, or not at all."""
    fresh = file.with_name(f"{file.name}.new")
    fresh.write_text(json.dumps({"app_key": app_key, "refresh_token": refresh_token}), "utf-8")
    fresh.replace(file)


def signed_in(folder: str = "", page: int = PAGE) -> Dropbox:
    """A target for the test account: in folder, in the app's folder."""
    file = account_file()
    assert file is not None
    written = cast(dict[str, str], json.loads(file.read_text("utf-8-sig")))
    app_key = written["app_key"]

    def renewed(token: str) -> None:
        keep_account(file, app_key, token)

    return Dropbox(app_key, written["refresh_token"], renewed, folder=folder, page=page)


def another_way_in(target: Dropbox) -> Dropbox:
    """To the same folder with connections and an access token of its own:
    another device."""
    return signed_in("/".join(target._folder))  # pyright: ignore[reportPrivateUsage]


@contextmanager
def folder_of_its_own(page: int = PAGE) -> Generator[Dropbox]:
    """A target whose Stickle folder is a new folder in the app's folder."""
    name = f"stickle-test-{secrets.token_hex(4)}"
    target = signed_in(name, page)
    try:
        yield target
    finally:
        # The folder and all in it; none made, Dropbox says there is none.
        _rpc(
            target._shared,  # pyright: ignore[reportPrivateUsage]
            target._api,  # pyright: ignore[reportPrivateUsage]
            "/2/files/delete_v2",
            {"path": target._path("")},  # pyright: ignore[reportPrivateUsage]
        )
        target.close()
