"""Support from the Store copy: an amount chosen, the Store's dialog, the answer."""

import enum

import pytest
from pytestqt.qtbot import QtBot

from stickle.app.i18n import Translations
from stickle.app.support_dialog import SUPPORT_ITEMS, SupportDialog


class Outcome(enum.Enum):  # as the Store answers (stickle.platform.windows.store_purchase)
    SUCCEEDED = 0
    ALREADY_PURCHASED = 1
    NOT_PURCHASED = 2
    NETWORK_ERROR = 3
    SERVER_ERROR = 4
    FAILED = -1


class FakeStore:
    """The Store's purchase dialog: opened for an item, answered when told to."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.asked: list[tuple[int, str]] = []
        self.answer: Outcome | None = None

    def __call__(self, window: int, store_id: str) -> FakeStore:
        if self.fails:
            raise OSError("no Store")
        self.asked.append((window, store_id))
        self.answer = None
        return self

    def outcome(self) -> Outcome | None:
        return self.answer

    def close(self) -> None:
        pass


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def websites() -> list[None]:
    return []


@pytest.fixture
def dialog(
    qtbot: QtBot, translations: Translations, store: FakeStore, websites: list[None]
) -> SupportDialog:
    dialog = SupportDialog(store, lambda: websites.append(None))
    qtbot.addWidget(dialog)
    dialog.present()
    return dialog


def answer(qtbot: QtBot, dialog: SupportDialog, store: FakeStore, outcome: Outcome) -> None:
    store.answer = outcome
    qtbot.waitUntil(lambda: all(button.isEnabled() for button in dialog.size_buttons))


def test_it_offers_three_amounts_and_starts_on_the_middle_one(dialog: SupportDialog) -> None:
    assert [b.text() for b in dialog.size_buttons] == ["&Small", "&Medium", "&Large"]
    assert dialog.focusWidget() is dialog.size_buttons[1]
    # Says it adds nothing, and leaves the price to the Store (each in its currency).
    assert "adds nothing" in dialog.text.text()
    assert "₩" not in dialog.text.text()


def test_each_amount_opens_the_stores_dialog_for_its_item_over_this_window(
    qtbot: QtBot, dialog: SupportDialog, store: FakeStore
) -> None:
    for button, store_id in zip(dialog.size_buttons, SUPPORT_ITEMS, strict=True):
        button.click()
        assert store.asked[-1] == (int(dialog.winId()), store_id)
        # One at a time: the amounts wait while the Store's dialog is open.
        assert not any(b.isEnabled() for b in dialog.size_buttons)
        answer(qtbot, dialog, store, Outcome.NOT_PURCHASED)
    assert len(store.asked) == 3


def test_a_payment_is_thanked_and_nothing_else_changes(
    qtbot: QtBot, dialog: SupportDialog, store: FakeStore
) -> None:
    dialog.size_buttons[1].click()
    answer(qtbot, dialog, store, Outcome.SUCCEEDED)

    assert dialog.status.isVisible()
    assert "Thank you" in dialog.status.text()
    assert not dialog.website_button.isVisible()

    # Opened again, it is as before: nothing about the payment was kept.
    dialog.close()
    dialog.present()
    assert not dialog.status.isVisible()


def test_closing_the_stores_dialog_says_nothing(
    qtbot: QtBot, dialog: SupportDialog, store: FakeStore
) -> None:
    dialog.size_buttons[0].click()
    answer(qtbot, dialog, store, Outcome.NOT_PURCHASED)

    assert not dialog.status.isVisible()
    assert not dialog.website_button.isVisible()


@pytest.mark.parametrize("outcome", [Outcome.NETWORK_ERROR, Outcome.SERVER_ERROR, Outcome.FAILED])
def test_when_the_store_fails_the_website_is_offered(
    qtbot: QtBot, dialog: SupportDialog, store: FakeStore, websites: list[None], outcome: Outcome
) -> None:
    dialog.size_buttons[2].click()
    answer(qtbot, dialog, store, outcome)

    assert dialog.status.isVisible()
    assert dialog.website_button.isVisible()
    dialog.website_button.click()
    assert websites == [None]


def test_when_the_stores_dialog_cannot_open_the_website_is_offered(
    qtbot: QtBot, translations: Translations, websites: list[None]
) -> None:
    dialog = SupportDialog(FakeStore(fails=True), lambda: websites.append(None))
    qtbot.addWidget(dialog)
    dialog.present()

    dialog.size_buttons[1].click()

    assert dialog.website_button.isVisible()
    assert all(b.isEnabled() for b in dialog.size_buttons)


def test_it_speaks_korean(dialog: SupportDialog, translations: Translations) -> None:
    translations.apply("ko")
    assert dialog.windowTitle() == "Stickle 후원"
    assert [b.text() for b in dialog.size_buttons] == ["작게(&S)", "보통(&M)", "크게(&L)"]
