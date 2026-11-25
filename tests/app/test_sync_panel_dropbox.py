"""The Connections page and the window that leaves, with Dropbox: the account,
no interval to choose, where the app connects, what Dropbox sees, and the
sign-out as sync is left."""

from typing import cast

from pytestqt.qtbot import QtBot
from sync_screens import FakeSyncing

from stickle.app.sync_panel import DisconnectSync, SyncPanel
from stickle.app.syncing import Syncing
from stickle.core.updates import FEED_HOST
from stickle.data.sync_connection import DROPBOX
from stickle.sync.outcome import Outcome, Stopped
from stickle.sync.targets.dropbox import DROPBOX as DROPBOX_PLACE

ACCOUNT = "someone@example.com"
SEES = (
    "Dropbox shows Stickle's developer totals, such as how many people sync with Stickle"
    " and how many sync requests it makes. It doesn't show who you are or anything in your"
    " notes."
)
BLOCKS = (
    "If Dropbox blocks Stickle, syncing with Dropbox stops. Your notes stay on this"
    " computer, and you can switch to another cloud."
)


def dropbox(user: str = ACCOUNT) -> FakeSyncing:
    return FakeSyncing(kind=DROPBOX, user=user)


def panel_of(qtbot: QtBot, syncing: FakeSyncing) -> SyncPanel:
    panel = SyncPanel(cast(Syncing, syncing), lambda: None)
    qtbot.addWidget(panel)
    return panel


def test_the_account_line_names_the_account(qtbot: QtBot) -> None:
    assert panel_of(qtbot, dropbox()).account.text() == f"Dropbox · {ACCOUNT}"


def test_an_account_whose_name_was_not_told_is_named_by_its_cloud(qtbot: QtBot) -> None:
    assert panel_of(qtbot, dropbox(user="")).account.text() == "Dropbox"


def test_there_is_no_interval_to_choose(qtbot: QtBot) -> None:
    panel = panel_of(qtbot, dropbox())
    for part in (panel.every_label, panel.every_box, panel.every_note):
        assert not part.isVisibleTo(panel)
    assert panel.every_box not in panel.tab_order()


def test_a_server_of_ones_own_has_its_interval(qtbot: QtBot) -> None:
    panel = panel_of(qtbot, FakeSyncing())
    assert panel.every_box.isVisibleTo(panel) and panel.every_box in panel.tab_order()


def test_it_lists_dropboxs_addresses_and_what_dropbox_sees(qtbot: QtBot) -> None:
    lines = panel_of(qtbot, dropbox()).connects_to(update_check=True).split("\n")
    assert lines == [
        "Stickle connects to:",
        "· api.dropboxapi.com, content.dropboxapi.com, notify.dropboxapi.com"
        " — to sync with Dropbox and sign in",
        f"· {FEED_HOST} — a daily check for a new version (you can turn it off in General)",
        "",
        SEES,
        BLOCKS,
    ]


def test_the_addresses_listed_are_those_it_reaches() -> None:
    # Not the sign-in page: the browser opens it, and the app sends it nothing.
    place = DROPBOX_PLACE
    assert place.hosts == [place.api, place.content, place.notify]
    assert place.www not in place.hosts


def test_signed_out_its_button_leads_to_signing_in_again(qtbot: QtBot) -> None:
    syncing = dropbox()
    panel = panel_of(qtbot, syncing)
    syncing.round_was(Outcome(Stopped.SIGN_IN))
    assert panel.status.text() == "Sync stopped — sign in to Dropbox again"
    assert panel.review_button.isVisibleTo(panel)
    assert panel.review_button.text() == "Sign &in again…"


def test_blocked_it_says_dropbox_and_has_sync_now_beside_it(qtbot: QtBot) -> None:
    syncing = dropbox()
    panel = panel_of(qtbot, syncing)
    syncing.round_was(Outcome(Stopped.NO_ACCESS))
    assert panel.status.text() == "Sync stopped — Dropbox has blocked Stickle"
    assert not panel.review_button.isVisibleTo(panel)


def test_leaving_dropbox_says_stickle_is_signed_out(qtbot: QtBot) -> None:
    dialog = DisconnectSync(cast(Syncing, dropbox()), lambda: None)
    qtbot.addWidget(dialog)
    assert dialog.folder_stays.text() == (
        "The Stickle folder in Dropbox isn't deleted, so your other computers keep syncing."
    )
    assert dialog.signs_out.isVisibleTo(dialog)
    assert dialog.signs_out.text() == "This signs Stickle out of Dropbox."
    for part in (dialog.password_goes, dialog.access_stays, dialog.access_button):
        assert not part.isVisibleTo(dialog)
    assert dialog.cancel_button.isDefault()  # nothing happens by a press of Enter


def test_leaving_another_cloud_says_nothing_of_signing_out(qtbot: QtBot) -> None:
    dialog = DisconnectSync(cast(Syncing, FakeSyncing()), lambda: None)
    qtbot.addWidget(dialog)
    assert not dialog.signs_out.isVisibleTo(dialog)
