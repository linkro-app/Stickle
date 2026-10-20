from pathlib import Path

import pytest
from PySide6.QtCore import QLocale
from pytestqt.qtbot import QtBot

from stickle.app.application import NoteManager, use_language
from stickle.app.i18n import Translations
from stickle.app.password_dialog import PasswordDialog
from stickle.app.tray import Tray
from stickle.data.startup import StartupSettings


def test_switching_language_updates_open_windows_and_the_tray(
    translations: Translations,
) -> None:
    manager = NoteManager()
    tray = Tray(manager.new_note, lambda: None, translations)
    note = manager.new_note()
    try:
        assert note.title_bar.close_button.toolTip() == "Hide note"
        assert tray.new_note_action.text() == "&New note"

        translations.apply("ko")

        assert note.title_bar.close_button.toolTip() == "메모 숨기기"
        assert note.editor.accessibleName() == "메모 내용"
        assert tray.new_note_action.text() == "새 메모(&N)"
        assert tray.language_menu.title() == "언어(&L)"
        # A note opened after the switch starts in the new language too.
        assert manager.new_note().title_bar.close_button.toolTip() == "메모 숨기기"

        translations.apply("en")

        assert note.title_bar.close_button.toolTip() == "Hide note"
        assert tray.quit_action.text() == "&Quit Stickle"
    finally:
        for window in manager.windows:
            window.close()


def test_language_names_stay_in_their_own_language(translations: Translations) -> None:
    tray = Tray(lambda: None, lambda: None, translations)

    translations.apply("ko")

    assert tray.language_actions["en"].text() == "English"
    assert tray.language_actions["ko"].text() == "한국어"


def test_the_language_chosen_before_applies_from_the_start(
    qtbot: QtBot, translations_dir: Path, tmp_path: Path
) -> None:
    StartupSettings(tmp_path).set_language("ko")
    translations = Translations(translations_dir)

    use_language(translations, StartupSettings(tmp_path))

    # Before any note (and before the database): the password prompt is Korean too.
    dialog = PasswordDialog(create=False, submit=lambda _password: None)
    qtbot.addWidget(dialog)
    assert translations.language == "ko"
    assert dialog.windowTitle() != "Unlock your notes"
    translations.apply("en")
    QLocale.setDefault(QLocale.system())


def test_a_language_chosen_in_the_tray_is_kept_for_next_time(
    qtbot: QtBot, translations_dir: Path, tmp_path: Path
) -> None:
    translations = Translations(translations_dir)
    use_language(translations, StartupSettings(tmp_path))
    tray = Tray(lambda: None, lambda: None, translations)

    tray.language_actions["ko"].trigger()
    assert StartupSettings(tmp_path).language == "ko"

    tray.language_actions[None].trigger()
    assert StartupSettings(tmp_path).language is None
    translations.apply("en")
    QLocale.setDefault(QLocale.system())


def test_a_choice_that_cannot_be_kept_still_applies(
    qtbot: QtBot, translations_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    translations = Translations(translations_dir)
    use_language(translations, StartupSettings(tmp_path))

    def fail(self: StartupSettings, language: str | None) -> None:
        raise PermissionError("read-only folder")

    monkeypatch.setattr(StartupSettings, "set_language", fail)
    translations.apply("ko")

    assert translations.language == "ko"
    translations.apply("en")
    QLocale.setDefault(QLocale.system())
