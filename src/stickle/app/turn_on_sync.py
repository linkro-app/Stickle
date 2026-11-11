"""Turning sync on, in one window going from step to step: what it is, where to,
the server, and the Stickle folder there (made, or opened with what the other
computers open it with), then the first round going on.

Nothing is kept until the folder is made or opened: cancelled before that, this
computer is as it was. Opening a folder there before, with notes on this
computer too, first shows what merging would make, the cloud's notes
downloaded to count them; nothing is kept until the user chooses to merge or
to keep only the cloud's notes. Whatever talks to the server is done off the
app's thread, so the window goes on answering. A wrong password is said, and
tried again only when the user asks. No password goes to a server whose
certificate no public authority issued until the user has looked at it and
trusted it.

Opened on request: this is what brings TLS and HTTP into a run.
"""

import logging
from collections.abc import Callable
from typing import override

from PySide6.QtCore import QEvent, QLocale, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from stickle.app.background import Job
from stickle.app.error_text import error_style
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog
from stickle.app.secret_dialogs import (
    ask_recovery_key,
    confirm_new_recovery_key,
    show_recovery_key,
)
from stickle.app.sync_setup import SyncGuideLine, SyncSetup
from stickle.app.sync_status import MANY, WEBDAV_NAME, SyncWords
from stickle.app.syncing import Ended
from stickle.app.typed import forget_typed
from stickle.crypto.keyfile import KeyFileError
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY
from stickle.data.sync_connection import WEBDAV, Login
from stickle.sync.engine import GETTING, FirstLook
from stickle.sync.format import NewerFormatError
from stickle.sync.targets.base import CertificateError, NoAccessError, SignInNeededError
from stickle.sync.targets.webdav import (
    Address,
    AddressError,
    NoWebDavFolderError,
    OutsideHomeError,
    WebDav,
    fingerprint,
    parse_address,
)
from stickle.sync.vault import (
    NoWayInError,
    Vault,
    VaultGoneError,
    VaultThereError,
    is_there,
    join,
    make,
    unfinished,
)

log = logging.getLogger(__name__)

WIDTH = 540
GROUP = 2  # characters of a fingerprint shown together
GROUPS_IN_A_LINE = 16
SHADE = 0.65  # dim text: this much of the text's colour over the window's, still easy to read
RECENT = 3  # the cloud's notes edited last whose first lines are shown


def shown_fingerprint(certificate: bytes) -> str:
    """As servers' admin pages show it: in capitals, in pairs, on two lines."""
    digits = fingerprint(certificate).upper()
    pairs = [digits[at : at + GROUP] for at in range(0, len(digits), GROUP)]
    lines = [pairs[at : at + GROUPS_IN_A_LINE] for at in range(0, len(pairs), GROUPS_IN_A_LINE)]
    return "\n".join(":".join(line) for line in lines)


def _wrapped(parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setWordWrap(True)
    return label


def _small(parent: QWidget) -> QLabel:
    label = _wrapped(parent)
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * 0.9)
    label.setFont(font)
    return label


def _heading(parent: QWidget) -> QLabel:
    label = _wrapped(parent)
    font = QFont(label.font())
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * 1.2)
    label.setFont(font)
    return label


def _page() -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    return page, layout


def _dim(label: QLabel, palette: QPalette) -> None:
    """In a colour between the text's and the window's, from the window's palette."""
    text = palette.color(QPalette.ColorRole.WindowText)
    back = palette.color(QPalette.ColorRole.Window)
    mixed = QColor(
        round(text.red() * SHADE + back.red() * (1 - SHADE)),
        round(text.green() * SHADE + back.green() * (1 - SHADE)),
        round(text.blue() * SHADE + back.blue() * (1 - SHADE)),
    )
    shown = QPalette(label.palette())
    shown.setColor(QPalette.ColorRole.WindowText, mixed)
    label.setPalette(shown)


class _OneLine(QLabel):
    """A line of its own, cut short with … to the room there is (read whole by
    screen readers): a long first line does not widen the window."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setAccessibleName(text)
        self._fit()

    @override
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        metrics = self.fontMetrics()
        self.setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, self.width()))


class _Relay(QObject):
    """How far a download has got, from the thread doing it to the window: kept
    by the work, so that a window gone meanwhile is not told."""

    moved = Signal(str, int, int)


class _Pages(QWidget):
    """One page at a time, and as tall as that one: a QStackedWidget is always
    as tall as the tallest of its pages."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._pages: list[QWidget] = []

    def add(self, page: QWidget) -> None:
        self._layout.addWidget(page)
        page.setVisible(not self._pages)
        self._pages.append(page)

    def current(self) -> QWidget:
        return next(page for page in self._pages if not page.isHidden())

    def show_page(self, page: QWidget) -> None:
        for each in self._pages:
            each.setVisible(each is page)


def _look(target: WebDav) -> bool:
    """Whether a vault is there, once the server has shown it is one to use."""
    target.check()
    return is_there(target)


class TurnOnSync(QDialog):
    def __init__(self, setup: SyncSetup, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup = setup
        self._words = SyncWords(self)
        self._job: Job | None = None
        self._target: WebDav | None = None
        self._address: Address | None = None
        self._certificate: bytes | None = None  # trusted, for this address
        self._shown_certificate: bytes | None = None
        self._joining = False
        self._ended = False  # the first round is through, or stopped
        self._line: Callable[[], str] = lambda: ""
        # The folder opened, while what merging would make is shown: the login is
        # read once, as the typed password is forgotten then.
        self._opened_login: Login | None = None
        self._preview: FirstLook | None = None  # what it would make (None: not known yet)
        self._relay: _Relay | None = None

        self.pages = _Pages(self)
        self.intro_page = self._make_intro_page()
        self.choose_page = self._make_choose_page()
        self.server_page = self._make_server_page()
        self.certificate_page = self._make_certificate_page()
        self.absent_page = self._make_absent_page()
        self.present_page = self._make_present_page()
        self.preview_page = self._make_preview_page()
        self.confirm_page = self._make_confirm_page()
        self.last_page = self._make_last_page()
        for page in (
            self.intro_page,
            self.choose_page,
            self.server_page,
            self.certificate_page,
            self.absent_page,
            self.present_page,
            self.preview_page,
            self.confirm_page,
            self.last_page,
        ):
            self.pages.add(page)

        self.message = _wrapped(self)
        self.message.hide()
        self.back_button = QPushButton(self)
        self.cloud_only_button = QPushButton(self)  # beside the next step, as the other choice
        self.next_button = QPushButton(self)
        self.cancel_button = QPushButton(self)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (
            self.back_button,
            self.cloud_only_button,
            self.next_button,
            self.cancel_button,
        ):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        self.next_button.setDefault(True)
        self.back_button.clicked.connect(self._back)
        self.cloud_only_button.clicked.connect(self._ask_cloud_only)
        self.next_button.clicked.connect(self._next)
        self.cancel_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.message)
        layout.addLayout(buttons)
        self.setMinimumWidth(WIDTH)
        self.resize(WIDTH, self.height())
        self.retranslate()
        self._show(self.intro_page)

    # The pages

    def _make_intro_page(self) -> QWidget:
        page, layout = _page()
        self.intro_heading = _heading(page)
        self.intro_text = _wrapped(page)
        self.intro_preview = _wrapped(page)
        self.guide = SyncGuideLine(page)
        for part in (self.intro_heading, self.intro_text, self.intro_preview):
            layout.addWidget(part)
        layout.addStretch(1)
        layout.addWidget(self.guide)
        return page

    def _make_choose_page(self) -> QWidget:
        page, layout = _page()
        self.choose_heading = _heading(page)
        self.webdav_choice = QRadioButton(page)
        self.webdav_choice.setChecked(True)
        layout.addWidget(self.choose_heading)
        layout.addWidget(self.webdav_choice)
        layout.addStretch(1)
        return page

    def _make_server_page(self) -> QWidget:
        page, layout = _page()
        self.server_heading = _heading(page)
        self.address = QLineEdit(page)
        self.address.setInputMethodHints(Qt.InputMethodHint.ImhUrlCharactersOnly)
        self.user = QLineEdit(page)
        self.password = QLineEdit(page)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.address_label = QLabel(page)
        self.address_label.setBuddy(self.address)
        self.user_label = QLabel(page)
        self.user_label.setBuddy(self.user)
        self.password_label = QLabel(page)
        self.password_label.setBuddy(self.password)
        form = QFormLayout()
        form.addRow(self.address_label, self.address)
        form.addRow(self.user_label, self.user)
        form.addRow(self.password_label, self.password)
        self.nextcloud_note = _small(page)
        self.address_note = _wrapped(page)
        self.address_note.hide()
        layout.addWidget(self.server_heading)
        layout.addLayout(form)
        layout.addWidget(self.address_note)
        layout.addWidget(self.nextcloud_note)
        layout.addStretch(1)
        self.address.textChanged.connect(self._address_changed)
        for field in (self.user, self.password):
            field.textChanged.connect(self._update_buttons)
        return page

    def _make_certificate_page(self) -> QWidget:
        page, layout = _page()
        self.certificate_text = _wrapped(page)
        self.fingerprint_label = _wrapped(page)
        self.fingerprint = QLabel(page)
        self.fingerprint.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for part in (self.certificate_text, self.fingerprint_label, self.fingerprint):
            layout.addWidget(part)
        layout.addStretch(1)
        return page

    def _make_absent_page(self) -> QWidget:
        page, layout = _page()
        self.absent_text = _wrapped(page)
        self.recovery_line = _wrapped(page)
        self.check_key_button = QPushButton(page)
        self.check_key_button.setAutoDefault(False)
        self.check_key_button.clicked.connect(self._check_recovery_key)
        self.make_key_button = QPushButton(page)
        self.make_key_button.setAutoDefault(False)
        self.make_key_button.clicked.connect(self._make_recovery_key)
        row = QHBoxLayout()
        row.addWidget(self.check_key_button)
        row.addWidget(self.make_key_button)
        row.addStretch(1)
        layout.addWidget(self.absent_text)
        layout.addSpacing(8)
        layout.addWidget(self.recovery_line)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _make_present_page(self) -> QWidget:
        page, layout = _page()
        self.present_text = _wrapped(page)
        self.by_key = QRadioButton(page)
        self.by_key.setChecked(True)
        self.by_password = QRadioButton(page)
        self.way_in = QLineEdit(page)
        self.neither_note = _small(page)
        layout.addWidget(self.present_text)
        layout.addWidget(self.by_key)
        layout.addWidget(self.by_password)
        layout.addWidget(self.way_in)
        layout.addWidget(self.neither_note)
        layout.addStretch(1)
        self.by_key.toggled.connect(self._way_in_chosen)
        self.way_in.textChanged.connect(self._update_buttons)
        return page

    def _make_preview_page(self) -> QWidget:
        page, layout = _page()
        self.preview_counts = _wrapped(page)
        self.recent_heading = _wrapped(page)
        self.recent_lines = [_OneLine(page) for _ in range(RECENT)]
        layout.addWidget(self.preview_counts)
        layout.addSpacing(8)
        layout.addWidget(self.recent_heading)
        for line in self.recent_lines:
            _dim(line, self.palette())
            layout.addWidget(line)
        layout.addStretch(1)
        return page

    def _make_confirm_page(self) -> QWidget:
        page, layout = _page()
        self.confirm_text = _wrapped(page)
        layout.addWidget(self.confirm_text)
        layout.addStretch(1)
        return page

    def _make_last_page(self) -> QWidget:
        page, layout = _page()
        self.last_line = _wrapped(page)
        layout.addWidget(self.last_line)
        layout.addStretch(1)
        return page

    def retranslate(self) -> None:
        self.setWindowTitle(self.tr("Turn on sync"))
        self.intro_heading.setText(self.tr("Sync your notes through your own cloud"))
        self.intro_text.setText(
            self.tr(
                "Your notes always stay on this computer. Only encrypted files go to the cloud."
            )
        )
        self.intro_preview.setText(
            self.tr(
                "Sync is still a preview, and its file format may change before 1.0. A newer"
                " Stickle converts it by itself; your other computers carry on once they update."
            )
        )
        self.choose_heading.setText(self.tr("Where should your notes sync?"))
        self.webdav_choice.setText(
            self.tr("&WebDAV — your own server, such as Nextcloud or a Synology NAS")
        )
        self.server_heading.setText(WEBDAV_NAME)
        self.address_label.setText(self.tr("Server &address:"))
        self.user_label.setText(self.tr("&User name:"))
        self.password_label.setText(self.tr("&Password:"))
        self.address.setAccessibleName(self.tr("Server address"))
        self.user.setAccessibleName(self.tr("User name"))
        self.password.setAccessibleName(self.tr("Password"))
        self.nextcloud_note.setText(
            self.tr(
                "For Nextcloud, make an app password in Settings under Security and use it here."
            )
        )
        self.certificate_text.setText(
            self.tr(
                "This server uses a certificate it made itself. Trust it only if the"
                " fingerprint below matches the one on the server's admin page."
            )
        )
        self.fingerprint_label.setText(
            self.tr("SHA-256 fingerprint (the check code unique to this certificate)")
        )
        self.fingerprint.setAccessibleName(self.tr("SHA-256 fingerprint"))
        self.check_key_button.setText(self.tr("&Check my existing recovery key…"))
        self.present_text.setText(
            self.tr(
                "There's already a Stickle folder in %1. Enter the recovery key or spare"
                " password of the computers that already sync. If you've made a new recovery"
                " key since, use the latest one."
            ).replace("%1", WEBDAV_NAME)
        )
        self.by_key.setText(self.tr("&Recovery key"))
        self.by_password.setText(self.tr("S&pare password"))
        self.neither_note.setText(
            self.tr(
                "If you have neither, make a new recovery key on a computer that already"
                " syncs (Settings, Protection) and enter it here."
            )
        )
        self.back_button.setText(self.tr("&Back"))
        self.cloud_only_button.setText(self.tr("&Keep only the cloud notes…"))
        self.recent_heading.setText(self.tr("Recently changed in the cloud"))
        self._show_way_in_name()
        self._show_address_note()
        self._show_recovery_state()
        self._show_look()
        if self.pages.current() is self.preview_page and self._preview is not None:
            self.next_button.setAccessibleDescription(self._look_told())
        self.last_line.setText(self._line())
        self._update_buttons()

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.PaletteChange:
            for line in self.recent_lines:
                _dim(line, self.palette())
        super().changeEvent(event)

    def _show(self, page: QWidget) -> None:
        self.pages.show_page(page)
        self._clear_message()
        self._update_buttons()
        focus: QWidget = self.next_button
        if page is self.choose_page:
            focus = self.webdav_choice
        elif page is self.server_page:
            focus = self.address
        elif page is self.present_page:
            focus = self.way_in
        elif page is self.last_page:
            focus = self.cancel_button
        if page is self.absent_page and not self.next_button.isEnabled():
            focus = self.make_key_button
        if page is self.confirm_page:
            focus = self.back_button  # what cannot be undone is not one Enter away
        # What the page says, with the button that has the keyboard.
        if page is self.preview_page and self._preview is not None:
            self.next_button.setAccessibleDescription(self._look_told())
        self.back_button.setAccessibleDescription(
            self.confirm_text.text() if page is self.confirm_page else ""
        )
        focus.setFocus()
        self._fit()

    def _fit(self) -> None:
        """As tall as what is on it now: wrapped text is not told to the window."""
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()
        wanted = layout.totalHeightForWidth(self.width())
        self.resize(self.width(), wanted if wanted > 0 else layout.totalSizeHint().height())

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit()

    def _update_buttons(self) -> None:
        page = self.pages.current()
        busy = self._job is not None
        self.pages.setEnabled(not busy)
        last = page is self.last_page
        self.back_button.setVisible(page not in (self.intro_page, self.preview_page) and not last)
        self.back_button.setEnabled(not busy)
        self.cloud_only_button.setVisible(page is self.preview_page and self._preview is not None)
        self.cloud_only_button.setEnabled(not busy)
        self.next_button.setVisible(not last)
        self.next_button.setEnabled(not busy and self._can_go_on())
        self.next_button.setText(self._next_text())
        # Enter does not delete: on the page that asks, it goes back.
        asking = page is self.confirm_page
        self.next_button.setDefault(not asking)
        self.back_button.setDefault(asking)
        self.cancel_button.setText(self.tr("Close") if last else self.tr("Cancel"))

    def _next_text(self) -> str:
        page = self.pages.current()
        if page is self.server_page:
            return self.tr("&Connect")
        if page is self.certificate_page:
            return self.tr("&Trust this certificate")
        if page is self.absent_page:
            return self.tr("&Start")
        if page is self.present_page:
            return self.tr("&Open")
        if page is self.preview_page:
            if self._preview is None and self._job is None:  # the download failed
                return self.tr("&Try again")
            return self.tr("&Merge (recommended)")
        if page is self.confirm_page:
            return self.tr("&Delete and bring in")
        return self.tr("&Next")

    def _can_go_on(self) -> bool:
        page = self.pages.current()
        if page is self.server_page:
            filled = self.address.text().strip() and self.user.text() and self.password.text()
            return bool(filled) and not self._outside_home()
        if page is self.absent_page:
            return self._recovery_state() == "ready"
        if page is self.present_page:
            return bool(self.way_in.text())
        return True

    # Messages

    def _clear_message(self) -> None:
        self.message.clear()
        self.message.hide()
        self.next_button.setAccessibleDescription("")

    def _say(self, text: str) -> None:
        self.message.setStyleSheet("")
        self.message.setText(text)
        self.message.show()
        self._fit()

    def _fail(self, text: str, field: QWidget | None = None) -> None:
        self.message.setStyleSheet(error_style(self.message))
        self.message.setText(text)
        self.message.show()
        self.next_button.setAccessibleDescription(text)
        self._fit()
        if field is not None:
            field.setFocus()
            if isinstance(field, QLineEdit):
                field.selectAll()

    # Going from step to step

    def _back(self) -> None:
        page = self.pages.current()
        if page is self.choose_page:
            self._show(self.intro_page)
        elif page is self.server_page:
            self._show(self.choose_page)
        elif page is self.confirm_page:
            self._show(self.preview_page)
        else:
            self._close_target()
            self._show(self.server_page)

    def _next(self) -> None:
        page = self.pages.current()
        if page is self.intro_page:
            self._show(self.choose_page)
        elif page is self.choose_page:
            self._show(self.server_page)
        elif page is self.server_page:
            self._connect()
        elif page is self.certificate_page:
            self._certificate = self._shown_certificate
            self._connect()
        elif page is self.absent_page:
            self._start()
        elif page is self.present_page:
            self._open()
        elif page is self.preview_page:
            if self._preview is None:
                self._look_first()
            else:
                self._connected(self._ready_login(), True)
        elif page is self.confirm_page:
            self._connected(self._ready_login(), True, only_the_cloud=True)

    def _begin(self, work: Callable[[], object], then: Callable[..., None], busy: str) -> None:
        def ended(outcome: object) -> None:
            self._job = None
            self._update_buttons()
            self._clear_message()
            if isinstance(outcome, Exception):
                self._failed(outcome)
            else:
                then(outcome)

        self._say(busy)
        self._job = Job(work, ended)
        self._update_buttons()

    # The server

    def _address_changed(self) -> None:
        self._certificate = None  # trusted for the server that was there
        self._show_address_note()
        self._update_buttons()

    def _outside_home(self) -> bool:
        try:
            parse_address(self.address.text())
        except OutsideHomeError:
            return True
        except AddressError:
            return False
        return False

    def _is_plain_at_home(self) -> bool:
        try:
            return not parse_address(self.address.text()).secure
        except AddressError:
            return False

    def _show_address_note(self) -> None:
        if self._outside_home():
            self.address_note.setText(
                self.tr("A server outside your home network needs an https address.")
            )
        elif self._is_plain_at_home():
            self.address_note.setText(
                self.tr(
                    "This connection isn't encrypted. Your files are, but your server password"
                    " is sent as it is. If your server can use https, use an https address; a"
                    " certificate it made itself works too. Use http only on your home network."
                )
            )
        else:
            self.address_note.clear()
        self.address_note.setVisible(bool(self.address_note.text()))
        self._fit()

    def _connect(self) -> None:
        try:
            address = parse_address(self.address.text())
        except AddressError:
            self._show(self.server_page)
            self._fail(
                self.tr("That server address can't be read. Enter one that starts with https://."),
                self.address,
            )
            return
        self._close_target()
        target = WebDav(address, self.user.text(), self.password.text(), self._certificate)
        self._address, self._target = address, target
        self._begin(lambda: _look(target), self._looked, self.tr("Checking the server…"))

    def _looked(self, there: object) -> None:
        if there:
            self._show(self.present_page)
        else:
            self._show_recovery_state()
            self._show(self.absent_page)

    def _look_again(self) -> None:
        target = self._target
        assert target is not None
        self._begin(lambda: _look(target), self._looked, self.tr("Checking the server…"))

    def _failed(self, error: Exception) -> None:
        log.info("sync: turning on: %s", type(error).__name__)
        match error:
            case CertificateError():
                self._shown_certificate = error.certificate
                self.fingerprint.setText(shown_fingerprint(error.certificate))
                self._show(self.certificate_page)
            case VaultThereError() | VaultGoneError():  # another computer got there first
                self._look_again()
            case SignInNeededError():
                self._fail_at_server(
                    self.tr(
                        "The server didn't accept the user name or password."
                        " Check them and try again."
                    ),
                    self.password,
                )
            case NoAccessError():
                self._fail_at_server(
                    self.tr(
                        "This account can't write to the folder at this address."
                        " Check its permissions on the server."
                    ),
                    self.address,
                )
            case NoWebDavFolderError():
                self._fail_at_server(
                    self.tr("There's no WebDAV folder at that address. Check the address."),
                    self.address,
                )
            case NewerFormatError():
                self._fail(
                    self.tr(
                        "A newer version of Stickle uses this folder."
                        " Update Stickle, then turn on sync again."
                    )
                )
            case RecoveryKeyTypoError():
                self._fail(
                    self.tr(
                        "This is not a recovery key as written: a character may be missing,"
                        " extra or mistyped. Please check it again."
                    ),
                    self.way_in,
                )
            case NoWayInError():
                self._fail(self._opens_nothing(), self.way_in)
            case _:
                self._fail(
                    self.tr(
                        "Couldn't reach the server. Check the address and your network,"
                        " then try again."
                    )
                )

    def _fail_at_server(self, text: str, field: QWidget) -> None:
        page = self.pages.current()
        if page in (self.server_page, self.certificate_page):
            self._show(self.server_page)
            self._fail(text, field)
        else:
            self._fail(text)

    def _opens_nothing(self) -> str:
        if self.by_key.isChecked():
            return self.tr(
                "This recovery key doesn't open this Stickle folder."
                " If you've made a new one since, use the latest one."
            )
        return self.tr("This spare password doesn't open this Stickle folder.")

    # The Stickle folder is not there: made

    def _absent_sentence(self) -> str:
        return self.tr(
            "Stickle will make its folder in %1 and upload the %Ln note(s) on this computer."
            " On your other computers, turn on sync with this computer's recovery key or"
            " spare password.",
            "",
            self._setup.note_count(),
        ).replace("%1", WEBDAV_NAME)

    def _recovery_state(self) -> str:
        recovery = self._setup.recovery
        if not recovery.exists():
            return "none"
        if recovery.is_old():
            return "old"
        return "ready" if recovery.was_kept() else "not_kept"

    def _show_recovery_state(self) -> None:
        self.absent_text.setText(self._absent_sentence())
        state = self._recovery_state()
        if state == "none":
            line = self.tr(
                "Make a recovery key first and keep it off this computer. You'll need it to"
                " turn on sync on your other computers."
            )
        elif state == "ready":
            line = ""
        else:
            line = self.tr(
                "Stickle doesn't know yet whether you kept your recovery key. Check the one"
                " you have, or make a new one if you can't find it. You'll need it to turn on"
                " sync on your other computers."
            )
        self.recovery_line.setText(line)
        self.recovery_line.setVisible(bool(line))
        self.check_key_button.setVisible(state in ("old", "not_kept"))
        self.make_key_button.setVisible(state != "ready")
        self.make_key_button.setText(
            self.tr("&Make a recovery key…")
            if state == "none"
            else self.tr("&Make a new recovery key…")
        )

    def _check_recovery_key(self) -> None:
        """The recovery key the user has, typed: counted as kept from then on."""
        recovery = self._setup.recovery
        take = recovery.upgrade if self._recovery_state() == "old" else recovery.confirm
        dialog: EnterRecoveryKeyDialog

        def submit(typed: str) -> str | None:
            try:
                take(typed)
            except RecoveryKeyTypoError:
                return dialog.typo()
            except WrongRecoveryKeyError:
                return dialog.wrong_key()
            except (KeyFileError, OSError, RuntimeError, MemoryError) as error:
                log.error("recovery key not checked: %s", type(error).__name__)
                return self.tr("The recovery key could not be kept (%1).").replace(
                    "%1", type(error).__name__
                )
            return None

        dialog = EnterRecoveryKeyDialog(submit, self)
        ask_recovery_key(dialog)
        self._recovery_shown()

    def _make_recovery_key(self) -> None:
        """A new recovery key, shown; any earlier one stops working."""
        recovery = self._setup.recovery
        if recovery.exists() and not confirm_new_recovery_key(self):
            return
        try:
            recovery_key = recovery.make()
        except (KeyFileError, OSError) as error:
            log.error("could not make a recovery key: %s", type(error).__name__)
            self._fail(self.tr("The recovery key could not be saved."))
            return
        if show_recovery_key(RecoveryKeyDialog(recovery_key, self)):
            recovery.kept()
        self._recovery_shown()

    def _recovery_shown(self) -> None:
        self._show_recovery_state()
        self._update_buttons()
        self._fit()

    def _start(self) -> None:
        target = self._target
        assert target is not None
        pairs = self._setup.pairs()
        if RECOVERY_KEY not in pairs:  # its key file no longer reads
            self._fail(self.tr("The recovery key could not be saved."))
            return
        self._begin(
            lambda: make(target, pairs, unfinished(target)),
            self._made,
            self.tr("Making the Stickle folder…"),
        )

    def _made(self, vault: object) -> None:
        assert isinstance(vault, Vault)
        self._connected(self._login(vault), False)

    # The Stickle folder is there: opened

    def _way_in_chosen(self) -> None:
        self.way_in.clear()
        self.way_in.setEchoMode(
            QLineEdit.EchoMode.Normal if self.by_key.isChecked() else QLineEdit.EchoMode.Password
        )
        self._show_way_in_name()
        self._clear_message()
        self.way_in.setFocus()

    def _show_way_in_name(self) -> None:
        self.way_in.setAccessibleName(
            self.tr("Recovery key") if self.by_key.isChecked() else self.tr("Spare password")
        )

    def _open(self) -> None:
        target = self._target
        assert target is not None
        kind = RECOVERY_KEY if self.by_key.isChecked() else MASTER_PASSWORD
        typed = self.way_in.text()
        self._begin(
            lambda: join(target, kind, typed),  # takes a moment on purpose (it slows down guessing)
            self._opened,
            self.tr("Opening the Stickle folder…"),
        )

    def _opened(self, vault: object) -> None:
        assert isinstance(vault, Vault)
        login = self._login(vault)
        if self._setup.look is None or self._setup.note_count() == 0:
            self._connected(login, True)  # nothing here to weigh against the cloud's
            return
        self._opened_login = login
        forget_typed(self, self.way_in)
        self._close_target()  # the look reaches the server on its own
        self._look_first()

    # What merging would make, before anything is kept

    def _ready_login(self) -> Login:
        assert self._opened_login is not None
        return self._opened_login

    def _login(self, vault: Vault) -> Login:
        assert self._address is not None
        return Login(
            vault.vault,
            WEBDAV,
            self._address.text,
            self.user.text(),
            self.password.text(),
            vault.keys.keys,
            self._certificate,
        )

    def _look_first(self) -> None:
        look = self._setup.look
        assert look is not None
        login = self._ready_login()
        relay = _Relay()
        relay.moved.connect(self._moved_on)
        self._relay = relay
        self._preview = None
        self._show(self.preview_page)
        self._begin(
            lambda: look(login, relay.moved.emit),
            self._first_looked,
            self.tr("Downloading the notes in the cloud…"),
        )

    def _moved_on(self, doing: str, done: int, of: int) -> None:
        """How far the download has got, while it goes on and there are many."""
        if self._job is None or doing != GETTING or of <= MANY:
            return
        locale = QLocale()
        self._say(
            self.tr("Downloading the notes in the cloud · %1 of %2")
            .replace("%1", locale.toString(done))
            .replace("%2", locale.toString(of))
        )

    def _first_looked(self, look: object) -> None:
        self._relay = None
        if not isinstance(look, FirstLook):
            self._fail(
                self.tr(
                    "Couldn't download the notes in the cloud. Check your network and try again."
                )
            )
            return
        if look.here <= look.alike:  # each of this computer's becomes one of the cloud's
            self._connected(self._ready_login(), True)
            return
        self._preview = look
        self._show_look()
        self._show(self.preview_page)

    def _show_look(self) -> None:
        """What merging would make, on the page that shows it."""
        look = self._preview
        recent = [] if look is None else look.recent[:RECENT]
        self.preview_counts.setVisible(look is not None)
        self.recent_heading.setVisible(bool(recent))
        for at, line in enumerate(self.recent_lines):
            line.setVisible(at < len(recent))
            title = recent[at] if at < len(recent) else ""
            if title is None:
                line.set_full_text(self.tr("Secret note"))
            else:
                line.set_full_text(title or self.tr("Empty note"))
            font = QFont(line.font())
            font.setItalic(not title)  # what stands for a line, not one of the note's
            line.setFont(font)
        if look is None:
            return
        counts = [
            self.tr("%Ln note(s) on this computer", "", look.here),
            self.tr("%Ln note(s) in the cloud", "", look.cloud),
        ]
        if look.alike:
            counts.append(self.tr("%Ln have the same text and become one.", "", look.alike))
        self.preview_counts.setText("\n".join(counts))
        self.confirm_text.setText(
            self.tr(
                "This deletes the %Ln note(s) on this computer and brings in the cloud notes."
                " A backup is made first.",
                "",
                look.here,
            )
        )

    def _look_told(self) -> str:
        """The page that shows what merging would make, as screen readers say it
        with the button that has the keyboard."""
        lines = [self.preview_counts.text()]
        shown = [line.full_text() for line in self.recent_lines if not line.isHidden()]
        if shown:
            lines += [self.recent_heading.text(), *shown]
        return "\n".join(lines)

    def _ask_cloud_only(self) -> None:
        self._show(self.confirm_page)

    # Connected

    def _connected(self, login: Login, joining: bool, only_the_cloud: bool = False) -> None:
        try:
            if only_the_cloud:
                assert self._setup.connect_cloud_only is not None
                syncing = self._setup.connect_cloud_only(login)
            else:
                syncing = self._setup.connect(login, joining)
        except Exception as error:  # nothing kept, nothing let go of: the user may try again
            log.error("sync: could not be turned on: %s", type(error).__name__)
            if only_the_cloud:
                self._fail(
                    self.tr(
                        "Couldn't make the backup, so the notes on this computer weren't"
                        " deleted. Check that the disk has room, then try again."
                    )
                )
            else:
                self._fail(
                    self.tr(
                        "Couldn't reach the server. Check the address and your network,"
                        " then try again."
                    )
                )
            return
        self._joining = joining
        self._close_target()
        forget_typed(self, self.way_in)
        self._set_line(self._going_on)
        self._show(self.last_page)
        syncing.moving.connect(self._moving)
        syncing.round_shown.connect(self._round_shown)

    def _set_line(self, line: Callable[[], str]) -> None:
        self._line = line
        self.last_line.setText(line())
        self._fit()

    def _going_on(self) -> str:
        if self._joining:
            return self.tr("Downloading your notes. You can close this window; it carries on.")
        return self.tr("Uploading your notes. You can close this window; it carries on.")

    def _moving(self, doing: str, done: int, of: int) -> None:
        if self._ended:
            return
        locale = QLocale()
        count, total = locale.toString(done), locale.toString(of)

        def line() -> str:
            said = (
                self.tr(
                    "Downloading your notes · %1 of %2. You can close this window; it carries on."
                )
                if doing == GETTING
                else self.tr(
                    "Uploading your notes · %1 of %2. You can close this window; it carries on."
                )
            )
            return said.replace("%1", count).replace("%2", total)

        self._set_line(line)

    def _round_shown(self, ended: Ended) -> None:
        if self._ended:
            return
        outcome = ended.outcome
        if outcome.stopped is not None:
            why = outcome.stopped
            self._set_line(lambda: self._words.stopped(why, WEBDAV_NAME))
        elif outcome.full:
            self._set_line(lambda: self._words.full(WEBDAV_NAME))
        elif outcome.wait is not None:
            return  # tried again by itself: the line stays as it is
        else:
            self._set_line(self._finished)
        self._ended = True

    def _finished(self) -> str:
        if self._joining:
            return self.tr("Done. This computer's notes now sync too.")
        return self.tr(
            "Done. On your other computers, choose Turn on sync… under Settings, Connections,"
            " and enter this computer's recovery key or spare password."
        )

    # Closing

    def _close_target(self) -> None:
        if self._target is not None:
            self._target.close()
            self._target = None

    @override
    def done(self, result: int, /) -> None:
        if self._job is not None:  # what it is doing is let finish, and comes to nothing
            target, self._target = self._target, None
            self._job.drop(target.close if target is not None else lambda: None)
            self._job = None
        self._close_target()
        forget_typed(self, self.way_in)
        super().done(result)
        # Used once: not kept, hidden, by the window it belongs to until that closes.
        self.deleteLater()
