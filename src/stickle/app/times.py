"""Times kept in UTC (stickle.core.clock), as the user reads them here: a day
of another year with its year, so that it is not taken for one this year."""

from PySide6.QtCore import QCoreApplication, QDate, QDateTime, QLocale, Qt


def _this_year(day: QDate) -> bool:
    return day.year() == QDate.currentDate().year()


def day_text(day: QDate) -> str:
    """A day alone, in the interface language."""
    if _this_year(day):
        pattern = QCoreApplication.translate("Times", "MMMM d", "a day, as QDate formats it")
    else:
        pattern = QCoreApplication.translate(
            "Times", "MMMM d, yyyy", "a day of another year, as QDate formats it"
        )
    return QLocale().toString(day, pattern)


def moment_text(moment: QDateTime, weekday: bool = False) -> str:
    """A day and time as given (not moved to another time zone), in the
    interface language; weekday: with its day of the week."""
    this_year = _this_year(moment.date())
    if weekday and this_year:
        pattern = QCoreApplication.translate(
            "Times", "ddd, MMMM d, h:mm AP", "a day and time, as QDateTime formats them"
        )
    elif weekday:
        pattern = QCoreApplication.translate(
            "Times",
            "ddd, MMMM d, yyyy, h:mm AP",
            "a day and time of another year, as QDateTime formats them",
        )
    elif this_year:
        pattern = QCoreApplication.translate(
            "Times", "MMMM d, h:mm AP", "a day and time, as QDateTime formats them"
        )
    else:
        pattern = QCoreApplication.translate(
            "Times",
            "MMMM d, yyyy, h:mm AP",
            "a day and time of another year, as QDateTime formats them",
        )
    return QLocale().toString(moment, pattern)


def local_time(stamp: str) -> str:
    """The day and time, in this computer's time zone and the interface language."""
    return moment_text(QDateTime.fromString(stamp, Qt.DateFormat.ISODateWithMs).toLocalTime())
