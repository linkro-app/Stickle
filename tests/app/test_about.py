"""About Stickle, and where "Support Stickle" leads."""

import pytest
from PySide6.QtCore import Qt, QUrl
from pytestqt.qtbot import QtBot

from stickle import __version__
from stickle.app import note_window, support
from stickle.app.about_dialog import AboutDialog
from stickle.app.i18n import Translations
from stickle.app.tray import Tray


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the app asked the system to open, instead of opening it."""
    urls: list[str] = []

    def open_url(url: QUrl) -> bool:
        urls.append(url.toString())
        return True

    monkeypatch.setattr(note_window, "open_url", open_url)
    return urls


def test_it_shows_the_running_version(qtbot: QtBot, translations: Translations) -> None:
    about = AboutDialog()
    qtbot.addWidget(about)
    assert __version__ in about.version.text()


def test_support_opens_the_websites_page_in_the_apps_language(
    qtbot: QtBot, translations: Translations, opened: list[str]
) -> None:
    about = AboutDialog()
    qtbot.addWidget(about)
    about.support_button.click()
    translations.apply("ko")
    about.support_button.click()
    assert opened == [
        "https://stickle.linkro.co/en/support",
        "https://stickle.linkro.co/ko/support",
    ]


def test_a_language_the_website_lacks_gets_english(
    qtbot: QtBot, translations: Translations, opened: list[str]
) -> None:
    translations.apply("fr")
    support.open_support()
    assert opened == ["https://stickle.linkro.co/en/support"]


def test_the_store_copy_chooses_an_amount_for_the_store(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, translations: Translations, opened: list[str]
) -> None:
    monkeypatch.setattr(support, "in_store_package", lambda: True)

    def never_called(window: int, store_id: str) -> None:
        raise AssertionError("no amount was chosen")

    monkeypatch.setattr(support, "store_purchaser", lambda: never_called)
    monkeypatch.setattr(support, "_dialog", [])
    support.open_support()
    support.open_support()  # the same window again, not a second one

    assert len(support._dialog) == 1  # pyright: ignore[reportPrivateUsage]
    dialog = support._dialog[0]  # pyright: ignore[reportPrivateUsage]
    qtbot.addWidget(dialog)
    assert dialog.isVisible()
    assert opened == []


def test_other_copies_never_open_the_store(
    monkeypatch: pytest.MonkeyPatch, translations: Translations, opened: list[str]
) -> None:
    monkeypatch.setattr(support, "in_store_package", lambda: False)
    monkeypatch.setattr(support, "_dialog", [])
    support.open_support()
    assert opened == ["https://stickle.linkro.co/en/support"]
    assert support._dialog == []  # pyright: ignore[reportPrivateUsage]


def test_the_tray_offers_support_and_about(
    qtbot: QtBot, translations: Translations, opened: list[str]
) -> None:
    tray = Tray(lambda: None, lambda: None, translations)
    actions = [a.text() for a in tray.contextMenu().actions() if a.text()]
    # Just above Quit, the last item.
    assert actions[-3:] == ["Su&pport Stickle…", "Abo&ut Stickle", "&Quit Stickle"]
    tray.support_action.trigger()
    assert opened == ["https://stickle.linkro.co/en/support"]


def test_links_open_only_web_pages(
    qtbot: QtBot, translations: Translations, opened: list[str]
) -> None:
    about = AboutDialog()
    qtbot.addWidget(about)
    about.links.linkActivated.emit("https://github.com/linkro-app/Stickle")
    about.links.linkActivated.emit("file:///C:/Windows/System32/calc.exe")
    assert opened == ["https://github.com/linkro-app/Stickle"]


def test_the_keyboard_starts_on_close_and_reaches_support(
    qtbot: QtBot, translations: Translations
) -> None:
    about = AboutDialog()
    qtbot.addWidget(about)
    about.present()
    qtbot.waitExposed(about)
    assert about.focusWidget() is about.close_button
    assert about.support_button.focusPolicy() & Qt.FocusPolicy.TabFocus
