"""Whether a newer Stickle is out, from the version file on the website.

The file is static JSON, the same for everyone:

    {"format": 1, "latest": "0.5.0", "page": "https://github.com/…/releases/…"}

Nothing is sent with the request that could tell one user from another, and
anything unexpected in the answer is taken as no news rather than trusted:
the page offered must be on the website or the project's GitHub.
"""

import json
import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

FEED_URL = "https://stickle.linkro.co/version.json"
FEED_HOST = "stickle.linkro.co"
MAX_FEED_BYTES = 64 * 1024
# Where the page offered may be.
PAGE_HOSTS = ("stickle.linkro.co", "github.com")
PAGE_PATH = re.compile(r"^/(linkro-app/Stickle/releases(/.*)?|[a-z]{2}(/.*)?)?$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(?:preview|rc|beta|alpha)\.?(\d+))?$")


@dataclass(frozen=True)
class Release:
    version: str
    page: str


def version_key(version: str) -> tuple[int, int, int, int] | None:
    """For comparing versions: 0.5.0-preview.1 comes before 0.5.0. None if unreadable."""
    match = _VERSION.match(version.strip())
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    # A preview sorts before the release of the same number.
    return int(major), int(minor), int(patch), int(pre) - 10_000 if pre else 0


def is_newer(latest: str, current: str) -> bool:
    new, now = version_key(latest), version_key(current)
    return new is not None and now is not None and new > now


def _page_allowed(page: str) -> bool:
    parts = urlsplit(page)
    return (
        parts.scheme == "https"
        and parts.hostname in PAGE_HOSTS
        and parts.port is None
        and not parts.username
        and bool(PAGE_PATH.match(parts.path))
    )


def read_feed(content: bytes) -> Release | None:
    """The release the file names, or None if it is not one this app understands."""
    if len(content) > MAX_FEED_BYTES:
        return None
    try:
        document: object = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(document, dict):
        return None
    fields = cast(dict[object, object], document)
    latest, page = fields.get("latest"), fields.get("page")
    if fields.get("format") != 1 or not isinstance(latest, str) or not isinstance(page, str):
        return None
    if version_key(latest) is None or not _page_allowed(page):
        return None
    return Release(latest, page)


def newer_release(content: bytes, current: str) -> Release | None:
    """The release named in the file, if it is newer than this one."""
    release = read_feed(content)
    return release if release is not None and is_newer(release.version, current) else None
