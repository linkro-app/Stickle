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

ONEDRIVE_VARIABLE = "STICKLE_ONEDRIVE_CLIENT_ID"
BUILT_IN = "stickle._cloud_apps"

_CLIENT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def is_client_id(text: str) -> bool:
    """A Microsoft application (client) ID: a GUID."""
    return _CLIENT_ID.fullmatch(text) is not None


def onedrive_client_id() -> str | None:
    """The app's ID for Microsoft, as the build put it in, or else as the
    environment gives it; None if neither does."""
    try:
        built = importlib.import_module(BUILT_IN)
    except ImportError:
        given: object = os.environ.get(ONEDRIVE_VARIABLE, "").strip()
    else:
        given = getattr(built, "ONEDRIVE_CLIENT_ID", None)
    return given if isinstance(given, str) and is_client_id(given) else None
