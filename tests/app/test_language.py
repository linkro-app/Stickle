from pathlib import Path

import pytest
from PySide6.QtCore import QLocale
from pytestqt.qtbot import QtBot

from stickle.app.application import NoteManager, use_language
from stickle.app.i18n import Translations
from stickle.app.password_dialog import PasswordDialog
from stickle.app.settings_window import SettingsWindow
from stickle.app.tray import Tray
from stickle.data.startup import StartupSettings


def test_switching_language_updates_open_windows_and_the_tray(
    translations: Translations,
) -> None:
    manager = NoteManager()
    tray = Tray(manager.new_note, lambda: None, translations)
    settings = SettingsWindow(manager, translations)
    note = manager.new_note()
    try:
        assert note.title_bar.close_button.toolTip() == "Hide note"
        assert tray.new_note_action.text() == "&New note"

        translations.apply("ko")

        assert note.title_bar.close_button.toolTip() == "메모 숨기기"
        assert note.editor.accessibleName() == "메모 내용"
        assert tray.new_note_action.text() == "새 메모(&N)"
        assert tray.settings_action.text() == "설정(&S)…"
        assert settings.language_label.text() == "언어(&L)"
        assert settings.windowTitle() == "Stickle 설정"
        # A note opened after the switch starts in the new language too.
        assert manager.new_note().title_bar.close_button.toolTip() == "메모 숨기기"

        translations.apply("en")

        assert note.title_bar.close_button.toolTip() == "Hide note"
        assert tray.quit_action.text() == "&Quit Stickle"
    finally:
        for window in manager.windows:
            window.close()
        settings.deleteLater()


def test_language_names_stay_in_their_own_language(
    qtbot: QtBot, translations: Translations
) -> None:
    settings = SettingsWindow(NoteManager(), translations)
    qtbot.addWidget(settings)
    box = settings.language_box

    translations.apply("ko")

    assert box.itemText(box.findData("en")) == "English"
    assert box.itemText(box.findData("ko")) == "한국어"
    assert box.itemText(0) == "시스템 설정 따르기"


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


def test_a_language_chosen_in_the_settings_is_kept_for_next_time(
    qtbot: QtBot, translations_dir: Path, tmp_path: Path
) -> None:
    translations = Translations(translations_dir)
    use_language(translations, StartupSettings(tmp_path))
    settings = SettingsWindow(NoteManager(), translations)
    qtbot.addWidget(settings)
    box = settings.language_box

    box.activated.emit(box.findData("ko"))
    assert StartupSettings(tmp_path).language == "ko"
    assert box.currentData() == "ko"

    box.activated.emit(0)  # the system's
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
