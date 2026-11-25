"""The identifiers the clouds gave Stickle as an app, put in when it is built.

They are no secret (a browser shows them at sign-in), but they are not in the
source either: a copy built from it registers an app of its own, rather than
signing people in under Stickle's name. A build writes them into
stickle._cloud_apps (scripts/build.py); run from the source, the app takes them
from the environment instead. Without one, that cloud is not offered.
"""

import importlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

ONEDRIVE_VARIABLE = "STICKLE_ONEDRIVE_CLIENT_ID"
DROPBOX_VARIABLE = "STICKLE_DROPBOX_APP_KEY"
BUILT_IN = "stickle._cloud_apps"

_CLIENT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_APP_KEY = re.compile(r"[a-z0-9]{15}")


def is_client_id(text: str) -> bool:
    """A Microsoft application (client) ID: a GUID."""
    return _CLIENT_ID.fullmatch(text) is not None


def is_app_key(text: str) -> bool:
    """A Dropbox app key: 15 lowercase letters and digits."""
    return _APP_KEY.fullmatch(text) is not None


@dataclass(frozen=True)
class Identifier:
    """One cloud's: where a build and the environment give it, and its shape."""

    cloud: str  # as said in a build's error and the self-test
    variable: str  # the environment's
    name: str  # the built-in module's
    shape: Callable[[str], bool]
    what: str  # as a build says what it is not


ONEDRIVE = Identifier(
    "OneDrive",
    ONEDRIVE_VARIABLE,
    "ONEDRIVE_CLIENT_ID",
    is_client_id,
    "a Microsoft application (client) ID",
)
DROPBOX = Identifier(
    "Dropbox", DROPBOX_VARIABLE, "DROPBOX_APP_KEY", is_app_key, "a Dropbox app key"
)
IDENTIFIERS = (ONEDRIVE, DROPBOX)


def given(identifier: Identifier) -> str | None:
    """The identifier as the build put it in, or else as the environment gives
    it; None if neither does, or it is not of its shape."""
    try:
        built = importlib.import_module(BUILT_IN)
    except ImportError:
        value: object = os.environ.get(identifier.variable, "").strip()
    else:
        value = getattr(built, identifier.name, None)
    return value if isinstance(value, str) and identifier.shape(value) else None


def onedrive_client_id() -> str | None:
    """The app's ID for Microsoft."""
    return given(ONEDRIVE)


def dropbox_app_key() -> str | None:
    """The app's key for Dropbox."""
    return given(DROPBOX)
