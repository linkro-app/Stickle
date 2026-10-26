import os
from collections.abc import Iterator

import pytest

# Qt tests run without a display, locally and in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def secret_taken_back() -> Iterator[None]:
    """A secret copied is taken back after each test, as Stickle does when it
    ends: left on the clipboard, it crashes the tests' process at its end."""
    yield
    from stickle.platform.clipboard import take_back_secret

    take_back_secret()
