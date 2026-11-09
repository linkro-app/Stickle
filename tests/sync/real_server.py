"""The real WebDAV server of whoever runs these tests, for those that need one.

STICKLE_TEST_ACCOUNTS names a file kept apart from the code, of the form
{"webdav": {"address": ..., "user": ..., "password": ...}}. Without it they do
not run, as in the commit checks and on CI. Each works in a folder of its own
under the address, made for it and removed after it, and nothing they print
says what is in that file.
"""

import json
import os
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from stickle.sync.targets.webdav import Address, WebDav, parse_address


@dataclass(frozen=True)
class Account:
    address: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)


def account() -> Account | None:
    file = os.environ.get("STICKLE_TEST_ACCOUNTS")
    if not file:
        return None
    written = cast(dict[str, dict[str, str]], json.loads(Path(file).read_text("utf-8-sig")))
    webdav = written["webdav"]
    return Account(webdav["address"], webdav["user"], webdav["password"])


def account_given() -> bool:
    return account() is not None


@contextmanager
def folder_of_its_own(*, keeps_conditions: bool = False) -> Generator[WebDav]:
    """A target whose Stickle folder is in a new folder under the address."""
    given = account()
    assert given is not None
    base = parse_address(given.address)
    name = f"stickle-test-{secrets.token_hex(4)}"
    inside = Address(base.secure, base.host, base.port, f"{base.path}/{name}")
    target = WebDav(inside, given.user, given.password, keeps_conditions=keeps_conditions)
    folder = f"{inside.path}/"
    made = target._ask("MKCOL", folder)  # pyright: ignore[reportPrivateUsage]
    assert made.status == 201, made.status
    try:
        yield target
    finally:
        target._ask("DELETE", folder)  # pyright: ignore[reportPrivateUsage]
        target.close()


def another_way_in(target: WebDav) -> WebDav:
    """To the same folder over a connection of its own: another device."""
    given = account()
    assert given is not None
    return WebDav(
        target._address,  # pyright: ignore[reportPrivateUsage]
        given.user,
        given.password,
    )
