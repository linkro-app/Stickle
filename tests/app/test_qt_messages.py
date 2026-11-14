"""Qt's own messages in Stickle's log."""

import logging

import pytest
from PySide6.QtCore import QMessageLogContext, QtMsgType

from stickle.app.application import log_qt_message

PORTAL = (
    'Failed to register with host portal QDBusError("org.freedesktop.portal.Error.Failed", '
    '"Could not register app ID: Connection already associated with an application ID")'
)


def test_qt_warnings_are_logged_as_warnings(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="qt"):
        log_qt_message(QtMsgType.QtWarningMsg, QMessageLogContext(), "something went wrong")

    assert [record.levelno for record in caplog.records] == [logging.WARNING]


@pytest.mark.parametrize(
    "message",
    [
        PORTAL,  # printed at every start on GNOME, and seen as an error while testing
        # With a screen reader on Linux, tens of thousands an hour (Orca, Ubuntu):
        'Could not find accessible on path: "/org/a11y/atspi/accessible/2147484960"',
        "Received text event for invalid interface.",
        'Subscription string not handled: "Window:Destroy:"',
        'AtSpiAdaptor::applicationInterface does not implement "GetApplicationBusAddress"'
        ' "/org/a11y/atspi/accessible/root"',
    ],
)
def test_a_warning_that_means_nothing_for_stickle_is_kept_quiet(
    caplog: pytest.LogCaptureFixture, message: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger="qt"):
        log_qt_message(QtMsgType.QtWarningMsg, QMessageLogContext(), message)

    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
