"""The secret notes' password is refused when guessable, as the spare password is,
and the window says what it must be before one is refused."""

import secrets
from pathlib import Path

from pytestqt.qtbot import QtBot
from test_startup_dialogs import FAST, Backend, store_of

from stickle.app import application
from stickle.app.i18n import Translations
from stickle.app.secret_dialogs import NewSecretPasswordDialog, SecretPasswords
from stickle.unlock import Unlock

KEY = secrets.token_bytes(32)


def passwords(folder: Path) -> SecretPasswords:
    folder.mkdir()
    unlock = Unlock(folder, store_of(Backend()), FAST, master_strength=FAST)
    unlock.opened_key = KEY
    found = application.secret_passwords(unlock, application.master_passwords(unlock))
    assert found is not None
    return found


def refusal(qtbot: QtBot, folder: Path, typed: str) -> NewSecretPasswordDialog:
    dialog = NewSecretPasswordDialog(passwords(folder), KEY)
    qtbot.addWidget(dialog)
    dialog.password.setText(typed)
    dialog.confirm.setText(typed)
    dialog.accept()
    assert dialog.result() != NewSecretPasswordDialog.DialogCode.Accepted.value
    return dialog


def test_a_common_one_is_refused_as_the_rules_say(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = refusal(qtbot, tmp_path / "data", "qwer1234!")

    assert dialog.rules.text() == "At least 8 characters, and not a common password."
    assert dialog.message.text() == "That is one of the most common passwords. Choose another."


def test_the_rules_and_why_read_in_korean(
    qtbot: QtBot, tmp_path: Path, translations: Translations
) -> None:
    translations.apply("ko")
    dialog = refusal(qtbot, tmp_path / "data", "abababab")

    assert dialog.rules.text() == "8자 이상이고, 흔한 암호가 아니어야 합니다."
    assert dialog.message.text().startswith("같은 글자를 되풀이하거나 자판을 차례로")
