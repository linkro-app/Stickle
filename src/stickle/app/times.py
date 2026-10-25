"""Times kept in UTC (stickle.core.clock), as the user reads them here."""

from PySide6.QtCore import QCoreApplication, QDateTime, QLocale, Qt


def local_time(stamp: str) -> str:
    """The day and time, in this computer's time zone and the interface language."""
    moment = QDateTime.fromString(stamp, Qt.DateFormat.ISODateWithMs).toLocalTime()
    pattern = QCoreApplication.translate(
        "Times", "MMMM d, h:mm AP", "a day and time, as QDateTime formats them"
    )
    return QLocale().toString(moment, pattern)
