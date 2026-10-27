import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Qt tests run without a display, locally and in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# On Windows the tests' files go to a folder of their own, which can be left
# out of virus scanning without leaving out the whole temporary folder.
# tmp_path, tempfile and the processes the tests start all follow it.
if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
    _scratch = Path(local) / "stickle-test-tmp"
    _scratch.mkdir(exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = tempfile.tempdir = str(_scratch)


@pytest.fixture(autouse=True)
def secret_taken_back() -> Iterator[None]:
    """A secret copied is taken back after each test, as Stickle does when it
    ends: left on the clipboard, it crashes the tests' process at its end."""
    yield
    from stickle.platform.clipboard import take_back_secret

    take_back_secret()
