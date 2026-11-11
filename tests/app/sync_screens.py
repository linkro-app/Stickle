"""Every window and state of sync there is, with made-up data: gone through by
test_sync_screens, and kept as pictures when it is asked to.

A scene builds its window in the state it shows and hands it back, not shown yet;
it reaches the state by the same methods the window's own flow calls."""
# pyright: reportPrivateUsage=false

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import cast

from PySide6.QtCore import QDate, QDateTime, QObject, QPoint, Qt, QTime, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QMenu, QSystemTrayIcon, QWidget

from stickle.app.app_icon import SyncSign, icon_pixmap
from stickle.app.background import Job
from stickle.app.conflict_bar import CopyOf
from stickle.app.i18n import Translations
from stickle.app.note_window import NoteWindow
from stickle.app.notes import NoteManager
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.settings_window import SettingsWindow
from stickle.app.stickle_window import StickleWindow
from stickle.app.sync_display import SyncDisplay
from stickle.app.sync_panel import ChangeName, DisconnectSync
from stickle.app.sync_setup import SyncSetup
from stickle.app.sync_status import WEBDAV_NAME, SyncStatus
from stickle.app.sync_stopped import SyncStopped
from stickle.app.syncing import BigChange, Ended, Syncing
from stickle.app.tray import Tray
from stickle.app.turn_on_sync import TurnOnSync
from stickle.crypto.recovery import RecoveryKeyTypoError
from stickle.data.colors import OwnColor
from stickle.data.sync_connection import WEBDAV, Login
from stickle.sync.devices import Other
from stickle.sync.engine import GETTING, PUTTING, FirstLook, Report
from stickle.sync.format import NewerFormatError
from stickle.sync.outcome import Outcome, Stopped
from stickle.sync.targets.base import CertificateError, NoAccessError, SignInNeededError
from stickle.sync.targets.webdav import NoWebDavFolderError
from stickle.sync.vault import NoWayInError

VAULT = "0a1b2c3d-1111-4222-8333-444455556677"
ADDRESS = "https://nas.example.com:5006/stickle"
USER = "stickle-user"
PASSWORD = "not a real password"
THIS_PC = "DESKTOP-7GQ2K1F"
A_CERTIFICATE = bytes(range(64))
A_RECOVERY_KEY = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q7"
_LONG_AGO = QDateTime(QDate(2024, 3, 14), QTime(9, 30))  # never today, nor this year
LONG_NAME = "거실에 있는 아주 오래된 데스크톱 컴퓨터의 이름을 끝까지 길게 적어 본 것입니다 끝"
LONG_ADDRESS = "https://a-rather-long-name-of-a-server.some-subdomain.example.com:5006/dav/stickle"
LONG_USER = "someone.with.a.long.name@some-subdomain.example.com"
_ICON_SIZES = (16, 20, 24, 32)
_ICON_SIGNS = (None, SyncSign.ON, SyncSign.OFFLINE, SyncSign.HALTED)


def _at_14_05() -> QDateTime:
    return QDateTime(QDate.currentDate(), QTime(14, 5))


def _others() -> list[Other]:
    now = _at_14_05().toMSecsSinceEpoch()
    return [
        Other("노트북", "Linux", "0.7.0", now),
        Other("회사 PC", "Windows", "0.7.0", _LONG_AGO.toMSecsSinceEpoch()),
        Other("Mac mini", "macOS", "0.7.1", None),
    ]


class FakeSyncing(QObject):
    """Sync, as far as the windows that show it can tell."""

    round_shown = Signal(object)
    moving = Signal(str, int, int)
    unsent_changed = Signal(object)
    cloud_name = WEBDAV_NAME

    def __init__(
        self,
        others: Sequence[Other] = (),
        to_put: int = 0,
        last: QDateTime | None = None,
        address: str = ADDRESS,
        user: str = USER,
        name: str = THIS_PC,
    ) -> None:
        super().__init__()
        self.login = Login(VAULT, WEBDAV, address, user, PASSWORD, {})
        moment = _at_14_05() if last is None else last
        self.status = SyncStatus(WEBDAV_NAME, None, lambda: to_put, lambda: moment, self)
        self._others = list(others)
        self._name = name

    def round_was(self, outcome: Outcome, left: Sequence[str] = ()) -> FakeSyncing:
        self.status.round_ended(Ended(Report(left=list(left)), outcome))
        return self

    def looks_every(self) -> int:
        return 300

    def set_looks_every(self, seconds: int) -> None:
        pass

    def others(self) -> list[Other]:
        return self._others

    def this_name(self) -> str:
        return self._name

    def chosen_name(self) -> str:
        return ""

    def system_name(self) -> str:
        return self._name

    def rename(self, name: str) -> None:
        pass

    def sync_now(self) -> None:
        pass

    def leaving(self) -> None:
        pass

    def leave(self) -> None:
        pass

    def big_change(self) -> BigChange:
        return A_BIG_CHANGE

    def take_big_change(self) -> None:
        pass

    def kept_login(self) -> Login:
        return self.login


class Stage:
    """What a scene needs, and what it made, to be put away when it is over."""

    def __init__(self, translations: Translations) -> None:
        self.translations = translations
        self._kept: list[object] = []
        self._at_end: list[Callable[[], None]] = []

    def keep[T](self, thing: T) -> T:
        self._kept.append(thing)
        return thing

    def at_end(self, step: Callable[[], None]) -> None:
        self._at_end.append(step)

    def end(self) -> None:
        for step in self._at_end:
            step()
        for thing in reversed(self._kept):
            if isinstance(thing, NoteWindow):
                thing.release()
            elif isinstance(thing, QWidget | QSystemTrayIcon):
                thing.hide()
                thing.deleteLater()
        self._kept.clear()


@dataclass(frozen=True)
class Scene:
    name: str
    build: Callable[[Stage], QWidget]
    # False: a window not gone through with Tab. A note's title bar is for the
    # mouse, what is on it being on its menu (F10) and its shortcuts as well.
    keyboard: bool = True


def _keys(there: bool = True, held: bool = True, old: bool = False) -> RecoveryKeys:
    return RecoveryKeys(
        exists=lambda: there,
        make=lambda: A_RECOVERY_KEY,
        kept=lambda: None,
        was_kept=lambda: held,
        is_old=lambda: old,
    )


def _setup(syncing: FakeSyncing | None, keys: RecoveryKeys | None = None) -> SyncSetup:
    def connect(_login: Login, _joining: bool) -> Syncing:
        raise AssertionError("a scene connects to nothing")

    return SyncSetup(
        lambda: cast("Syncing | None", syncing),
        _keys() if keys is None else keys,
        lambda: 12,
        lambda: {},
        connect,
        lambda: None,
    )


# The window that turns sync on


def _turn_on(stage: Stage, keys: RecoveryKeys | None = None) -> TurnOnSync:
    return stage.keep(TurnOnSync(_setup(None, keys)))


def _choosing(stage: Stage) -> TurnOnSync:
    window = _turn_on(stage)
    window.next_button.click()
    return window


def _server(
    stage: Stage, address: str = ADDRESS, user: str = USER, password: str = PASSWORD
) -> TurnOnSync:
    window = _choosing(stage)
    window.next_button.click()
    window.address.setText(address)
    window.user.setText(user)
    window.password.setText(password)
    return window


def _server_failing(error: Exception) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _server(stage)
        window._failed(error)
        return window

    return build


def _checking(stage: Stage) -> QWidget:
    window = _server(stage)
    window._job = cast("Job", object())  # as long as there is one, it is busy
    window._say(window.tr("Checking the server…"))
    window._update_buttons()

    def over() -> None:
        window._job = None

    stage.at_end(over)
    return window


def _unreadable_address(stage: Stage) -> QWidget:
    window = _server(stage, "ftp://nas.example.com")
    window._connect()
    return window


def _absent(keys: RecoveryKeys) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _turn_on(stage, keys)
        window._show_recovery_state()
        window._show(window.absent_page)
        return window

    return build


def _present(stage: Stage) -> TurnOnSync:
    window = _turn_on(stage)
    window._show(window.present_page)
    window.way_in.setText(A_RECOVERY_KEY)
    return window


def _present_by_password(stage: Stage) -> QWidget:
    window = _present(stage)
    window.by_password.setChecked(True)
    window.way_in.setText(PASSWORD)
    return window


def _present_failing(error: Exception) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _present(stage)
        window._failed(error)
        return window

    return build


A_LOOK = FirstLook(12, 340, 3, ["장보기: 우유, 빵 · Groceries", None, ""])
A_LONG_LOOK = FirstLook(1_000, 1_250, 0, [LONG_NAME * 3, LONG_ADDRESS * 2, "회의 10시"])


def _preview(look: FirstLook) -> Callable[[Stage], QWidget]:
    """What merging with a Stickle folder there before would make (a secret
    note and an empty one among the cloud's last three)."""

    def build(stage: Stage) -> QWidget:
        window = _turn_on(stage)
        window._preview = look
        window._show_look()
        window._show(window.preview_page)
        return window

    return build


def _downloading(stage: Stage) -> QWidget:
    window = _turn_on(stage)
    window._show(window.preview_page)
    window._job = cast("Job", object())  # as long as there is one, it is busy
    window._update_buttons()
    window._moved_on(GETTING, 340, 1_000)

    def over() -> None:
        window._job = None

    stage.at_end(over)
    return window


def _download_failed(stage: Stage) -> QWidget:
    window = _turn_on(stage)
    window._show(window.preview_page)
    window._first_looked(None)
    return window


def _confirm(stage: Stage) -> TurnOnSync:
    window = cast("TurnOnSync", _preview(A_LOOK)(stage))
    window.cloud_only_button.click()
    return window


def _confirm_failed(stage: Stage) -> QWidget:
    def no_backup(_login: Login) -> Syncing:
        raise OSError("the disk is full")

    window = stage.keep(TurnOnSync(replace(_setup(None), connect_cloud_only=no_backup)))
    window._opened_login = Login(VAULT, WEBDAV, ADDRESS, USER, PASSWORD, {1: bytes(32)})
    window._preview = A_LOOK
    window._show_look()
    window._show(window.confirm_page)
    window.next_button.click()
    return window


def _last(stage: Stage, joining: bool = False) -> TurnOnSync:
    window = _turn_on(stage)
    window._joining = joining
    window._set_line(window._going_on)
    window._show(window.last_page)
    return window


def _last_moving(doing: str, joining: bool) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _last(stage, joining)
        window._moving(doing, 340, 1000)
        return window

    return build


def _last_ended(outcome: Outcome, joining: bool = False) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _last(stage, joining)
        window._round_shown(Ended(Report(), outcome))
        return window

    return build


def _again(window: TurnOnSync) -> TurnOnSync:
    """As going on again from the server as kept (without reaching it)."""
    window._again = Login(VAULT, WEBDAV, ADDRESS, USER, PASSWORD, {})
    return window


def _again_absent(stage: Stage) -> QWidget:
    window = _again(_turn_on(stage))
    window._show_recovery_state()
    window._show(window.absent_page)
    return window


def _again_done(stage: Stage) -> QWidget:
    window = _again(_last(stage))
    window._round_shown(Ended(Report(), Outcome()))
    return window


def _again_password(stage: Stage) -> QWidget:
    """The server turned the password kept down: it is typed anew."""
    kept = Login(VAULT, WEBDAV, ADDRESS, USER, PASSWORD, {})
    return stage.keep(TurnOnSync(_setup(None), None, kept, True))


class _ManyColours(NoteManager):
    """More colours of one's own than are added here, from two computers."""

    def own_color_entries(self) -> list[OwnColor]:
        return [OwnColor(f"colour {n}", f"#{n * 17:02x}{200 - n * 9:02x}c0", n) for n in range(14)]


def _many_colours(stage: Stage) -> QWidget:
    window = stage.keep(SettingsWindow(stage.keep(_ManyColours()), stage.translations))
    window.tabs.setCurrentWidget(window.notes_page)
    window.refresh()
    return window


# The window that asks, while sync is stopped for it


A_BIG_CHANGE = BigChange(
    ["장보기: 우유, 빵 · Groceries", "", None, LONG_NAME * 2, *(f"메모 {n}" for n in range(8))],
    ["회의 10시 · Meeting at 10", "전화번호 정리"],
    ["여행 준비물"],
)


def _asking(why: Stopped) -> Callable[[Stage], SyncStopped]:
    def build(stage: Stage) -> SyncStopped:
        syncing = stage.keep(_synced().round_was(Outcome(why)))
        return stage.keep(SyncStopped(cast("Syncing", syncing), lambda: None, lambda: None))

    return build


def _asking_unfolded(stage: Stage) -> QWidget:
    window = _asking(Stopped.BIG_CHANGE)(stage)
    window.trash.toggle.click()
    return window


def _asking_busy(stage: Stage) -> QWidget:
    window = _asking(Stopped.BIG_CHANGE)(stage)
    window.first_button.click()
    window._taking = False  # not waited for in a scene
    return window


def _asking_after(ended: Ended) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        window = _asking(Stopped.BIG_CHANGE)(stage)
        window.first_button.click()
        window._round_shown(ended)
        return window

    return build


def _asking_disconnecting(stage: Stage) -> QWidget:
    window = _asking(Stopped.VAULT_GONE)(stage)
    window.leaving = True
    window._busy(True)
    window._say(lambda: window.tr("Disconnecting…"))

    def over() -> None:
        window.leaving = False

    stage.at_end(over)
    return window


# The settings' Connections page


def _page(stage: Stage, syncing: FakeSyncing | None) -> QWidget:
    if syncing is not None:
        stage.keep(syncing)
    window = stage.keep(
        SettingsWindow(NoteManager(), stage.translations, sync_setup=_setup(syncing))
    )
    window.tabs.setCurrentWidget(window.connections_page)
    window.refresh()
    return window


def _page_showing(make: Callable[[], FakeSyncing]) -> Callable[[Stage], QWidget]:
    return lambda stage: _page(stage, make())


def _synced() -> FakeSyncing:
    return FakeSyncing(_others())


def _moving(doing: str) -> Callable[[], FakeSyncing]:
    def make() -> FakeSyncing:
        syncing = _synced()
        syncing.status.moved(doing, 340, 1000)
        return syncing

    return make


def _offline(to_put: int) -> Callable[[], FakeSyncing]:
    def make() -> FakeSyncing:
        return FakeSyncing(_others(), to_put).round_was(
            Outcome(wait=15.0, failures=1, offline=True)
        )

    return make


def _trying_again() -> FakeSyncing:
    return FakeSyncing(_others(), 3).round_was(Outcome(wait=15.0, failures=1))


def _stopped(why: Stopped) -> Callable[[], FakeSyncing]:
    return lambda: _synced().round_was(Outcome(why))


def _full() -> FakeSyncing:
    return _synced().round_was(Outcome(full=True))


def _many_computers() -> FakeSyncing:
    now = _at_14_05().toMSecsSinceEpoch()
    more = [Other("태블릿", "Windows", "0.7.0", now), Other("서재 PC", "Linux", "0.7.0", now)]
    return FakeSyncing([*_others(), *more])


def _long_names() -> FakeSyncing:
    others = [Other(LONG_NAME, "Windows", "0.7.0", _at_14_05().toMSecsSinceEpoch())]
    return FakeSyncing(others, address=LONG_ADDRESS, user=LONG_USER, name=LONG_NAME)


def _long_ago() -> FakeSyncing:
    return FakeSyncing(_others(), last=_LONG_AGO)


def _change_name(stage: Stage) -> QWidget:
    return stage.keep(ChangeName("", THIS_PC))


def _disconnect(stage: Stage) -> DisconnectSync:
    syncing = stage.keep(_synced())
    return stage.keep(DisconnectSync(cast("Syncing", syncing), lambda: None))


def _disconnecting(stage: Stage) -> QWidget:
    window = _disconnect(stage)
    window.message.show()
    window.disconnect_button.setEnabled(False)
    window.cancel_button.setEnabled(False)
    return window


# Where it shows outside the settings


def _displayed(stage: Stage, syncing: FakeSyncing) -> tuple[Tray, StickleWindow, SyncDisplay]:
    manager = stage.keep(NoteManager())
    stage.keep(syncing)
    tray = stage.keep(Tray(manager.new_note, lambda: None, stage.translations, manager))
    window = stage.keep(StickleWindow(manager, stage.translations, lambda: None))
    display = stage.keep(SyncDisplay(tray, window, manager, stage.translations))
    display.follow(cast("Syncing", syncing))
    return tray, window, display


def shown_by(stage: Stage, syncing: FakeSyncing) -> tuple[Tray, StickleWindow]:
    tray, window, _display = _displayed(stage, syncing)
    return tray, window


def _manager(make: Callable[[], FakeSyncing]) -> Callable[[Stage], QWidget]:
    return lambda stage: shown_by(stage, make())[1]


def _manager_trashed(make: Callable[[], FakeSyncing]) -> Callable[[Stage], QWidget]:
    """Notes put in the trash here, deleted elsewhere while this computer was away."""

    def build(stage: Stage) -> QWidget:
        _tray, window, display = _displayed(stage, make())
        display._more_trashed(3)
        return window

    return build


def _menu(make: Callable[[], FakeSyncing]) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QMenu:
        menu = shown_by(stage, make())[0].contextMenu()
        assert menu is not None
        stage.at_end(menu.hide)
        return menu

    return build


def _tooltip(make: Callable[[], FakeSyncing]) -> Callable[[Stage], QWidget]:
    def build(stage: Stage) -> QWidget:
        label = stage.keep(QLabel(shown_by(stage, make())[0].toolTip()))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setContentsMargins(6, 4, 6, 4)
        return label

    return build


def _note(stage: Stage) -> QWidget:
    manager = stage.keep(NoteManager())
    note = stage.keep(manager.new_note())
    note.resize(260, 120)
    note.editor.setPlainText("장보기 · Groceries")
    note.set_unsent(True)
    return note


def _copy(copy: CopyOf, width: int = 260) -> Callable[[Stage], QWidget]:
    """A conflict copy, with its band."""

    def build(stage: Stage) -> QWidget:
        manager = stage.keep(NoteManager())
        note = stage.keep(manager.new_note())
        note.resize(width, 200)
        note.editor.setPlainText("회의 10시 · Meeting at 10")
        note.show_formatted()
        note.set_copy(copy)
        return note

    return build


def _written(here: bool = False, name: str | None = "노트북", original: bool = True) -> CopyOf:
    return CopyOf(here, name, _at_14_05().toMSecsSinceEpoch(), original, original)


def _original(copies: int) -> Callable[[Stage], QWidget]:
    """A note one of whose texts is in conflict copies: its dark cloud."""

    def build(stage: Stage) -> QWidget:
        note = _note(stage)
        assert isinstance(note, NoteWindow)
        note.set_unsent(False)
        note.set_copies(copies)
        return note

    return build


def _held(text: str, width: int = 260) -> Callable[[Stage], QWidget]:
    """A note being edited, what came for it from elsewhere waiting: its line."""

    def build(stage: Stage) -> QWidget:
        manager = stage.keep(NoteManager())
        note = stage.keep(manager.new_note())
        note.resize(width, 200)
        note.editor.setPlainText(text)
        note.edit()
        note.set_held(True)
        return note

    return build


def _icons(stage: Stage) -> QWidget:
    """The tray's icon as each taskbar would show it, enlarged: light, then dark."""
    zoom, pad = 3, 8
    cell = max(_ICON_SIZES) * zoom + pad
    side = len(_ICON_SIGNS) * cell + pad
    image = QImage(side * 2, len(_ICON_SIZES) * cell + pad, QImage.Format.Format_ARGB32)
    image.fill(QColor("#f3f3f3"))
    painter = QPainter(image)
    painter.fillRect(side, 0, side, image.height(), QColor("#202020"))
    for half in (0, 1):
        for across, sign in enumerate(_ICON_SIGNS):
            for down, size in enumerate(_ICON_SIZES):
                big = icon_pixmap(size, 1.0, sign).scaled(
                    size * zoom,
                    size * zoom,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
                place = QPoint(half * side + pad + across * cell, pad + down * cell)
                painter.drawPixmap(place, big)
    painter.end()
    label = stage.keep(QLabel())
    label.setPixmap(QPixmap.fromImage(image))
    return label


_REASONS = {
    Stopped.SIGN_IN: "sign-in",
    Stopped.NO_ACCESS: "no-access",
    Stopped.BIG_CHANGE: "big-change",
    Stopped.VAULT_GONE: "folder-gone",
    Stopped.VAULT_CHANGED: "folder-remade",
    Stopped.CERTIFICATE: "certificate",
    Stopped.NEWER: "newer",
}
# Those a window asks about, as the window shows them.
ASKING_ORDER = (Stopped.BIG_CHANGE, Stopped.VAULT_GONE, Stopped.VAULT_CHANGED, Stopped.CERTIFICATE)
# The others, told once all the same.
TOLD_ORDER = (Stopped.SIGN_IN, Stopped.NO_ACCESS, Stopped.NEWER)

SCENES: list[Scene] = [
    Scene("turn-on-1-intro", _turn_on),
    Scene("turn-on-2-choose", _choosing),
    Scene("turn-on-3-empty", lambda stage: _server(stage, "", "", "")),
    Scene("turn-on-3-filled", _server),
    Scene("turn-on-3-long-address", lambda stage: _server(stage, LONG_ADDRESS, LONG_USER)),
    Scene("turn-on-3-http-at-home", lambda stage: _server(stage, "http://192.168.0.10:5005/dav")),
    Scene("turn-on-3-http-outside", lambda stage: _server(stage, "http://nas.example.com/dav")),
    Scene("turn-on-3-checking", _checking),
    Scene("turn-on-3-unreadable-address", _unreadable_address),
    Scene("turn-on-3-wrong-password", _server_failing(SignInNeededError())),
    Scene("turn-on-3-no-access", _server_failing(NoAccessError())),
    Scene("turn-on-3-no-webdav-folder", _server_failing(NoWebDavFolderError())),
    Scene("turn-on-3-unreachable", _server_failing(OSError())),
    Scene("turn-on-3-certificate", _server_failing(CertificateError(A_CERTIFICATE))),
    Scene("turn-on-4-new-ready", _absent(_keys())),
    Scene("turn-on-4-new-no-key", _absent(_keys(there=False))),
    Scene("turn-on-4-new-not-kept", _absent(_keys(held=False))),
    Scene("turn-on-4-new-old-key", _absent(_keys(old=True))),
    Scene("turn-on-4-join-key", _present),
    Scene("turn-on-4-join-password", _present_by_password),
    Scene("turn-on-4-join-wrong", _present_failing(NoWayInError())),
    Scene("turn-on-4-join-typo", _present_failing(RecoveryKeyTypoError())),
    Scene("turn-on-4-join-newer", _present_failing(NewerFormatError())),
    Scene("turn-on-4-preview", _preview(A_LOOK)),
    Scene("turn-on-4-preview-long", _preview(A_LONG_LOOK)),
    Scene("turn-on-4-preview-downloading", _downloading),
    Scene("turn-on-4-preview-failed", _download_failed),
    Scene("turn-on-4-confirm", _confirm),
    Scene("turn-on-4-confirm-failed", _confirm_failed),
    Scene("turn-on-5-uploading", _last),
    Scene("turn-on-5-downloading", lambda stage: _last(stage, True)),
    Scene("turn-on-5-uploading-count", _last_moving(PUTTING, False)),
    Scene("turn-on-5-downloading-count", _last_moving(GETTING, True)),
    Scene("turn-on-5-done", _last_ended(Outcome())),
    Scene("turn-on-5-done-joined", _last_ended(Outcome(), True)),
    Scene("turn-on-5-stopped", _last_ended(Outcome(Stopped.NO_ACCESS))),
    Scene("turn-on-5-full", _last_ended(Outcome(full=True))),
    Scene("connections-off", lambda stage: _page(stage, None)),
    Scene("connections-synced", _page_showing(_synced)),
    Scene("connections-synced-long-ago", _page_showing(_long_ago)),
    Scene("connections-uploading", _page_showing(_moving(PUTTING))),
    Scene("connections-downloading", _page_showing(_moving(GETTING))),
    Scene("connections-offline", _page_showing(_offline(0))),
    Scene("connections-offline-to-upload", _page_showing(_offline(3))),
    Scene("connections-trying-again", _page_showing(_trying_again)),
    *(
        Scene(f"connections-stopped-{name}", _page_showing(_stopped(why)))
        for why, name in _REASONS.items()
    ),
    *(Scene(f"stopped-{_REASONS[why]}", _asking(why)) for why in ASKING_ORDER),
    Scene("stopped-big-change-unfolded", _asking_unfolded),
    Scene("stopped-big-change-backing-up", _asking_busy),
    Scene(
        "stopped-big-change-no-backup",
        _asking_after(Ended(Report(), Outcome(Stopped.BIG_CHANGE), None, True)),
    ),
    Scene("stopped-big-change-again", _asking_after(Ended(Report(), Outcome(Stopped.BIG_CHANGE)))),
    Scene("stopped-disconnecting", _asking_disconnecting),
    *(Scene(f"manager-told-{_REASONS[why]}", _manager(_stopped(why))) for why in ASKING_ORDER),
    *(Scene(f"manager-told-{_REASONS[why]}", _manager(_stopped(why))) for why in TOLD_ORDER),
    Scene("manager-told-full", _manager(_full)),
    Scene("manager-told-trash", _manager_trashed(_synced)),
    Scene("manager-told-trash-and-stopped", _manager_trashed(_stopped(Stopped.SIGN_IN))),
    Scene("turn-on-again-new-folder", _again_absent),
    Scene("turn-on-again-done", _again_done),
    Scene("turn-on-again-password", _again_password),
    Scene("settings-own-colours-many", _many_colours),
    Scene("connections-full", _page_showing(_full)),
    Scene("connections-many-computers", _page_showing(_many_computers)),
    Scene("connections-long-names", _page_showing(_long_names)),
    Scene("change-name", _change_name),
    Scene("disconnect", _disconnect),
    Scene("disconnecting", _disconnecting),
    Scene("manager-synced", _manager(_synced)),
    Scene("manager-offline", _manager(_offline(3))),
    Scene("manager-stopped", _manager(_stopped(Stopped.NO_ACCESS))),
    Scene("tray-menu-synced", _menu(_synced), keyboard=False),
    Scene("tray-menu-stopped", _menu(_stopped(Stopped.NO_ACCESS)), keyboard=False),
    Scene("tray-menu-update", _menu(_stopped(Stopped.NEWER)), keyboard=False),
    Scene("tray-menu-full", _menu(_full), keyboard=False),
    Scene("tray-tooltip-synced", _tooltip(_synced), keyboard=False),
    Scene("tray-tooltip-stopped", _tooltip(_stopped(Stopped.NO_ACCESS)), keyboard=False),
    Scene("tray-icons", _icons, keyboard=False),
    Scene("note-not-sent", _note, keyboard=False),
    Scene("note-conflict-copy", _copy(_written()), keyboard=False),
    Scene("note-conflict-copy-here", _copy(_written(here=True, name=None)), keyboard=False),
    Scene("note-conflict-copy-unknown", _copy(_written(name=None)), keyboard=False),
    Scene("note-conflict-copy-long-name", _copy(_written(name=LONG_NAME)), keyboard=False),
    Scene("note-conflict-copy-narrow", _copy(_written(), width=180), keyboard=False),
    Scene("note-conflict-copy-no-original", _copy(_written(original=False)), keyboard=False),
    Scene("note-conflict-original", _original(1), keyboard=False),
    Scene("note-conflict-original-two", _original(2), keyboard=False),
    Scene("note-held", _held("회의 10시 · Meeting at 10"), keyboard=False),
    Scene("note-held-narrow", _held("회의 10시", width=160), keyboard=False),
]
