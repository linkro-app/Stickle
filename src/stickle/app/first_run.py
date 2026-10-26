"""The first start on a computer: a welcome, a few choices, and the recovery key.

Shown once, when the notes database was just made, before the first note
appears. Page one offers the interface language (it switches at once and is
kept) and asks whether the notes are for this computer only (kept for when
sync exists), whether Stickle starts at login, and, for an AppImage,
whether it goes in the application list. Page two shows the recovery key.
Closing the window without going on changes nothing: no choice is taken for
the user. The sample note is written afterwards, in the language chosen.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from stickle.app.app_list import switch_app_list
from stickle.app.i18n import LANGUAGES, Translations
from stickle.app.master_password import MasterPasswordDialog, MasterPasswords
from stickle.app.recovery_key_dialog import RecoveryKeyPanel
from stickle.app.support import site_page
from stickle.app.tray import switch_autostart
from stickle.data.notes import NoteRepository
from stickle.data.settings import (
    APP_MENU_ASKED,
    RECOVERY_KEY_KEPT,
    SEVERAL_DEVICES,
    THIS_DEVICE,
    USAGE,
    Settings,
)
from stickle.platform.autostart import Autostart, may_be_blocked
from stickle.platform.linux.appimage import AppMenuEntry

log = logging.getLogger(__name__)


def _exec_master(dialog: MasterPasswordDialog) -> object:
    return dialog.exec()


show_master_dialog: Callable[[MasterPasswordDialog], object] = _exec_master  # replaced in tests


def sample_note() -> str:
    """The first note: how Stickle works, in the interface language, as Markdown,
    ending with a link to the guide to everything it does."""
    text = QCoreApplication.translate(
        "FirstRun",
        "# Welcome to Stickle\n"
        "Double-click this note to edit it; click elsewhere to see it formatted again.\n"
        "\n"
        "- [ ] Tick a box like this one\n"
        "- [ ] Double-click the title bar to fold the note\n"
        "- [ ] Use the pin to keep a note above other windows, or not\n"
        "- [ ] Change its colour, category and marks from the menu with three dots\n"
        "- [ ] Put a note near another one: they line up like magnets\n"
        "- [ ] **F1** shows every keyboard shortcut\n"
        "\n"
        "The **X** hides a note: bring it back from the Stickle icon. "
        "**Ctrl+N** makes a new note.\n"
        "Search, the trash and your data (backups, export, notes from other apps) "
        "are in Stickle Manager, opened from the Stickle icon.\n"
        "[Everything Stickle does](%1)",
    )
    return text.replace("%1", site_page("guide"))


@dataclass(frozen=True)
class FirstRunChoices:
    usage: str | None  # THIS_DEVICE, SEVERAL_DEVICES, or None if not answered
    start_at_login: bool
    app_list: bool
    recovery_key_kept: bool


class LoginNote(QLabel):
    """Under the "start at login" box, where turning it on may get Stickle blocked."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setIndent(24)  # under the box's text, not its tick
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.9)
        self.setFont(font)
        self.retranslate()

    def retranslate(self) -> None:
        self.setText(
            self.tr(
                "Windows Security has been seen to block Stickle by mistake when this is on. "
                "A signed version will put that right."
            )
        )

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)


class FirstRunDialog(QDialog):
    def __init__(
        self,
        recovery_key: str,
        offer_start_at_login: bool,
        offer_app_list: bool,
        translations: Translations | None = None,
        parent: QWidget | None = None,
        login_may_be_blocked: bool = False,
        master: MasterPasswords | None = None,
        later_allowed: bool = True,
    ) -> None:
        super().__init__(parent)
        self._offers = (offer_start_at_login, offer_app_list)
        self._translations = translations
        self._master = master
        # Notes locked with a password have no other way back: the key must be kept now.
        self._later_allowed = later_allowed
        self.heading = QLabel()
        font = QFont(self.heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)

        # Page one: welcome and choices.
        self.language_label = QLabel()
        self.language_box = QComboBox()
        self.language_label.setBuddy(self.language_box)
        for code, native_name in LANGUAGES:
            self.language_box.addItem(native_name, code)
        if translations is not None:
            self.language_box.setCurrentIndex(
                max(0, self.language_box.findData(translations.language))
            )
        self.language_box.activated.connect(self._choose_language)
        language = QHBoxLayout()
        language.addWidget(self.language_label)
        language.addWidget(self.language_box, 1)
        self.language_label.setVisible(translations is not None)
        self.language_box.setVisible(translations is not None)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.usage_label = QLabel()
        self.usage_label.setWordWrap(True)
        self.this_device = QRadioButton()
        self.several_devices = QRadioButton()
        self.this_device.setChecked(True)
        self.usage = QButtonGroup(self)
        self.usage.addButton(self.this_device)
        self.usage.addButton(self.several_devices)
        self.start_at_login = QCheckBox()
        # Left for the user to choose where the login entry may get Stickle blocked.
        self.start_at_login.setChecked(not login_may_be_blocked)
        self.start_at_login.setVisible(offer_start_at_login)
        self.login_note = LoginNote(self)
        self.login_note.setVisible(offer_start_at_login and login_may_be_blocked)
        self.app_list = QCheckBox()
        self.app_list.setChecked(True)
        self.app_list.setVisible(offer_app_list)
        welcome = QWidget()
        welcome_layout = QVBoxLayout(welcome)
        welcome_layout.setContentsMargins(0, 0, 0, 0)
        welcome_layout.addLayout(language)
        welcome_layout.addSpacing(8)
        welcome_layout.addWidget(self.intro)
        welcome_layout.addSpacing(8)
        welcome_layout.addWidget(self.usage_label)
        welcome_layout.addWidget(self.this_device)
        welcome_layout.addWidget(self.several_devices)
        welcome_layout.addSpacing(8)
        welcome_layout.addWidget(self.start_at_login)
        welcome_layout.addWidget(self.login_note)
        welcome_layout.addWidget(self.app_list)
        welcome_layout.addStretch()

        # Page two: the recovery key, and below it a master password to add, in
        # case the place it is kept is forgotten (not instead of it).
        self.recovery = RecoveryKeyPanel(recovery_key)
        self.master_note = QLabel()
        self.master_note.setWordWrap(True)
        self.master_button = QPushButton()
        self.master_button.setAutoDefault(False)
        self.master_button.clicked.connect(self.set_master_password)
        for part in (self.master_note, self.master_button):
            part.setVisible(master is not None)
        master_row = QHBoxLayout()
        master_row.addWidget(self.master_button)
        master_row.addStretch()
        keeping = QWidget()
        keeping_layout = QVBoxLayout(keeping)
        keeping_layout.setContentsMargins(0, 0, 0, 0)
        keeping_layout.addWidget(self.recovery)
        keeping_layout.addSpacing(8)
        keeping_layout.addWidget(self.master_note)
        keeping_layout.addLayout(master_row)

        self.pages = QStackedWidget()
        self.pages.addWidget(welcome)
        self.pages.addWidget(keeping)

        self.buttons = QDialogButtonBox()
        self.next_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.next_button.clicked.connect(self._next)
        self.next_button.setDefault(True)
        self.done_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.later_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        self.done_button.hide()
        self.later_button.hide()
        self.done_button.setEnabled(False)
        self.recovery.kept.toggled.connect(self.done_button.setEnabled)
        # Kept: Enter is Done, wherever the desktop puts Done and Later.
        self.recovery.kept.toggled.connect(self.done_button.setDefault)
        self.done_button.clicked.connect(self.accept)
        self.later_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(520)
        self.went_on = False  # past page one: its choices stand
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle("Stickle")
        if self.pages.currentIndex() == 0:
            self.heading.setText(self.tr("Welcome to Stickle"))
        else:
            self.heading.setText(self.tr("Your recovery key"))
        self.language_label.setText(self.tr("&Language"))
        self.language_box.setAccessibleName(self.tr("Language"))
        self.language_box.setItemText(0, self.tr("System language"))
        self.intro.setText(
            self.tr(
                "Stickle keeps sticky notes on your desktop. They are saved as you type, "
                "encrypted on this computer, and nothing is sent anywhere."
            )
        )
        self.usage_label.setText(self.tr("Where will you use your notes?"))
        self.this_device.setText(self.tr("On this &computer only"))
        self.several_devices.setText(
            self.tr("On several &devices (syncing through your own cloud comes later)")
        )
        self.start_at_login.setText(self.tr("&Start Stickle when I log in"))
        self.app_list.setText(self.tr("Show Stickle in the &app list"))
        self.next_button.setText(self.tr("&Next"))
        self.done_button.setText(self.tr("Done"))
        self.later_button.setText(self.tr("Later"))
        self.master_note.setText(
            self.tr(
                "Keep the recovery key somewhere other than this computer. In case you"
                " forget where you put it, you can also set a master password to remember."
            )
        )
        master_set = self._master is not None and self._master.exists()
        self.master_button.setText(
            self.tr("Master password set") if master_set else self.tr("Set a &master password…")
        )
        self.master_button.setEnabled(not master_set)

    @override
    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _choose_language(self, index: int) -> None:
        if self._translations is not None:
            code = self.language_box.itemData(index)
            # Every window, this one too, switches at once; the choice is kept.
            self._translations.apply(code if isinstance(code, str) else None)

    def _next(self) -> None:
        self.went_on = True
        if not self.recovery.recovery_key:
            # It could not be made (logged); it is offered again later.
            self.reject()
            return
        self.pages.setCurrentIndex(1)
        self.next_button.hide()
        self.next_button.setDefault(False)
        self.done_button.show()
        self.later_button.setVisible(self._later_allowed)
        self.recovery.kept.setFocus()
        self.retranslate()

    @override
    def reject(self) -> None:
        # Esc and the close button are "Later" too: not where there is no later.
        if self.went_on and not self._later_allowed:
            return
        super().reject()

    def set_master_password(self) -> None:
        """Added beside the recovery key; the button then says it is set."""
        if self._master is not None:
            show_master_dialog(MasterPasswordDialog(self._master, self))
            self.retranslate()

    def choices(self) -> FirstRunChoices:
        """What was chosen; nothing at all if the window was closed on page one."""
        if not self.went_on:
            return FirstRunChoices(None, False, False, False)
        usage = SEVERAL_DEVICES if self.several_devices.isChecked() else THIS_DEVICE
        offer_login, offer_list = self._offers
        return FirstRunChoices(
            usage=usage,
            start_at_login=offer_login and self.start_at_login.isChecked(),
            app_list=offer_list and self.app_list.isChecked(),
            recovery_key_kept=self.result() == QDialog.DialogCode.Accepted,
        )


def welcome(
    notes: NoteRepository,
    settings: Settings,
    make_recovery_key: Callable[[], str],
    autostart: Autostart | None,
    app_list: AppMenuEntry | None,
    ask: Callable[[FirstRunDialog], object] = FirstRunDialog.exec,
    translations: Translations | None = None,
    login_may_be_blocked: bool | None = None,
    master: MasterPasswords | None = None,
    later_allowed: bool = True,
) -> FirstRunChoices:
    """The first start: the choices, carried out, then the sample note."""
    try:
        recovery_key = make_recovery_key()
    except OSError as error:
        log.error("no recovery key at first start: %s", type(error).__name__)
        recovery_key = ""
    if login_may_be_blocked is None:
        login_may_be_blocked = may_be_blocked()
    dialog = FirstRunDialog(
        recovery_key,
        autostart is not None,
        app_list is not None,
        translations,
        login_may_be_blocked=login_may_be_blocked,
        master=master,
        later_allowed=later_allowed,
    )
    ask(dialog)
    choices = dialog.choices()
    dialog.recovery.forget()  # the key shown, not kept once the window is done with
    # In the language chosen in the welcome, if one was.
    notes.create(sample_note())
    if choices.usage is not None:
        settings.set(USAGE, choices.usage)
    if autostart is not None and choices.start_at_login:
        switch_autostart(autostart, True)
    if app_list is not None and dialog.went_on:
        settings.set(APP_MENU_ASKED, True)  # asked here: not again separately
        if choices.app_list:
            switch_app_list(app_list, True)
    if choices.recovery_key_kept:
        settings.set(RECOVERY_KEY_KEPT, True)
    log.info(
        "first start: usage %s, start at login %s, recovery key %s",
        choices.usage,
        choices.start_at_login,
        "kept" if choices.recovery_key_kept else "not yet",
    )
    return choices
