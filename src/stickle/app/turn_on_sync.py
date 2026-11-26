"""Turning sync on, in one window going from step to step: what it is, where to,
the server (or signing in to OneDrive or Dropbox in the browser), and the Stickle folder
there (made, or opened with what the other computers open it with), then the
first round going on.

Nothing is kept until the folder is made or opened: cancelled before that, this
computer is as it was. Opening a folder there before, with notes on this
computer too, first shows what merging would make, the cloud's notes
downloaded to count them; nothing is kept until the user chooses to merge or
to keep only the cloud's notes. Whatever talks to the server is done off the
app's thread, so the window goes on answering. A wrong password is said, and
tried again only when the user asks. No password goes to a server whose
certificate no public authority issued until the user has looked at it and
trusted it.

Sync stopped with its Stickle folder gone, made anew, or the server showing
another certificate, the same window goes on from the server as kept: it looks
at the server at once and goes on as that is now. The folder as it was, sync
just goes on (with the certificate trusted anew, if that was it); gone, a new
one is made from this computer's notes; another there, it is opened with its
key. A folder there is never made over. Nothing of the connection kept changes
until the new one is kept in its place. Stopped with the server turning down
the password kept, it starts with the password to type anew: the one turned
down is not sent again (servers count those, and lock the account out).
OneDrive and Dropbox likewise start from signing in anew, when the cloud turned
the refresh token down.

OneDrive and Dropbox are signed in to in the browser: a listener on this
computer hears the browser sent back, for as long as the step waits for it, and
is closed however that ends (signing in again, going back, cancelling, closing).
With every port registered with Dropbox taken, the user pastes the code Dropbox
shows them instead, as many times as it takes. Each new refresh token the cloud
gives on the way is the one kept, should sync be turned on; signing in and
cancelling keeps none.

Opened on request: this is what brings TLS and HTTP into a run.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, override

from PySide6.QtCore import QByteArray, QEvent, QLocale, QObject, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QKeyEvent,
    QPalette,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtNetwork import QSsl, QSslCertificate
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
from stickle.app.support import site_language
from stickle.app.sync_setup import SyncGuideLine, SyncSetup
from stickle.app.sync_status import BLOCKERS, MANY, SyncWords, cloud_name
from stickle.app.syncing import Ended
from stickle.app.typed import forget_typed
from stickle.crypto.following import digest_of
from stickle.crypto.keyfile import KeyFileError
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.crypto.vault_keys import MASTER_PASSWORD, RECOVERY_KEY, DataKeys
from stickle.data.sync_connection import DROPBOX, ONEDRIVE, SIGNED_IN, WEBDAV, Login
from stickle.platform.linux.x11 import activate
from stickle.sync.engine import GETTING, FirstLook
from stickle.sync.format import NewerFormatError
from stickle.sync.keys import newest
from stickle.sync.targets.base import (
    CertificateError,
    NoAccessError,
    SignInNeededError,
    TooBusyError,
    UnavailableError,
    UnusableCertificateError,
)
from stickle.sync.targets.dropbox import App as DropboxApp
from stickle.sync.targets.dropbox import Dropbox
from stickle.sync.targets.onedrive import App as OneDriveApp
from stickle.sync.targets.onedrive import OneDrive
from stickle.sync.targets.sign_in import Pages, SignedIn, SignIn, SignInFailedError
from stickle.sync.targets.webdav import (
    Address,
    AddressError,
    NoWebDavFolderError,
    OutsideHomeError,
    WebDav,
    fingerprint,
    issued_for,
    parse_address,
)
from stickle.sync.vault import (
    NoWayInError,
    Vault,
    VaultGoneError,
    VaultThereError,
    join,
    make,
    there,
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


THEIRS = "theirs"  # the vault's spare password becomes this computer's
MINE = "mine"  # this computer's becomes the vault's, which has none


@dataclass(frozen=True)
class _Taken:
    """What joining a vault does to this computer's recovery key and spare password."""

    recovery: bool = False  # this computer's recovery key becomes the vault's
    secrets_wait: bool = False  # its secret notes still open with the old one until opened
    master: Literal["theirs", "mine"] | None = None


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
        """The page shown hidden before the next one is: never two at once, for
        the window would be made as tall as both (on Windows, with a warning)
        and stay so, going back to a page before it."""
        for each in self._pages:
            if each is not page:
                each.setVisible(False)
        page.setVisible(True)


type _Reached = WebDav | OneDrive | Dropbox


class _CodeField(QLineEdit):
    """Where the code Dropbox shows is pasted: Enter there enters it, and does
    not go on to the window's next step as well."""

    @override
    def keyPressEvent(self, event: QKeyEvent) -> None:
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()


class _Token:
    """The refresh token to sign in to OneDrive or Dropbox with: the newest
    given, from whichever thread is told of it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = ""

    @property
    def value(self) -> str:
        with self._lock:
            return self._value

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value


def _look(target: _Reached) -> str | None:
    """The vault there, if there is one, once the server has shown it is one to use."""
    if isinstance(target, WebDav):
        try:
            target.check()
        except UnusableCertificateError:
            raise
        except CertificateError as error:
            error.issued_for = _public_name(target.address, error.certificate)
            raise
    return there(target)


def _public_name(address: Address, certificate: bytes) -> str | None:
    """A name the certificate is for that a public authority issued it for, if
    it has one (not one with a *: that names no one server): at that address,
    it would be taken without a word. Looked for by its handshakes alone."""
    shown = QSslCertificate(QByteArray(certificate), QSsl.EncodingFormat.Der)
    names = shown.subjectAlternativeNames().get(QSsl.AlternativeNameEntryType.DnsEntry, [])
    here = address.host.casefold()
    for name in names:
        if "*" not in name and name.casefold() != here and issued_for(address, name):
            return name
    return None


class TurnOnSync(QDialog):
    def __init__(
        self,
        setup: SyncSetup,
        parent: QWidget | None = None,
        again: Login | None = None,
        signed_out: bool = False,
    ) -> None:
        """again: the connection kept, which sync stopped with (its Stickle folder
        gone, made anew, or the server's certificate changed): the window starts
        by looking at the cloud as kept, and goes on as that is now. signed_out:
        it stopped with the cloud turning down what it was signed in with; the
        window starts at the server (or signing in to OneDrive), sending it
        nothing until a new password is typed (or the user has signed in)."""
        super().__init__(parent)
        self._setup = setup
        self._again = again
        self._onedrive = setup.onedrive()  # None: OneDrive is not offered
        self._dropbox = setup.dropbox()  # nor Dropbox
        self._kind = WEBDAV if again is None else again.kind
        self._words = SyncWords(self)
        self._job: Job | None = None
        self._target: _Reached | None = None
        # Signing in to OneDrive or Dropbox: the sign-in waited for, and its
        # work; and a code pasted, being entered.
        self._signing: SignIn | None = None
        self._signing_job: Job | None = None
        self._entering: Job | None = None
        self._sign_in_address = ""
        self._token = _Token()
        self._account: str | None = None
        self._address: Address | None = None
        self._certificate: bytes | None = None  # trusted, for this address
        self._shown_certificate: bytes | None = None
        self._issued_for: str | None = None  # by a public authority, the one shown
        self._joining = False
        self._ended = False  # the first round is through, or stopped
        self._line: Callable[[], str] = lambda: ""
        # The folder opened, while what merging would make is shown: the login is
        # read once, as the typed password is forgotten then.
        self._opened_login: Login | None = None
        # Joining: what of this computer's recovery key and spare password becomes
        # the others' (told once on), and the public key of the recovery key typed.
        self._taken = _Taken()
        self._typed_pair: bytes | None = None
        self._preview: FirstLook | None = None  # what it would make (None: not known yet)
        self._relay: _Relay | None = None

        self.pages = _Pages(self)
        self.intro_page = self._make_intro_page()
        self.choose_page = self._make_choose_page()
        self.sign_in_page = self._make_sign_in_page()
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
            self.sign_in_page,
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
        if again is None:
            self._show(self.intro_page)
            return
        if again.kind in SIGNED_IN:
            self._account = again.user or None
            self._show(self.sign_in_page)
            if not signed_out:  # the refresh token was not turned down: on with it
                self._token.set(again.secret)
                self._reach_cloud()
            return
        self.address.setText(again.address)
        self.user.setText(again.user)
        self._certificate = again.certificate  # after the address, which forgets it
        self._show(self.server_page)
        if signed_out:
            self._fail(self._turned_down(), self.password)
            return
        self.password.setText(again.secret)
        self._connect()

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
        self.onedrive_choice = QRadioButton(page)
        self.dropbox_choice = QRadioButton(page)
        self.webdav_choice = QRadioButton(page)
        self.onedrive_choice.setVisible(self._onedrive is not None)
        self.dropbox_choice.setVisible(self._dropbox is not None)
        offered = [each for each in self._choices() if not each.isHidden()]
        offered[0].setChecked(True)
        self.own_folder_note = _small(page)  # what a cloud of its own folder lets it see
        self.own_folder_note.setVisible(bool(self._own_folders()))
        layout.addWidget(self.choose_heading)
        for choice in self._choices():
            layout.addWidget(choice)
        layout.addWidget(self.own_folder_note)
        layout.addStretch(1)
        return page

    def _choices(self) -> tuple[QRadioButton, ...]:
        return (self.onedrive_choice, self.dropbox_choice, self.webdav_choice)

    def _own_folders(self) -> list[str]:
        """The clouds offered where Stickle has a folder of its own."""
        apps = ((ONEDRIVE, self._onedrive), (DROPBOX, self._dropbox))
        return [cloud_name(kind) for kind, app in apps if app is not None]

    def _own_folder_sentence(self) -> str:
        clouds = self._own_folders()
        if len(clouds) > 1:
            return (
                self.tr(
                    "In %1 and %2, Stickle uses only its own folder and can't see your other files."
                )
                .replace("%1", clouds[0])
                .replace("%2", clouds[1])
            )
        return self.tr(
            "In %1, Stickle uses only its own folder and can't see your other files."
        ).replace("%1", clouds[0] if clouds else "")

    def _signs_in(self) -> bool:
        """Whether the chosen cloud is signed in to in the browser."""
        return self._kind in SIGNED_IN

    def _app(self) -> OneDriveApp | DropboxApp | None:
        """Stickle as registered with the chosen cloud; None: not, by this build."""
        if self._kind == ONEDRIVE:
            return self._onedrive
        return self._dropbox if self._kind == DROPBOX else None

    def _make_sign_in_page(self) -> QWidget:
        page, layout = _page()
        self.sign_in_heading = _heading(page)
        self.sign_in_waiting = _wrapped(page)
        self.other_account_note = _small(page)  # Dropbox: it signs in as the browser is
        self.copy_line = _wrapped(page)
        self.copy_button = QPushButton(page)
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self._copy_address)
        row = QHBoxLayout()
        row.addWidget(self.copy_button)
        row.addStretch(1)
        # With every port Dropbox has registered taken: the code it shows, pasted.
        self.paste_line = _wrapped(page)
        self.code = _CodeField(page)
        self.code_label = QLabel(page)
        self.code_label.setBuddy(self.code)
        self.code_button = QPushButton(page)
        self.code_button.setAutoDefault(False)
        self.code.returnPressed.connect(self._enter_code)
        self.code.textChanged.connect(self._show_code_button)
        self.code_button.clicked.connect(self._enter_code)
        pasted = QHBoxLayout()
        pasted.addWidget(self.code_label)
        pasted.addWidget(self.code, 1)
        pasted.addWidget(self.code_button)
        for part in self._signing_parts():
            part.hide()  # until the browser is sent to sign in
        layout.addWidget(self.sign_in_heading)
        layout.addWidget(self.sign_in_waiting)
        layout.addWidget(self.other_account_note)
        layout.addSpacing(8)
        layout.addWidget(self.copy_line)
        layout.addLayout(row)
        layout.addSpacing(8)
        layout.addWidget(self.paste_line)
        layout.addLayout(pasted)
        layout.addStretch(1)
        return page

    def _signing_parts(self) -> tuple[QWidget, ...]:
        """What the sign-in page shows while the browser is out signing in."""
        return (
            self.sign_in_waiting,
            self.other_account_note,
            self.copy_line,
            self.copy_button,
            *self._pasting_parts(),
        )

    def _pasting_parts(self) -> tuple[QWidget, ...]:
        return (self.paste_line, self.code_label, self.code, self.code_button)

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
        self.certificate_name = _wrapped(page)  # a public authority's, for another name
        self.certificate_name.hide()
        parts = (self.certificate_text, self.fingerprint_label, self.fingerprint)
        for part in (*parts, self.certificate_name):
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
        self.keys_line = _wrapped(page)  # what joining did to the recovery key, if anything
        self.keys_line.hide()
        layout.addWidget(self.keys_line)
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
        self.onedrive_choice.setText(
            self.tr(
                "&OneDrive — a personal Microsoft account. Work or school accounts don't work yet."
            )
        )
        self.dropbox_choice.setText(self.tr("&Dropbox"))
        self.webdav_choice.setText(
            self.tr("&WebDAV — your own server, such as Nextcloud or a Synology NAS")
        )
        self.own_folder_note.setText(self._own_folder_sentence())
        self.sign_in_heading.setText(cloud_name(self._kind if self._signs_in() else ONEDRIVE))
        self.sign_in_waiting.setText(
            self.tr(
                "Sign in on the page that opened in your browser and allow Stickle. When"
                " you're done, you'll continue in this window."
            )
        )
        self.other_account_note.setText(
            self.tr(
                "If the page shows a different %1 account, sign out on the %1 website first,"
                " then choose Sign in with your browser… again."
            ).replace("%1", cloud_name(DROPBOX))
        )
        self.copy_line.setText(self.tr("If no browser opened, copy the address into one."))
        self.copy_button.setText(self.tr("&Copy the address"))
        self.paste_line.setText(
            self.tr("Paste the code shown on the %1 page.").replace("%1", cloud_name(DROPBOX))
        )
        self.code_label.setText(self.tr("Co&de:"))
        self.code.setAccessibleName(self.tr("Code"))
        self.code_button.setText(self.tr("C&onnect"))
        self.server_heading.setText(cloud_name(WEBDAV))
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
        if self._issued_for is not None:
            self.certificate_name.setText(
                self.tr(
                    "This certificate is for %1. Put that name in the address, and you"
                    " won't be asked about the certificate again."
                ).replace("%1", self._issued_for)
            )
        self.check_key_button.setText(self.tr("&Check my existing recovery key…"))
        self.present_text.setText(
            self.tr(
                "There's already a Stickle folder in %1. Enter the recovery key or spare"
                " password of the computers that already sync. If you've made a new recovery"
                " key since, use the latest one."
            ).replace("%1", cloud_name(self._kind))
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
        self.keys_line.setText(self._keys_told())
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
            focus = next(each for each in self._choices() if each.isChecked())
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
        """As tall as what is on it now: wrapped text is not told to the window.
        And wide enough for what cannot wrap, a choice's line, in letters wider on
        some systems than on others."""
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()
        narrowest = max(WIDTH, layout.totalMinimumSize().width())
        if narrowest != self.minimumWidth():
            self.setMinimumWidth(narrowest)
        width = max(self.width(), narrowest)
        wanted = layout.totalHeightForWidth(width)
        self.resize(width, wanted if wanted > 0 else layout.totalSizeHint().height())

    @override
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit()

    def _update_buttons(self) -> None:
        page = self.pages.current()
        busy = self._job is not None
        self.pages.setEnabled(not busy)
        last = page is self.last_page
        first = self._cloud_page() if self._again is not None else self.intro_page
        self.back_button.setVisible(page not in (first, self.preview_page) and not last)
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
        if page is self.sign_in_page:
            return self.tr("&Sign in with your browser…")
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
        if page is self.sign_in_page:
            return self._app() is not None
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

    def _cloud_page(self) -> QWidget:
        """Where the chosen cloud is reached: its server, or signing in."""
        return self.sign_in_page if self._signs_in() else self.server_page

    def _back(self) -> None:
        page = self.pages.current()
        if page is self.choose_page:
            self._show(self.intro_page)
        elif page in (self.server_page, self.sign_in_page):
            self._end_sign_in()
            self._show(self.choose_page)
        elif page is self.confirm_page:
            self._show(self.preview_page)
        else:
            self._close_target()
            self._show(self._cloud_page())

    def _next(self) -> None:
        page = self.pages.current()
        if page is self.intro_page:
            self._show(self.choose_page)
        elif page is self.choose_page:
            kinds = dict(zip(self._choices(), (ONEDRIVE, DROPBOX, WEBDAV), strict=True))
            self._kind = next(kinds[each] for each in self._choices() if each.isChecked())
            self.retranslate()  # what is said of the Stickle folder names the cloud
            self._show(self._cloud_page())
        elif page is self.sign_in_page:
            self._sign_in()
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

    # Signing in to OneDrive or Dropbox

    def _sign_in(self) -> None:
        """The browser sent to sign in, and its coming back waited for (or the
        code it shows, pasted); any sign-in waited for before is let go of (its
        tab may have been closed)."""
        app = self._app()
        assert app is not None
        self._end_sign_in()
        self._close_target()
        self._clear_message()
        signing = app.sign_in(
            Pages(
                site_language(),
                self.tr("You're signed in. You can close this tab and go back to Stickle."),
                self.tr("Sign-in didn't finish. Go back to Stickle and try again."),
            )
        )
        try:
            address = signing.start()
        except UnavailableError:  # no listener could be opened here
            self._fail(self.tr("Couldn't start signing in. Try again."))
            return
        self._signing, self._sign_in_address = signing, address
        self._signing_job = Job(signing.wait, lambda outcome: self._signed_in(signing, outcome))
        pasting = signing.pasting
        for part in (self.sign_in_waiting, self.copy_line, self.copy_button):
            part.show()
        self.other_account_note.setVisible(self._kind == DROPBOX)
        for part in self._pasting_parts():
            part.setVisible(pasting)
        self._show_code_button()
        self._fit()
        # Not opened, the address is there to copy.
        QDesktopServices.openUrl(QUrl.fromEncoded(QByteArray(address.encode("ascii"))))
        if pasting:
            self.code.setFocus()

    def _show_code_button(self) -> None:
        self.code_button.setEnabled(bool(self.code.text().strip()) and self._entering is None)

    def _enter_code(self) -> None:
        """The code pasted, exchanged for the tokens: the sign-in ends with them
        (as the browser coming back would), or the user is told and may try again."""
        signing, code = self._signing, self.code.text()
        if signing is None or not signing.pasting or self._entering is not None:
            return
        if not code.strip():
            return
        self._clear_message()
        self._entering = Job(
            lambda: signing.enter(code), lambda outcome: self._entered(signing, outcome)
        )
        self.code.setEnabled(False)
        self._show_code_button()

    def _entered(self, signing: SignIn, outcome: object) -> None:
        if signing is not self._signing:  # let go of meanwhile
            return
        self._entering = None
        self.code.setEnabled(True)
        self._show_code_button()
        if not isinstance(outcome, Exception):
            return  # signed in: told by the sign-in waited for
        log.info("sync: a code entered: %s", type(outcome).__name__)
        if type(outcome) is SignInFailedError:
            said = self.tr(
                "That code didn't sign you in. Check it, or choose Sign in with your browser…"
                " again."
            )
        else:
            said = self._sign_in_failure(outcome)
        self._fail(said, self.code)

    def _copy_address(self) -> None:
        if self._sign_in_address:
            QGuiApplication.clipboard().setText(self._sign_in_address)

    def _end_sign_in(self) -> None:
        """The sign-in waited for, let go of: its listener closed, nothing of it kept."""
        signing, job, entering = self._signing, self._signing_job, self._entering
        self._signing, self._signing_job, self._sign_in_address = None, None, ""
        self._entering = None
        if signing is not None:
            signing.cancel()
        for each in (job, entering):
            if each is not None:
                each.drop(lambda: None)
        for part in self._signing_parts():
            part.hide()
        self.code.setEnabled(True)
        self.code.clear()  # good once, but not left about

    def _signed_in(self, signing: SignIn, outcome: object) -> None:
        if signing is not self._signing:  # let go of meanwhile
            return
        self._signing_job = None
        self._end_sign_in()
        self._fit()
        self._come_back()
        if isinstance(outcome, Exception):
            self._sign_in_failed(outcome)
        elif isinstance(outcome, SignedIn):
            self._token.set(outcome.refresh_token)
            self._account = outcome.account
            self._reach_cloud()

    def _come_back(self) -> None:
        """Back from the browser, whose page says to go back to Stickle: this
        window brought forward for what comes next. Windows lets no program take
        the keyboard from the one in front, and flashes the window's taskbar
        button instead (the settings', when opened from them)."""
        self.raise_()
        self.activateWindow()
        if QGuiApplication.platformName() == "xcb":
            # Asked for as a taskbar would; the application's own request above
            # may be refused.
            activate(int(self.winId()))

    def _sign_in_failed(self, error: Exception) -> None:
        log.info("sync: signing in: %s", type(error).__name__)
        self._fail(self._sign_in_failure(error))

    def _sign_in_failure(self, error: Exception) -> str:
        match error:
            case SignInFailedError(declined=True):
                return self.tr(
                    "Stickle wasn't allowed, so sign-in didn't finish. To sync, sign in again"
                    " and allow Stickle."
                )
            case NoAccessError():
                return self._blocked()
            case TooBusyError():
                return self._too_busy()
            case UnavailableError():
                return self._unreachable_at(self._blocker())
            case _:
                return self._not_signed_in()

    def _blocker(self) -> str:
        """Who runs the chosen cloud, and may block Stickle from it."""
        return BLOCKERS.get(self._kind, cloud_name(self._kind))

    def _not_signed_in(self) -> str:
        return self.tr("Sign-in didn't finish. Try again.")

    def _blocked(self) -> str:
        return self.tr(
            "%1 has blocked Stickle. Until a new version fixes this, you can use another cloud."
        ).replace("%1", self._blocker())

    def _too_busy(self) -> str:
        return self.tr("%1 isn't taking requests right now. Try again in a little while.").replace(
            "%1", self._blocker()
        )

    def _unreachable_at(self, cloud: str) -> str:
        return self.tr("Couldn't reach %1. Check your network and try again.").replace("%1", cloud)

    def _reach_cloud(self) -> None:
        """The cloud looked at as signed in to: its Stickle folder, if there is one."""
        app = self._app()
        if app is None:  # this build has Stickle registered with no one
            return
        self._close_target()
        target = app.target(self._token.value, self._token.set)
        self._target = target
        self._begin(
            lambda: _look(target),
            self._looked,
            self.tr("Checking %1…").replace("%1", cloud_name(self._kind)),
        )

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

    def _looked(self, vault: object) -> None:
        again = self._again
        if again is not None and vault == again.vault:  # as it was: on with it
            self._connected(self._login(Vault(again.vault, DataKeys(again.keys))), False)
        elif vault is not None:
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
        if self._signs_in() and isinstance(
            error, SignInNeededError | NoAccessError | TooBusyError | UnavailableError | OSError
        ):
            self._failed_at_cloud(error)
            return
        match error:
            case UnusableCertificateError():  # trusted already: no use trusting it again
                self._fail_at_server(
                    self.tr(
                        "This server's certificate can't be used for a safe connection. Make"
                        " a new certificate on the server, then try again."
                    ),
                    self.address,
                )
            case CertificateError():
                self._shown_certificate = error.certificate
                self.fingerprint.setText(shown_fingerprint(error.certificate))
                self._issued_for = error.issued_for
                self.certificate_name.setVisible(error.issued_for is not None)
                self.retranslate()
                self._show(self.certificate_page)
            case VaultThereError() | VaultGoneError():  # another computer got there first
                self._look_again()
            case SignInNeededError():
                self._fail_at_server(self._turned_down(), self.password)
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
                self._fail(self._unreachable())

    def _failed_at_cloud(self, error: Exception) -> None:
        match error:
            case SignInNeededError():  # what it was signed in with is no more
                self._close_target()
                self._show(self.sign_in_page)
                self._fail(self._not_signed_in())
            case NoAccessError():
                self._fail(self._blocked())
            case TooBusyError():
                self._fail(self._too_busy())
            case _:
                self._fail(self._unreachable())

    def _unreachable(self) -> str:
        """The cloud did not answer as it should."""
        if self._signs_in():
            return self._unreachable_at(cloud_name(self._kind))
        return self.tr(
            "Couldn't reach the server. Check the address and your network, then try again."
        )

    def _turned_down(self) -> str:
        return self.tr(
            "The server didn't accept the user name or password. Check them and try again."
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
        if self._again is not None:  # the other computers sync already, and stopped
            return self.tr(
                "Stickle will make a new Stickle folder in %1 and upload the %Ln note(s) on"
                " this computer. Your other computers carry on syncing once they enter this"
                " computer's recovery key or spare password.",
                "",
                self._setup.note_count(),
            ).replace("%1", cloud_name(self._kind))
        return self.tr(
            "Stickle will make its folder in %1 and upload the %Ln note(s) on this computer."
            " On your other computers, turn on sync with this computer's recovery key or"
            " spare password.",
            "",
            self._setup.note_count(),
        ).replace("%1", cloud_name(self._kind))

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
        self._taken = self._what_is_taken(vault)
        self._typed_pair = vault.opened_by if self.by_key.isChecked() else None
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
        """The folder's login, as opened: with the newest refresh token, if signed in to."""
        login = self._opened_login
        assert login is not None
        return replace(login, secret=self._token.value) if login.kind in SIGNED_IN else login

    def _login(self, vault: Vault) -> Login:
        if self._signs_in():
            return Login(
                vault.vault, self._kind, "", self._account or "", self._token.value, vault.keys.keys
            )
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
            lambda: look(login, relay.moved.emit, self._token.set),
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
                self._fail(self._unreachable())
            return
        if self._again is not None and login.vault == self._again.vault:
            self.accept()  # the folder as it was: sync just goes on, and says so where it does
            return
        self._joining = joining
        if joining and self._typed_pair is not None:
            self._setup.keep_recovery_key(self._typed_pair)
        self.keys_line.setText(self._keys_told())
        self.keys_line.setVisible(joining and bool(self.keys_line.text()))
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

    def _what_is_taken(self, vault: Vault) -> _Taken:
        """What joining vault does to this computer's recovery key and spare
        password: each becomes the vault's (the first round takes it), or this
        computer's spare password the vault's, the vault having none."""
        theirs = newest(list(vault.receivers), vault.keys)
        mine = self._setup.pairs()
        recovery, master = theirs.get(RECOVERY_KEY), theirs.get(MASTER_PASSWORD)
        taken = _Taken()
        pair = None if recovery is None else recovery.slot
        if pair is not None and digest_of(pair) != digest_of(mine.get(RECOVERY_KEY)):
            kept = self._setup.secret_notes_kept_for(pair.public)
            taken = replace(taken, recovery=True, secrets_wait=not kept)
        if master is not None and master.slot is not None:
            if digest_of(master.slot) != digest_of(mine.get(MASTER_PASSWORD)):
                taken = replace(taken, master=THEIRS)
        elif MASTER_PASSWORD in mine:
            taken = replace(taken, master=MINE)
        return taken

    def _keys_told(self) -> str:
        """What joining did to the recovery key and the spare password, said on the last page."""
        taken = self._taken
        said: list[str] = []
        if taken.recovery and taken.secrets_wait:
            said.append(
                self.tr(
                    "This computer now opens with the same recovery key as your other"
                    " computers. Until you next open its secret notes, though, they still"
                    " open with the old recovery key, so don't throw it away yet."
                )
            )
        elif taken.recovery:
            said.append(
                self.tr(
                    "This computer now opens with the same recovery key as your other"
                    " computers, so you can throw its old one away."
                )
            )
        if taken.master == THEIRS:
            said.append(self.tr("It also uses the same spare password as your other computers."))
        elif taken.master == MINE:
            said.append(self.tr("Your other computers now use this computer's spare password too."))
        return " ".join(said)

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
            self._set_line(lambda: self._words.stopped(why, self._kind))
        elif outcome.full:
            self._set_line(lambda: self._words.full(self._kind))
        elif outcome.wait is not None:
            return  # tried again by itself: the line stays as it is
        else:
            self._set_line(self._finished)
        self._ended = True

    def _finished(self) -> str:
        if self._joining:
            return self.tr("Done. This computer's notes now sync too.")
        if self._again is not None:
            return self.tr(
                "Done. On your other computers, enter this computer's recovery key or spare"
                " password once."
            )
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
        self._end_sign_in()
        if self._job is not None:  # what it is doing is let finish, and comes to nothing
            target, self._target = self._target, None
            self._job.drop(target.close if target is not None else lambda: None)
            self._job = None
        self._close_target()
        forget_typed(self, self.way_in)
        super().done(result)
        # Used once: not kept, hidden, by the window it belongs to until that closes.
        self.deleteLater()
