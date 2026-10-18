import json

import pytest

from stickle.core.updates import (
    MAX_FEED_BYTES,
    Release,
    is_newer,
    newer_release,
    read_feed,
    version_key,
)

PAGE = "https://github.com/linkro-app/Stickle/releases/tag/v0.5.0-preview.1"


def feed(**fields: object) -> bytes:
    document: dict[str, object] = {"format": 1, "latest": "0.5.0", "page": PAGE}
    document.update(fields)
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.4.0", "0.5.0"),
        ("0.5.0-preview.1", "0.5.0"),
        ("0.5.0-preview.1", "0.5.0-preview.2"),
        ("0.9.9", "1.0.0"),
        ("1.0.0", "1.0.1"),
        ("0.4.0", "0.5.0-preview.1"),
    ],
)
def test_versions_compare_as_released(older: str, newer: str) -> None:
    assert is_newer(newer, older)
    assert not is_newer(older, newer)
    assert not is_newer(newer, newer)


def test_an_unreadable_version_is_never_newer() -> None:
    assert version_key("next") is None
    assert not is_newer("next", "0.4.0")
    assert not is_newer("0.5.0", "dev")


def test_the_file_names_a_release() -> None:
    assert read_feed(feed()) == Release("0.5.0", PAGE)
    assert read_feed(feed(page="https://stickle.linkro.co/ko/download")) is not None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[1, 2]",
        "\xff".encode("latin-1"),
        feed(format=2),
        feed(latest=5),
        feed(latest="soon"),
        feed(page="http://github.com/linkro-app/Stickle/releases"),
        feed(page="https://evil.example/linkro-app/Stickle/releases"),
        feed(page="https://github.com/someone/else/releases"),
        feed(page="https://github.com:8443/linkro-app/Stickle/releases"),
        feed(page="https://user@github.com/linkro-app/Stickle/releases"),
        feed(page="javascript:alert(1)"),
        feed(page="file:///etc/passwd"),
    ],
)
def test_anything_unexpected_is_no_news(content: bytes) -> None:
    assert read_feed(content) is None


def test_an_oversized_file_is_no_news() -> None:
    assert read_feed(feed() + b" " * MAX_FEED_BYTES) is None


def test_only_a_newer_release_is_offered() -> None:
    assert newer_release(feed(latest="0.5.0"), "0.4.0") == Release("0.5.0", PAGE)
    assert newer_release(feed(latest="0.4.0"), "0.4.0") is None
    assert newer_release(feed(latest="0.3.0"), "0.4.0") is None
