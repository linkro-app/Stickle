"""What Qt (and PySide) says while something is done: its warnings, as the app's
log would have them."""

from collections.abc import Generator
from contextlib import contextmanager

from PySide6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler


@contextmanager
def qt_said() -> Generator[list[str]]:
    """Within a test's own body: pytest-qt puts its handler back as the test
    ends, before a fixture's teardown. Put back later, the handler a fixture
    found would be one pytest-qt had let go of, left to the end of the run."""
    said: list[str] = []

    def heard(_kind: QtMsgType, _context: QMessageLogContext, message: str) -> None:
        said.append(message)

    before = qInstallMessageHandler(heard)
    try:
        yield said
    finally:
        qInstallMessageHandler(before)
