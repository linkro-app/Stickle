"""The Connections page and the window that leaves, with OneDrive: the account,
where the app connects, what Microsoft sees, and where its access is taken away."""

from typing import cast

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from pytestqt.qtbot import QtBot
from sync_screens import FakeSyncing

from stickle.app.sync_panel import MICROSOFT_APP_ACCESS, DisconnectSync, SyncPanel
from stickle.app.syncing import Syncing
from stickle.core.updates import FEED_HOST
from stickle.data.sync_connection import ONEDRIVE
from stickle.sync.outcome import Outcome, Stopped
from stickle.sync.targets.onedrive import CONTENT_HOSTS, MICROSOFT

ACCOUNT = "someone@outlook.com"
SEES = (
    "Microsoft shows Stickle's developer totals, such as how many people sync with Stickle"
    " and how many sync requests it makes. It doesn't show who you are or anything in your"
    " notes."
)
BLOCKS = (
    "If Microsoft blocks Stickle, syncing with OneDrive stops. Your notes stay on this"
    " computer, and you can switch to another cloud."
)


def onedrive(user: str = ACCOUNT) -> FakeSyncing:
    return FakeSyncing(kind=ONEDRIVE, user=user)


def panel_of(qtbot: QtBot, syncing: FakeSyncing) -> SyncPanel:
    panel = SyncPanel(cast(Syncing, syncing), lambda _left: None)
    qtbot.addWidget(panel)
    return panel


def test_the_account_line_names_the_personal_account(qtbot: QtBot) -> None:
    assert panel_of(qtbot, onedrive()).account.text() == f"OneDrive (personal account) · {ACCOUNT}"


def test_an_account_whose_name_was_not_told_is_named_by_its_kind(qtbot: QtBot) -> None:
    assert panel_of(qtbot, onedrive(user="")).account.text() == "OneDrive (personal account)"


def test_it_lists_microsofts_addresses_and_what_microsoft_sees(qtbot: QtBot) -> None:
    lines = panel_of(qtbot, onedrive()).connects_to(update_check=True).split("\n")
    assert lines == [
        "Stickle connects to:",
        "· graph.microsoft.com, login.microsoftonline.com, *.1drv.com,"
        " *.microsoftpersonalcontent.com, api.onedrive.com — to sync with OneDrive and sign in",
        f"· {FEED_HOST} — a daily check for a new version (you can turn it off in General)",
        "",
        SEES,
        BLOCKS,
    ]


def test_the_addresses_listed_are_those_it_may_reach() -> None:
    # The list is made from what the target holds to: nothing reached is left out.
    assert MICROSOFT.hosts[:2] == [MICROSOFT.graph, MICROSOFT.login]
    assert [host.lstrip("*") for host in MICROSOFT.hosts[2:]] == list(CONTENT_HOSTS)


def test_without_the_update_check_microsoft_is_still_said(qtbot: QtBot) -> None:
    lines = panel_of(qtbot, onedrive()).connects_to(update_check=False).split("\n")
    assert len(lines) == 5 and lines[-2:] == [SEES, BLOCKS]


def test_a_server_of_ones_own_says_nothing_of_microsoft(qtbot: QtBot) -> None:
    assert "Microsoft" not in panel_of(qtbot, FakeSyncing()).connects_to(update_check=True)


def test_signed_out_its_button_leads_to_signing_in_again(qtbot: QtBot) -> None:
    syncing = onedrive()
    panel = panel_of(qtbot, syncing)
    syncing.round_was(Outcome(Stopped.SIGN_IN))
    assert panel.status.text() == "Sync stopped — sign in to OneDrive again"
    assert panel.review_button.isVisibleTo(panel)
    assert panel.review_button.text() == "Sign &in again…"


def test_blocked_it_says_microsoft_and_has_sync_now_beside_it(qtbot: QtBot) -> None:
    syncing = onedrive()
    panel = panel_of(qtbot, syncing)
    syncing.round_was(Outcome(Stopped.NO_ACCESS))
    assert panel.status.text() == "Sync stopped — Microsoft has blocked Stickle"
    assert not panel.review_button.isVisibleTo(panel)


def test_leaving_onedrive_tells_where_microsoft_takes_the_access_away(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = DisconnectSync(cast(Syncing, onedrive()), lambda _left: None)
    qtbot.addWidget(dialog)
    assert dialog.folder_stays.text() == (
        "The Stickle folder in OneDrive isn't deleted, so your other computers keep syncing."
    )
    assert not dialog.password_goes.isVisibleTo(dialog)
    assert dialog.access_stays.isVisibleTo(dialog)
    assert dialog.access_stays.text() == (
        "To remove Stickle's access from your Microsoft account too, remove it under"
        " 'Apps and services you've given access'."
    )
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", opened.append)
    dialog.access_button.click()
    assert [url.toString() for url in opened] == [MICROSOFT_APP_ACCESS]
    assert dialog.cancel_button.isDefault()  # nothing happens by a press of Enter


def test_leaving_a_server_of_ones_own_says_its_password_goes(qtbot: QtBot) -> None:
    dialog = DisconnectSync(cast(Syncing, FakeSyncing()), lambda _left: None)
    qtbot.addWidget(dialog)
    assert dialog.password_goes.isVisibleTo(dialog)
    assert not dialog.access_stays.isVisibleTo(dialog)
    assert not dialog.access_button.isVisibleTo(dialog)
