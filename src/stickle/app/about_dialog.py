"""About Stickle: what it is, its version and licence, and one line on support.

Opened from the Stickle window and the tray. The support line is one of the
two places Stickle mentions support (the other is the tray menu); nothing
else in the app asks for it. A build also links to the licenses of what it
includes, written next to the program when it was built.
"""

from pathlib import Path
from typing import override

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stickle import __version__
from stickle.app import note_window
from stickle.app.app_icon import icon_pixmap
from stickle.app.feedback import open_feedback
from stickle.app.link_label import LinkLabel
from stickle.app.support import LICENSE, PRIVACY, SOURCE, open_support, site_page
from stickle.platform.paths import own_files_folder

ICON_SIZE = 64
NOTICES = Path("licenses") / "THIRD-PARTY-NOTICES.txt"


def third_party_notices() -> Path | None:
    """The list of what the build includes, with their licenses; None when run
    from source, which has none."""
    folder = own_files_folder()
    if folder is None or not (folder / NOTICES).is_file():
        return None
    return folder / NOTICES


def _link_label(parent: QWidget) -> LinkLabel:
    """Web links, opened in the browser."""
    label = LinkLabel(parent)
    label.setWordWrap(True)
    label.linkActivated.connect(note_window.open_link)
    return label


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, portable: bool = False) -> None:
        super().__init__(parent)
        self._portable = portable
        # A draft issue in the browser; Stickle sends nothing itself.
        self.feedback_button = QPushButton(self)
        self.feedback_button.clicked.connect(self.report)
        self.icon = QLabel(self)
        self.icon.setPixmap(icon_pixmap(ICON_SIZE))
        self.name = QLabel("Stickle", self)
        font = self.name.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.6)
        self.name.setFont(font)
        self.version = QLabel(self)
        self.version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.links = _link_label(self)
        self.license = _link_label(self)
        # A file on this computer, so not through open_link, which opens web pages only.
        self._notices = third_party_notices()
        self.notices = LinkLabel(self)
        self.notices.linkActivated.connect(self.open_notices)
        self.notices.setVisible(self._notices is not None)
        self.support_text = QLabel(self)
        self.support_text.setWordWrap(True)
        self.support_button = QPushButton(self)
        self.support_button.clicked.connect(open_support)
        self.close_button = QPushButton(self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.close)

        heading = QVBoxLayout()
        heading.addWidget(self.name)
        heading.addWidget(self.version)
        top = QHBoxLayout()
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addSpacing(12)
        top.addLayout(heading, 1)
        support = QHBoxLayout()
        support.addWidget(self.support_text, 1)
        support.addWidget(self.support_button, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addSpacing(8)
        layout.addWidget(self.summary)
        layout.addWidget(self.links)
        layout.addWidget(self.license)
        layout.addWidget(self.notices)
        feedback = QHBoxLayout()
        feedback.addWidget(self.feedback_button)
        feedback.addStretch(1)
        layout.addLayout(feedback)
        layout.addSpacing(12)
        layout.addLayout(support)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self.setMinimumWidth(440)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("About Stickle"))
        self.icon.setAccessibleName("Stickle")
        self.version.setText(self.tr("Version %1").replace("%1", __version__))
        self.summary.setText(
            self.tr(
                "Notes that stay on your desktop, saved as you type and kept encrypted "
                "on your computer. No account, no server."
            )
        )
        # Translated outside the f-string: lupdate does not read calls inside one.
        website = self.tr("Website")
        guide = self.tr("Guide")
        source = self.tr("Source code")
        privacy = self.tr("Privacy")
        self.links.setText(
            f'<a href="{site_page()}">{website}</a> · '
            f'<a href="{site_page("guide")}">{guide}</a> · '
            f'<a href="{SOURCE}">{source}</a> · '
            f'<a href="{PRIVACY}">{privacy}</a>'
        )
        self.links.setAccessibleName(self.tr("Links"))
        self.license.setText(
            f'<a href="{LICENSE}">'
            + self.tr("Free software under the GNU General Public License, version 3 or later.")
            + "</a>"
        )
        self.license.setAccessibleName(self.tr("License"))
        notices = self.tr("Open source licenses")
        self.notices.setText(f'<a href="notices">{notices}</a>')
        self.notices.setAccessibleName(notices)
        self.support_text.setText(
            self.tr(
                "If Stickle is useful to you, your support keeps it going. "
                "Every feature is free, whether or not you support Stickle."
            )
        )
        self.feedback_button.setText(self.tr("&Report a problem or idea…"))
        self.feedback_button.setToolTip(
            self.tr("Opens a draft on GitHub in your browser. Nothing is sent until you submit it.")
        )
        self.support_button.setText(self.tr("&Support Stickle…"))
        self.close_button.setText(self.tr("Close"))

    def report(self) -> None:
        open_feedback(self._portable)

    def open_notices(self) -> None:
        """In the system's app for text files."""
        if self._notices is not None:
            note_window.open_url(QUrl.fromLocalFile(str(self._notices)))

    def present(self) -> None:
        """Shown, or brought forward if it already is, with the keyboard on Close."""
        self.show()
        self.raise_()
        self.activateWindow()
        self.close_button.setFocus()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
