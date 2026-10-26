"""Text put on the clipboard marked as a secret, for clipboard histories to leave out.

Windows keeps what is copied in its clipboard history (Win+V) and can send it
to the user's other devices; desktop clipboard managers keep a history too.
Password managers mark what they copy so that these leave it out: Windows
reads three clipboard formats, KDE's Klipper (and others that follow it) a
hint. A clipboard tool that ignores the marks still sees the text.

A secret still on the clipboard when Stickle ends is taken off it: it is not
left behind, and the clipboard data, made here rather than by Qt, must not
outlive Python (PySide frees it twice then, and the process crashes).
"""

import sys

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication
from shiboken6 import Shiboken

NOT_IN_HISTORY = QByteArray(bytes(4))  # a DWORD 0: "no"

# Clipboard formats Windows reads by name (Qt registers each as written after value=).
WINDOWS_FORMATS = (
    # Its mere presence keeps the rest out of the history and the cloud.
    'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"',
    'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"',
    'application/x-qt-windows-mime;value="CanUploadToCloudClipboard"',
)
KDE_HINT = "x-kde-passwordManagerHint"

_copied: list[int] = []  # where the secret last copied is (its address), until taken back


def secret_text(text: str, platform: str = sys.platform) -> QMimeData:
    """text, with the marks of a secret where the system reads any."""
    data = QMimeData()
    data.setText(text)
    if platform == "win32":
        for name in WINDOWS_FORMATS:
            data.setData(name, NOT_IN_HISTORY)
    elif platform.startswith("linux"):
        data.setData(KDE_HINT, QByteArray(b"secret"))
    return data


def _address(data: QMimeData) -> int:
    return int(Shiboken.getCppPointer(data)[0])


def copy_secret(text: str) -> None:
    """text on the clipboard, marked as a secret."""
    data = secret_text(text)
    QGuiApplication.clipboard().setMimeData(data)  # the clipboard's from now on
    _copied[:] = [_address(data)]


def take_back_secret() -> None:
    """Before Stickle ends: the secret it copied, if still on the clipboard,
    taken off it. Anything copied since, by Stickle or another app, stays."""
    if not _copied or QGuiApplication.instance() is None:
        return
    clipboard = QGuiApplication.clipboard()
    data = clipboard.mimeData()
    # Still the very data copied (another app's copy reads as other data), with its marks.
    marks = (*WINDOWS_FORMATS, KDE_HINT)
    same = _address(data) == _copied[0]
    if same and (any(data.hasFormat(name) for name in marks) or not _marked_here()):
        clipboard.clear()
    _copied.clear()


def _marked_here(platform: str = sys.platform) -> bool:
    return platform == "win32" or platform.startswith("linux")
