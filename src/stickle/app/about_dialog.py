"""About Stickle: what it is, its version and licence, and one line on support.

Opened from the Stickle window and the tray. The support line is one of the
two places Stickle mentions support (the other is the tray menu); nothing
else in the app asks for it.
"""

from typing import override

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from stickle import __version__
from stickle.app import note_window
from stickle.app.app_icon import icon_pixmap
from stickle.app.feedback import open_feedback
from stickle.app.support import LICENSE, PRIVACY, SOURCE, open_support, site_page
from stickle.core.updates import FEED_HOST

ICON_SIZE = 64


def _link_label(parent: QWidget) -> QLabel:
    """Links reached with Tab and opened with Enter, as well as by a click."""
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    label.linkActivated.connect(note_window.open_link)
    return label


class AboutDialog(QDialog):
    def __init__(
        self, parent: QWidget | None = None, checks_updates: bool = False, portable: bool = False
    ) -> None:
        super().__init__(parent)
        self._checks_updates = checks_updates
        self._portable = portable
        self.connections = QLabel(self)
        self.connections.setWordWrap(True)
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
        layout.addWidget(self.connections)
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
        source = self.tr("Source code")
        privacy = self.tr("Privacy")
        self.links.setText(
            f'<a href="{site_page()}">{website}</a> · '
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
        # Every address the app itself connects to, said where it can be read.
        if self._checks_updates:
            self.connections.setText(
                self.tr(
                    "Stickle connects only to %1, once a day, to see whether a new version "
                    "is out. It sends nothing about you or your notes, and the check can be "
                    "turned off."
                ).replace("%1", FEED_HOST)
            )
        else:
            self.connections.setText(self.tr("Stickle connects to nothing on the internet."))
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
