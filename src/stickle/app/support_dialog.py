"""Support Stickle from the copy installed from the Microsoft Store.

Three sizes of support, each a Store add-on that adds nothing to the app. A
choice opens the Store's own purchase dialog over this window; the Store
shows the price in the user's currency and takes the payment. The answer is
only used to say thanks or that the Store could not be reached, there and
then: nothing about it is kept, and every feature stays free either way.
"""

import logging
from collections.abc import Callable
from typing import Protocol, override

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

log = logging.getLogger(__name__)
POLL_MS = 250

# The add-ons' Store IDs (Partner Center), smallest first.
SUPPORT_ITEMS = ("9NFXMVHKF1DX", "9NQ1Z47DG4F2", "9NTV941PHQHZ")


class Pending(Protocol):
    def outcome(self) -> object | None: ...

    def close(self) -> None: ...


# window handle, Store ID -> the open purchase dialog (raises OSError if it cannot open)
type Purchaser = Callable[[int, str], Pending]


def store_purchaser() -> Purchaser:
    import sys

    assert sys.platform == "win32"
    from stickle.platform.windows.store_purchase import request_purchase

    return request_purchase


class SupportDialog(QDialog):
    def __init__(
        self,
        purchase: Purchaser,
        open_website: Callable[[], object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._purchase = purchase
        self._open_website = open_website
        self._pending: Pending | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        self.text = QLabel(self)
        self.text.setWordWrap(True)
        self.size_buttons = [QPushButton(self) for _ in SUPPORT_ITEMS]
        for button, store_id in zip(self.size_buttons, SUPPORT_ITEMS, strict=True):
            button.clicked.connect(lambda _=False, store_id=store_id: self._choose(store_id))
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()
        self.website_button = QPushButton(self)
        self.website_button.clicked.connect(self._website)
        self.website_button.hide()
        self.close_button = QPushButton(self)
        self.close_button.clicked.connect(self.close)

        sizes = QHBoxLayout()
        for button in self.size_buttons:
            sizes.addWidget(button)
        bottom = QHBoxLayout()
        bottom.addWidget(self.website_button)
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addSpacing(8)
        layout.addLayout(sizes)
        layout.addWidget(self.status)
        layout.addSpacing(8)
        layout.addLayout(bottom)
        self.setMinimumWidth(400)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Support Stickle"))
        self.text.setText(
            self.tr(
                "Choose an amount. The Microsoft Store shows the price and takes the "
                "payment. Every feature is free, whether or not you support Stickle."
            )
        )
        names = (self.tr("&Small"), self.tr("&Medium"), self.tr("&Large"))
        for button, name in zip(self.size_buttons, names, strict=True):
            button.setText(name)
        self.website_button.setText(self.tr("Support on the &website…"))
        self.close_button.setText(self.tr("Close"))

    def present(self) -> None:
        """Shown, or brought forward if it already is, with the keyboard on Medium."""
        self.status.hide()
        self.website_button.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        self.size_buttons[1].setFocus()

    def _choose(self, store_id: str) -> None:
        if self._pending is not None:
            return  # the Store's dialog is already open
        try:
            self._pending = self._purchase(int(self.winId()), store_id)
        except OSError as error:
            log.warning("could not open the Store's purchase dialog: %s", error)
            self._could_not_open()
            return
        log.info("Store purchase dialog opened")
        self._set_choosing(False)
        self._timer.start()

    def _poll(self) -> None:
        pending = self._pending
        if pending is None:
            self._timer.stop()
            return
        try:
            outcome = pending.outcome()
        except OSError as error:
            log.warning("the Store's purchase dialog failed: %s", error)
            outcome = "FAILED"
        if outcome is None:
            return
        self._timer.stop()
        self._pending = None
        self._set_choosing(True)
        name = getattr(outcome, "name", str(outcome))
        log.info("Store purchase dialog closed: %s", name.lower())
        if name in ("SUCCEEDED", "ALREADY_PURCHASED"):
            self._say(self.tr("Thank you for supporting Stickle."))
        elif name in ("NETWORK_ERROR", "SERVER_ERROR", "FAILED"):
            self._could_not_open()
        # NOT_PURCHASED: closed by the user, nothing to say.

    def _could_not_open(self) -> None:
        self._say(self.tr("The Microsoft Store could not take the payment just now."))
        self.website_button.show()

    def _say(self, text: str) -> None:
        self.status.setText(text)
        self.status.show()

    def _set_choosing(self, on: bool) -> None:
        for button in self.size_buttons:
            button.setEnabled(on)

    def _website(self) -> None:
        self._open_website()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)
