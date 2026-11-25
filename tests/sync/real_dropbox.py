"""The real Dropbox of whoever runs these tests, for those that need one.

STICKLE_TEST_DROPBOX names a file kept apart from the code, of the form
{"app_key": ..., "refresh_token": ...}, as a sign-in with a test account left
it (sign_in_dropbox.py). Without it they do not run, as in the commit checks
and on CI. Dropbox's refresh token lasts until revoked; should it ever give a
new one, that takes the old one's place in the file at once. Nothing printed
says what is in that file.
"""

import json
import os
from pathlib import Path
from typing import cast

from stickle.sync.targets.dropbox import Dropbox

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


def signed_in(folder: str = "") -> Dropbox:
    """A target for the test account: in folder, in the app's folder."""
    file = account_file()
    assert file is not None
    written = cast(dict[str, str], json.loads(file.read_text("utf-8-sig")))
    app_key = written["app_key"]

    def renewed(token: str) -> None:
        keep_account(file, app_key, token)

    return Dropbox(app_key, written["refresh_token"], renewed, folder=folder)
