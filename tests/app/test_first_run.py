"""The first start: a sample note, a few choices, and the recovery key."""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import apsw
import pytest
from pytestqt.qtbot import QtBot

from stickle.app.first_run import FirstRunDialog, sample_note, welcome
from stickle.app.i18n import Translations
from stickle.crypto.key_files import KeyFiles
from stickle.crypto.recovery import generate, read_recovery, unwrap, write_recovery
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import (
    APP_MENU_ASKED,
    RECOVERY_KEY_KEPT,
    SEVERAL_DEVICES,
    THIS_DEVICE,
    USAGE,
    Settings,
)
from stickle.platform.autostart import Autostart, Places
from stickle.platform.linux.appimage import AppMenuEntry
from stickle.unlock import Unlock

KEY = secrets.token_bytes(32)


class Place:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.connection: apsw.Connection = open_store(folder / "notes.db", KEY)
        self.notes = NoteRepository(self.connection)
        self.settings = Settings(self.connection)
        self.autostart = Autostart(
            Places(home=folder, config=folder / "config", appdata=folder), "linux", ["/x"]
        )
        self.app_list = AppMenuEntry(folder / "Stickle.AppImage", b"png", folder / "share")
        self.shown: list[str] = []

    def make_recovery_key(self) -> str:
        recovery_key = generate()
        write_recovery(KeyFiles(self.folder), KEY, recovery_key, "2026-10-26T00:00:00.000Z")
        return recovery_key

    def welcome(
        self,
        ask: Callable[[FirstRunDialog], object],
        offers: bool = True,
        login_may_be_blocked: bool = False,
    ) -> None:
        def record(dialog: FirstRunDialog) -> object:
            self.shown.append(dialog.recovery.recovery_key)
            return ask(dialog)

        welcome(
            self.notes,
            self.settings,
            self.make_recovery_key,
            self.autostart if offers else None,
            self.app_list if offers else None,
            record,
            login_may_be_blocked=login_may_be_blocked,
        )


@pytest.fixture
def place(qtbot: QtBot, tmp_path: Path) -> Iterator[Place]:
    place = Place(tmp_path)
    yield place
    place.connection.close()


def go_through(kept: bool, several: bool = False) -> Callable[[FirstRunDialog], object]:
    def answer(dialog: FirstRunDialog) -> object:
        if several:
            dialog.several_devices.setChecked(True)
        dialog.next_button.click()
        assert dialog.pages.currentIndex() == 1
        assert not dialog.done_button.isEnabled()  # not before it is kept
        if kept:
            dialog.recovery.kept.setChecked(True)
            # Enter is Done once it is kept, whatever order the desktop gives the buttons.
            assert dialog.done_button.isDefault() and not dialog.next_button.isDefault()
            dialog.done_button.click()
        else:
            dialog.later_button.click()
        return None

    return answer


def test_going_through_sets_everything_up(place: Place) -> None:
    place.welcome(go_through(kept=True))

    assert [note.body for note in place.notes.all()] == [sample_note()]
    assert place.settings.get(USAGE) == THIS_DEVICE
    assert place.autostart.enabled
    assert place.app_list.added
    assert place.settings.get(APP_MENU_ASKED)
    assert place.settings.get(RECOVERY_KEY_KEPT)
    # The key shown is the one that opens the notes.
    slot = read_recovery(KeyFiles(place.folder))
    assert slot is not None
    assert unwrap(slot, place.shown[0]) == KEY


def test_later_leaves_the_recovery_key_to_be_offered_again(place: Place) -> None:
    place.welcome(go_through(kept=False, several=True))

    assert place.settings.get(USAGE) == SEVERAL_DEVICES
    assert not place.settings.get(RECOVERY_KEY_KEPT)
    assert place.autostart.enabled


def test_closing_it_at_once_chooses_nothing_for_the_user(place: Place) -> None:
    place.welcome(lambda dialog: dialog.reject())

    assert len(place.notes.all()) == 1  # the sample note is there anyway
    assert place.settings.get(USAGE) is None
    assert not place.autostart.enabled
    assert not place.app_list.added
    assert not place.settings.get(APP_MENU_ASKED)
    assert not place.settings.get(RECOVERY_KEY_KEPT)


def test_unchecked_choices_are_respected(place: Place) -> None:
    def answer(dialog: FirstRunDialog) -> object:
        dialog.start_at_login.setChecked(False)
        dialog.app_list.setChecked(False)
        return go_through(kept=True)(dialog)

    place.welcome(answer)

    assert not place.autostart.enabled
    assert not place.app_list.added
    assert place.settings.get(APP_MENU_ASKED)  # asked, and the answer was no


def test_choices_that_do_not_apply_are_not_shown(place: Place) -> None:
    def answer(dialog: FirstRunDialog) -> object:
        assert dialog.start_at_login.isHidden()
        assert dialog.app_list.isHidden()
        return go_through(kept=True)(dialog)

    place.welcome(answer, offers=False)


def test_without_a_recovery_key_the_welcome_ends_after_the_first_page(
    qtbot: QtBot, tmp_path: Path
) -> None:
    place = Place(tmp_path)

    def fail() -> str:
        raise PermissionError("read-only")

    def answer(dialog: FirstRunDialog) -> object:
        dialog.next_button.click()
        assert dialog.result() == FirstRunDialog.DialogCode.Rejected
        return None

    welcome(
        place.notes, place.settings, fail, place.autostart, None, answer,
        login_may_be_blocked=False,
    )  # fmt: skip
    assert place.autostart.enabled
    assert not place.settings.get(RECOVERY_KEY_KEPT)
    place.connection.close()


def test_the_language_is_chosen_first_and_the_sample_note_follows(
    place: Place, translations: Translations
) -> None:
    # An English system for someone who reads Korean: chosen on the first page.
    seen: list[str] = []

    def answer(dialog: FirstRunDialog) -> object:
        assert dialog.language_box.isVisibleTo(dialog)
        assert dialog.language_box.currentData() == "en"
        korean = dialog.language_box.findData("ko")
        dialog.language_box.setCurrentIndex(korean)
        dialog.language_box.activated.emit(korean)
        seen.append(dialog.heading.text())
        return go_through(kept=True)(dialog)

    welcome(
        place.notes,
        place.settings,
        place.make_recovery_key,
        place.autostart,
        place.app_list,
        answer,
        translations=translations,
    )

    assert seen == ["Stickle에 오신 것을 환영합니다"]
    assert translations.language == "ko"
    assert [note.body for note in place.notes.all()] == [sample_note()]
    assert place.notes.all()[0].body.startswith("# Stickle에 오신 것을 환영합니다")


def test_without_translations_to_switch_no_language_is_offered(qtbot: QtBot) -> None:
    dialog = FirstRunDialog(generate(), True, True)
    qtbot.addWidget(dialog)

    assert not dialog.language_box.isVisibleTo(dialog)


def test_the_sample_note_speaks_the_interface_language(translations: Translations) -> None:
    translations.apply("ko")

    note = sample_note()

    assert note.startswith("# Stickle에 오신 것을 환영합니다")
    assert "- [ ] " in note
    assert note.endswith("[모든 기능 안내](https://stickle.linkro.co/ko/guide)")


def test_the_sample_note_leads_to_the_whole_guide(translations: Translations) -> None:
    translations.apply("en")

    note = sample_note()

    assert "**F1**" in note
    assert "Stickle Manager" in note
    assert note.endswith("[Everything Stickle does](https://stickle.linkro.co/en/guide)")


def test_first_start_is_only_the_start_that_made_the_notes(tmp_path: Path) -> None:
    unlock = Unlock(tmp_path, lambda: pytest.fail("no store needed"), (1, 8192))
    assert unlock.first_start
    open_store(tmp_path / "notes.db", KEY).close()

    assert not Unlock(tmp_path, lambda: pytest.fail("no store needed"), (1, 8192)).first_start


def test_everything_has_a_name_and_a_key(qtbot: QtBot) -> None:
    dialog = FirstRunDialog(generate(), True, True)
    qtbot.addWidget(dialog)

    for check in (dialog.this_device, dialog.several_devices, dialog.start_at_login):
        assert "&" in check.text()
    assert dialog.recovery.key.accessibleName()
    assert "&" in dialog.language_label.text()
    assert dialog.language_label.buddy() is dialog.language_box
    assert dialog.language_box.accessibleName()


def mnemonics(texts: list[str]) -> list[str]:
    """The letter after each "&" (an "&&" is a plain ampersand)."""
    keys: list[str] = []
    for text in texts:
        cleaned = text.replace("&&", "")
        if "&" in cleaned:
            keys.append(cleaned[cleaned.index("&") + 1].lower())
    return keys


@pytest.mark.parametrize("language", ["en", "ko"])
def test_each_choice_has_a_key_of_its_own(
    qtbot: QtBot, translations: Translations, language: str
) -> None:
    translations.apply(language)
    dialog = FirstRunDialog(generate(), True, True, translations=translations)
    qtbot.addWidget(dialog)

    page_one = [
        dialog.language_label.text(),
        dialog.this_device.text(),
        dialog.several_devices.text(),
        dialog.start_at_login.text(),
        dialog.app_list.text(),
        dialog.next_button.text(),
    ]
    keys = mnemonics(page_one)
    assert len(keys) == len(page_one)
    assert len(set(keys)) == len(keys), page_one


def test_where_starting_at_login_may_get_it_blocked_the_box_starts_unticked(
    qtbot: QtBot,
) -> None:
    blocked = FirstRunDialog(generate(), True, True, login_may_be_blocked=True)
    usual = FirstRunDialog(generate(), True, True, login_may_be_blocked=False)
    for dialog in (blocked, usual):
        qtbot.addWidget(dialog)
        dialog.show()

    assert not blocked.start_at_login.isChecked()
    assert blocked.login_note.isVisible() and "block" in blocked.login_note.text()
    assert usual.start_at_login.isChecked()
    assert not usual.login_note.isVisible()


def test_left_unticked_nothing_is_added_to_the_startup_folder(place: Place) -> None:
    place.welcome(go_through(kept=True), login_may_be_blocked=True)

    assert not place.autostart.enabled
