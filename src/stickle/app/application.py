"""Application start-up."""

import functools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import apsw
from PySide6.QtCore import (
    QLocale,
    QMessageLogContext,
    QTimer,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from stickle import __version__
from stickle.app.app_list import icon_png, offer_app_list
from stickle.app.backups import BackupAccess, DailyBackups
from stickle.app.data_menu import DataAccess
from stickle.app.first_run import welcome
from stickle.app.fonts import ensure_korean_font
from stickle.app.i18n import Translations
from stickle.app.import_dialog import ImportAccess
from stickle.app.instance_server import InstanceServer
from stickle.app.master_password import MasterPasswords
from stickle.app.notes import NoteManager
from stickle.app.perf import SAMPLE_NOTE, PerfMode, open_storage_like_startup
from stickle.app.placement import monitors
from stickle.app.protection_changes import ProtectionChanges
from stickle.app.recovery_key_dialog import RecoveryKeys
from stickle.app.recovery_offer import RecoveryOffer
from stickle.app.secret_dialogs import SecretPasswords
from stickle.app.shortcuts import GlobalShortcuts, no_portal
from stickle.app.signals import SignalWatcher
from stickle.app.startup import open_notes
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.app.update_check import UpdateCheck
from stickle.app.whats_new import give_news
from stickle.core.clock import Clock, utc_now
from stickle.core.layout import Place, Rect, place_of
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.recovery import WrongRecoveryKeyError
from stickle.crypto.secret_notes import NotKeptForRecoveryKeyError, seal_text
from stickle.data.backups import (
    backups,
    cancel_restore,
    contents,
    keep_secret_notes_secret,
    old_secret_texts,
    secret_notes_now,
    stage_restore,
    take,
)
from stickle.data.colors import ColorRepository
from stickle.data.database import WrongKeyError
from stickle.data.export import ExportResult, SecretExport, export_all
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import MigrationError, latest_version
from stickle.data.settings import RECOVERY_KEY_KEPT, Settings
from stickle.data.startup import StartupSettings
from stickle.importers.base import ImportResult
from stickle.importers.bring_in import BroughtIn, bring_in, new_notes
from stickle.importers.sources import Source, found_here, recognise
from stickle.platform.autostart import AUTOSTART_FLAG, Autostart, this_computers_autostart
from stickle.platform.clipboard import take_back_secret
from stickle.platform.hotkeys import Portal, desktop_portal
from stickle.platform.instance import FIND, NEW_NOTE, NEXT_VIEW, SET_ASIDE
from stickle.platform.linux.appimage import (
    AppMenuEntry,
    mounted_appdir,
    point_launcher,
    read_mapped_files,
    running_appimage,
)
from stickle.platform.linux.display import preferred_qt_platform
from stickle.platform.power import watch_sleep
from stickle.platform.session import watch_session
from stickle.platform.updates import updated_by_its_store
from stickle.unlock import Unlock

APP_ID = "co.linkro.stickle"


def report_ready(perf: PerfMode, manager: NoteManager) -> None:
    perf.mark("drawn")
    print(perf.ready_line(), flush=True)
    if perf.blur:
        # Idle as when the user works elsewhere: no text cursor blinking.
        for window in manager.windows:
            window.editor.clearFocus()


log = logging.getLogger(__name__)
_ended_by_signal = False
_restore_staged = False


def ended_by_signal() -> bool:
    """Whether the last run was ended by a signal (a logout, a shutdown, kill)."""
    return _ended_by_signal


def restore_staged() -> bool:
    """Whether the run ended to go back to a backup: once the database is closed,
    the staged copy is to take its place (finish_restore) and Stickle to start again."""
    return _restore_staged


def go_back_to(
    backup: Path,
    manager: NoteManager,
    connection: apsw.Connection,
    database: Path,
    key: bytes,
    quit_app: Callable[[], object],
    clock: Clock = utc_now,
    secret_key: bytes | None = None,
) -> bool:
    """Back up the notes as they are, stage the backup and quit to put it in place.
    The notes secret now stay secret in it: their earlier text there sealed with
    secret_key, or, without it, their text as it is now. False, with nothing
    changed, if the copies could not be made."""
    global _restore_staged
    manager.save_all()
    seal = None if secret_key is None else functools.partial(seal_text, secret_key)
    try:
        take(connection, database, clock, before_restore=True)
        stage_restore(database, backup)
        kept = keep_secret_notes_secret(database, key, secret_notes_now(connection), seal)
    except (OSError, apsw.Error, WrongKeyError, MigrationError) as error:
        cancel_restore(database)
        log.error("could not go back to a backup: %s", type(error).__name__)
        return False
    if kept:
        how = "their earlier text sealed" if seal is not None else "as they are now"
        log.info("%d secret notes kept secret in the backup, %s", kept, how)
    log.info("going back to a backup: starting again")
    _restore_staged = True
    QTimer.singleShot(0, quit_app)
    return True


def data_access(
    manager: NoteManager,
    connection: apsw.Connection,
    database: Path,
    key: bytes,
    quit_app: Callable[[], object],
    secret_key_copies: Callable[[], dict[str, object] | None] = lambda: None,
) -> DataAccess:
    """What the Data menu does with the notes database."""

    def export(folder: Path, secret: SecretExport) -> ExportResult:
        manager.save_all()  # what is being typed goes out too
        return export_all(connection, folder, __version__, secret=secret)

    def secret_notes() -> int:
        return sum(note.secret for note in manager.listed_notes() + manager.trash_notes())

    def old_texts(backup: Path) -> int:
        """Not known, 0: going back then keeps those notes as they are now."""
        try:
            return old_secret_texts(backup, key, secret_notes_now(connection))
        except (OSError, apsw.Error, WrongKeyError) as error:
            log.warning("could not look into a backup: %s", type(error).__name__)
            return 0

    def open_secret_notes(password: str) -> bytes:
        if manager.secrets is None:
            raise WrongPasswordError("no secret notes here")
        return manager.secrets.open(password)

    def place_for(rect: Rect) -> Place | None:
        """Where a note brought in goes: where it was, if that is on a screen now."""
        screens = monitors()
        x, y = rect.x + rect.width / 2, rect.y + min(rect.height, 20) / 2
        if not any(screen.geometry.contains(x, y) for screen in screens):
            return None  # placed as a new note is
        return place_of(rect, screens)

    def bring(source: Source, result: ImportResult, line: str | None) -> BroughtIn:
        manager.save_all()
        brought = bring_in(source.name, result, connection, place_for, source_line=line)
        manager.open_brought_in(brought.note_ids)
        return brought

    return DataAccess(
        backups=BackupAccess(
            available=lambda: backups(database),
            contents=lambda path: contents(path, key, latest_version()),
            restore=lambda path, secret_key: go_back_to(
                path, manager, connection, database, key, quit_app, secret_key=secret_key
            ),
            old_secret_texts=old_texts,
            secrets=lambda: manager.secrets,
        ),
        export=export,
        importing=ImportAccess(
            found=lambda: found_here(Path.home()),
            recognise=recognise,
            new_notes=lambda source, result: new_notes(source.name, result, connection),
            bring_in=bring,
        ),
        secret_notes=secret_notes,
        open_secret_notes=open_secret_notes,
        secret_key_copies=secret_key_copies,
    )


QT_LOG_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


# Warnings Qt gives that say nothing is wrong with Stickle, kept out of the log's warnings.
HARMLESS_QT_WARNINGS = (
    # Qt registers with the desktop portal, which knows the app already on some
    # desktops; Stickle uses nothing the registration is for.
    "Failed to register with host portal",
)


def log_qt_message(kind: QtMsgType, _context: QMessageLogContext, message: str) -> None:
    level = QT_LOG_LEVELS.get(kind, logging.WARNING)
    if level == logging.WARNING and message.startswith(HARMLESS_QT_WARNINGS):
        level = logging.DEBUG
    logging.getLogger("qt").log(level, "%s", message)


def connect_stickle_window(
    window: StickleWindow,
    manager: NoteManager,
    tray: Tray | None,
    quit_app: Callable[[], object],
) -> None:
    """When the Stickle window opens, and when closing it ends the app.

    With a tray, the app stays in it when every note is hidden, and the tray's
    menu opens the window (a double click on the icon makes a new note, see
    Tray). Without one there would be no way back,
    so the window opens saying all notes are hidden, and closing it quits;
    closing it while the notes are set aside brings them back.
    """
    if tray is not None:
        tray.open_window_action.triggered.connect(lambda: window.open())
        tray.guide_action.triggered.connect(window.guide.open)
        tray.about_action.triggered.connect(window.about.present)
        tray.feedback_action.triggered.connect(window.about.report)
        tray.find_action.triggered.connect(window.finder.open)
        tray.data_menu.menuAction().setVisible(not window.data_button.isHidden())
        tray.import_action.triggered.connect(window.bring_in)
        tray.export_action.triggered.connect(window.export_all)
        tray.restore_backup_action.triggered.connect(window.restore_backup)
        tray.settings_action.triggered.connect(lambda: window.open_settings())
        return

    manager.last_note_closed.connect(lambda: window.open(notice=True))

    def window_closed() -> None:
        if manager.set_aside:
            # Without a tray, nothing else on screen would lead back to them.
            manager.bring_back()
        elif not manager.windows:
            quit_app()

    window.closed.connect(window_closed)

    def notes_changed() -> None:
        if manager.windows:
            window.show_notice(False)  # a note is back: the notice no longer holds

    manager.changed.connect(notes_changed)


def answer_request(asked: bytes, manager: NoteManager, window: StickleWindow) -> None:
    """Do what a command line asked (see stickle.platform.instance)."""
    if asked == NEW_NOTE:
        manager.quick_note()
    elif asked == SET_ASIDE:
        manager.switch_set_aside()
    elif asked == FIND:
        window.finder.open()
    elif asked == NEXT_VIEW:
        manager.next_view()
    else:
        window.open()


def fits_any(_key: bytes) -> bool:
    return True


def recovery_keys(
    unlock: Unlock | None,
    settings: Settings | None,
    changes: ProtectionChanges | None = None,
    fits: Callable[[bytes], bool] = fits_any,
) -> RecoveryKeys | None:
    """Making recovery keys for the key that opened the notes (none in measurement
    mode); each one made noted in changes, to be told later. With secret notes,
    their key is sealed for the new one once fits shows it opens them."""
    if unlock is None or unlock.opened_key is None or settings is None:
        return None
    key = unlock.opened_key

    def made(recovery_key: str) -> str:
        if changes is not None:
            changes.note("recovery_key")
        return recovery_key

    def make_with_secret(secret_key: bytes) -> str:
        if not fits(secret_key):
            raise NotKeptForRecoveryKeyError("the key opens no secret note")
        return made(unlock.make_recovery_key(key, secret_key))

    def upgrade(typed: str) -> None:
        unlock.upgrade_recovery_key(typed)
        settings.set(RECOVERY_KEY_KEPT, True)  # typed: the user has it

    return RecoveryKeys(
        exists=lambda: unlock.has_recovery_key,
        make=lambda: made(unlock.make_recovery_key(key)),
        kept=lambda: settings.set(RECOVERY_KEY_KEPT, True),
        made_at=lambda: unlock.recovery_key_made_at,
        was_kept=lambda: settings.get(RECOVERY_KEY_KEPT),
        make_with_secret=make_with_secret,
        is_old=lambda: unlock.recovery_key_is_old,
        upgrade=upgrade,
    )


def master_passwords(
    unlock: Unlock | None, changes: ProtectionChanges | None = None
) -> MasterPasswords | None:
    """The master password for the key that opened the notes (none in measurement
    mode); each one set or changed noted in changes, to be told later."""
    if unlock is None or unlock.opened_key is None:
        return None
    key = unlock.opened_key

    def set_password(password: str) -> None:
        unlock.set_master_password(key, password)
        if changes is not None:
            changes.note("master_password_set")

    def change_password(current: str, new: str) -> None:
        unlock.change_master_password(current, new)
        if changes is not None:
            changes.note("master_password_changed")

    def recovery_key_opens(typed: str) -> bool:
        """RecoveryKeyTypoError for a mistyped one."""
        try:
            return unlock.open_with_recovery_key(typed) == key
        except WrongRecoveryKeyError, KeyFileError, OSError:
            return False

    return MasterPasswords(
        exists=lambda: unlock.has_master_password,
        set=set_password,
        change=change_password,
        check=unlock.is_master_password,
        opens_notes=unlock.opens_with_password,
        recovery_key_opens=recovery_key_opens,
        set_at=lambda: unlock.master_password_set_at,
        is_secret_password=unlock.opens_secret_notes,
    )


def secret_passwords(
    unlock: Unlock | None,
    master: MasterPasswords | None,
    changes: ProtectionChanges | None = None,
    recovery: RecoveryKeys | None = None,
    fits: Callable[[bytes], bool] = fits_any,
) -> SecretPasswords | None:
    """The secret notes' key (none in measurement mode); their password changed
    or set anew noted in changes, to be told later. A key from anywhere but
    their password is taken only once fits shows it opens them."""
    if unlock is None or unlock.opened_key is None or master is None:
        return None

    def open_secret_notes(password: str) -> bytes:
        """With their password; sealed again for the recovery key if it is not
        (a new one made without it, Stickle stopped in between, an earlier build)."""
        key = unlock.open_secret_notes(password)
        if not unlock.secret_notes_kept_for_recovery_key and fits(key):
            try:
                unlock.keep_secret_notes_for_recovery_key(key)
                log.info("secret notes kept for the recovery key again")
            except (KeyFileError, OSError) as error:
                log.warning("secret notes not kept for the recovery key: %s", type(error).__name__)
        return key

    def open_with_recovery_key(typed: str) -> bytes:
        key = unlock.open_secret_notes_with_recovery_key(typed)
        if not fits(key):
            raise NotKeptForRecoveryKeyError("the key opens no secret note")
        return key

    def change_password(key: bytes, password: str) -> None:
        unlock.change_secret_password(key, password)
        if changes is not None:
            changes.note("secret_password_changed")

    def reset_password(key: bytes, password: str) -> None:
        unlock.change_secret_password(key, password)
        if changes is not None:
            changes.note("secret_password_reset")

    return SecretPasswords(
        turned_on=lambda: unlock.has_secret_notes,
        turn_on=unlock.turn_on_secret_notes,
        open=open_secret_notes,
        open_with_recovery_key=open_with_recovery_key,
        change_password=change_password,
        reset_password=reset_password,
        is_password=unlock.opens_secret_notes,
        master=master,
        recovery=recovery,
        set_at=lambda: unlock.secret_password_set_at,
    )


def tell_changes(changes: ProtectionChanges, window: StickleWindow, tray: Tray | None) -> None:
    """Changes now due are told: at the top of Stickle Manager and, with it
    closed, in one notification of the system (a click on it opens it)."""
    if not changes.due():
        return
    window.refresh_changes()
    told = window.changes_told()
    if told and tray is not None and not window.isVisible():
        tray.showMessage("Stickle", told)


def read_appimage_files() -> None:
    """For an AppImage, read the files it runs from in the background (see read_mapped_files)."""
    appdir = mounted_appdir()
    if appdir is None:
        return

    def read() -> None:
        started = time.perf_counter()
        try:
            total = read_mapped_files(appdir)
        except OSError as error:
            log.warning("could not read the AppImage's files: %s", type(error).__name__)
            return
        log.debug("read %d MB of the AppImage in %.2fs", total >> 20, time.perf_counter() - started)

    threading.Thread(target=read, name="appimage-files", daemon=True).start()


def point_launcher_here() -> None:
    """For an AppImage, make the launcher the entries start lead to this one."""
    appimage = running_appimage()
    if appimage is None:
        return
    try:
        if point_launcher(appimage):
            log.info("the launcher now leads to this AppImage")
    except OSError as error:
        # The entries then name the AppImage itself, as before.
        log.warning("could not point the launcher: %s", type(error).__name__)


def app_list_entry() -> AppMenuEntry | None:
    """For an AppImage, its entry in the application list (followed if it moved)."""
    appimage = running_appimage()
    if appimage is None:
        return None
    entry = AppMenuEntry(appimage, icon_png())
    try:
        if entry.refresh():
            log.info("the application list entry was brought up to date")
    except OSError as error:
        log.warning("could not update the application list: %s", type(error).__name__)
    return entry


def on_this_computer(
    measuring: bool, portable: bool
) -> tuple[Autostart | None, AppMenuEntry | None, Callable[[], Portal | None]]:
    """What Stickle keeps on the computer itself: starting at login, its entry
    in the application list (with the launcher it leads to) and, under Wayland,
    shortcuts kept in the desktop's settings.

    None of it when measuring (the real login items are not to be touched) or
    for portable notes, which leave nothing behind on the computer they are
    used on (the program's path would change from computer to computer anyway).
    """
    if measuring or portable:
        return None, None, no_portal
    point_launcher_here()
    autostart = this_computers_autostart()
    try:
        if autostart.refresh():
            log.info("start at login was brought up to date")
    except OSError as error:
        log.warning("could not update start at login: %s", type(error).__name__)
    return autostart, app_list_entry(), lambda: desktop_portal(APP_ID)


def open_at_start(
    manager: NoteManager, window: StickleWindow, tray_available: bool, at_login: bool = False
) -> None:
    """Open the stored notes; with only hidden ones, the Stickle window that lists
    them, unless Stickle was started at login (it then waits in the tray)."""
    manager.open_stored()
    if not manager.windows and not (at_login and tray_available):
        window.open(notice=not tray_available)


def use_language(translations: Translations, startup: StartupSettings | None) -> None:
    """Apply the language chosen on this computer, and keep any new choice."""
    translations.apply(startup.language if startup else None)
    if startup is None:
        return

    def remember() -> None:
        try:
            startup.set_language(translations.language)
        except OSError as error:
            # The choice still applies now; it is only not kept for next time.
            log.error("could not keep the interface language: %s", type(error).__name__)

    translations.changed.connect(remember)


def run(
    argv: list[str],
    perf: PerfMode | None = None,
    unlock: Unlock | None = None,
    started: float | None = None,
    startup: StartupSettings | None = None,
    instance: Path | None = None,
    at_login: bool = False,
    request: bytes | None = None,
    portable: bool = False,
) -> int:
    """Run the app. In measurement mode, open perf.notes notes and print READY.

    at_login: started at login (--autostart), so no Stickle window at start.

    With unlock, the notes database is opened first (asking for a password or
    showing the recovery dialog when needed); quitting there ends the app.
    With startup, the language chosen before applies from the first window
    on, the password prompt and recovery screen included, and a new choice
    is kept there. With instance (the data folder, whose instance lock this
    process holds), a second start of Stickle brings up the Stickle window, or
    does what its command line asked. request: what this start's own command
    line asked (see stickle.platform.instance), done once the notes are up.
    portable: the notes are in a stickle-data folder next to the program
    (instance), carried from computer to computer; nothing is left on this one.
    """
    timings: list[str] = []

    def mark(phase: str) -> None:
        if perf is not None:
            perf.mark(phase)
        if started is not None:
            timings.append(f"{phase} {time.perf_counter() - started:.2f}s")

    mark("imports")
    if sys.platform == "linux" and (platform := preferred_qt_platform(os.environ)):
        # An argument rather than QT_QPA_PLATFORM, so programs we open do not inherit it.
        argv = [argv[0], "-platform", platform, *argv[1:]]
    app = QApplication(argv)
    app.setApplicationName("Stickle")
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)
    # Qt and Python are loaded now; read again below once the notes are up, for
    # what was loaded after (a second read is served from memory).
    read_appimage_files()
    # Listening at once: a second start may come while a password is being asked for.
    instance_server = InstanceServer(instance) if instance is not None else None
    mark("qt")
    # Ctrl+C in a terminal, logout and shutdown all end the app, also while a start-up
    # dialog is open. quit() only acts once the main loop runs; until then exit() ends
    # the dialog's loop (and any later one) instead.
    main_loop_running = False
    # Asked to end while starting up. exit() closes the dialog open at the time,
    # but a dialog that goes on when closed (the welcome) must not lead into the
    # main loop, which would then run on.
    ended_early = False

    def on_signal() -> None:
        nonlocal ended_early
        global _ended_by_signal
        _ended_by_signal = True
        if main_loop_running:
            app.quit()
        else:
            ended_early = True
            app.exit(0)

    watcher = SignalWatcher(on_signal)
    connection = None
    try:
        ensure_korean_font()
        mark("fonts")
        translations = Translations()
        use_language(translations, startup)
        log.info("interface language: %s", QLocale().name())
        mark("translations")
        if perf is not None:
            open_storage_like_startup(perf)
        if unlock is not None:
            qInstallMessageHandler(log_qt_message)
            connection = open_notes(unlock)
            if connection is None:
                return 0
            mark("notes-database")

        manager = NoteManager(
            NoteRepository(connection) if connection else None,
            settings=Settings(connection) if connection else None,
            layouts=LayoutRepository(connection) if connection else None,
            labels=LabelRepository(connection) if connection else None,
            colors=ColorRepository(connection) if connection else None,
        )
        manager.watch_quit(app)
        app.aboutToQuit.connect(manager.save_all)
        app.aboutToQuit.connect(take_back_secret)  # a recovery key copied, not left behind
        manager.empty_old_trash()

        # Logging out or shutting down: save first. No quitting yet, since another
        # program may still cancel the logout.
        def before_session_end(*_: object) -> None:
            log.info("saving before the session ends")
            manager.save_all()
            manager.lock_secret_notes()

        def before_sleep() -> None:
            log.info("saving before sleep")
            manager.save_all()
            manager.lock_secret_notes()

        app.commitDataRequest.connect(before_session_end)
        close_filter = None
        if sys.platform == "win32" and perf is None:
            from stickle.platform.windows.restart_manager import CloseForUpdate, register_restart

            # Updated while running (the Store does that): saved and closed when
            # asked, then started again by Windows, quietly as at login.
            if not register_restart(AUTOSTART_FLAG):
                log.warning("could not ask to be started again after an update")

            def close_for_update() -> None:
                log.info("closing for an update")
                manager.save_all()
                app.quit()

            close_filter = CloseForUpdate(close_for_update)
            app.installNativeEventFilter(close_filter)
        sleep_watch = watch_sleep(before_sleep)
        log.info("sleep %s", "watched" if sleep_watch is not None else "not watched")
        unlock_watch: object | None = None
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        autostart, app_list, make_portal = on_this_computer(perf is not None, portable)
        # Not when measuring, nor where the Store or Flatpak keep Stickle up to date.
        updates = (
            UpdateCheck(Settings(connection))
            if connection is not None and perf is None and not updated_by_its_store()
            else None
        )
        tray = Tray(manager.new_note, app.quit, translations, manager, updates)
        tray.show()
        # Changes to what protects the notes, told at a start or an unlock (not when measuring).
        changes = (
            ProtectionChanges(Settings(connection))
            if connection is not None and perf is None
            else None
        )
        recovery = recovery_keys(
            unlock, Settings(connection) if connection else None, changes, manager.secret_key_fits
        )
        # Measuring must not take the user's shortcuts from their own Stickle.
        shortcuts = (
            GlobalShortcuts(Settings(connection) if connection else None, make_portal=make_portal)
            if perf is None
            else None
        )
        daily_backups: DailyBackups | None = None
        access: DataAccess | None = None
        if unlock is not None and connection is not None and unlock.opened_key is not None:
            daily_backups = DailyBackups(connection, unlock.database)
            access = data_access(
                manager,
                connection,
                unlock.database,
                unlock.opened_key,
                app.quit,
                unlock.secret_key_copies,
            )
        master = master_passwords(unlock, changes)
        # Before any note opens: each offers to become a secret note.
        manager.secrets = secret_passwords(
            unlock, master, changes, recovery, manager.secret_key_fits
        )
        stickle_window = StickleWindow(
            manager,
            translations,
            app.quit,
            autostart,
            app_list,
            recovery,
            Settings(connection) if connection else None,
            shortcuts=shortcuts,
            portable_folder=instance if portable else None,
            data=access,
            updates=updates,
            master=master,
            changes=changes,
        )
        connect_stickle_window(stickle_window, manager, tray if tray_available else None, app.quit)

        def answer(asked: bytes) -> None:
            answer_request(asked, manager, stickle_window)

        if shortcuts is not None:
            shortcuts.pressed.connect(answer)

        if perf is not None:
            for _ in range(max(1, perf.notes)):
                manager.open_unstored(SAMPLE_NOTE)
        else:
            first_start = unlock is not None and unlock.first_start and connection is not None
            if first_start and unlock is not None and connection is not None:
                opened = unlock
                key = opened.opened_key
                assert key is not None
                welcome(
                    NoteRepository(connection),
                    Settings(connection),
                    lambda: opened.make_recovery_key(key),
                    autostart,
                    app_list,
                    translations=translations,
                    master=master_passwords(opened),
                    # A forgotten password has no way back but the recovery key.
                    later_allowed=not opened.uses_password,
                )
            if connection is not None:
                give_news(NoteRepository(connection), Settings(connection), first_start)
            open_at_start(manager, stickle_window, tray_available, at_login)
            # Changes made in an earlier run are told now, and anything made later
            # once the computer has been locked and unlocked; locking it locks
            # the secret notes left open.
            shown_tray = tray if tray_available else None
            told, window_for_changes = changes, stickle_window
            if told is not None:
                tray.messageClicked.connect(lambda: stickle_window.open())
                tell_changes(told, stickle_window, shown_tray)

            def unlocked() -> None:
                if told is not None:
                    tell_changes(told, window_for_changes, shown_tray)

            unlock_watch = watch_session(unlocked, manager.lock_secret_notes)
            log.info("locking %s", "watched" if unlock_watch is not None else "not watched")
            if shortcuts is not None:
                # The desktop may ask about them: after the first start's windows.
                shortcuts.start(stickle_window)
            if request is not None:
                answer(request)
            if instance_server is not None:
                # Started again while this one was still starting: done now, in
                # order, then as each comes. Nothing is asked in between, as
                # requests are only read while the main loop (or a dialog) runs.
                for asked in instance_server.received:
                    answer(asked)
                instance_server.asked.connect(answer)
            # At first start the welcome asked already.
            if app_list is not None and connection is not None and not (at_login or first_start):
                entry, settings = app_list, Settings(connection)
                # Once the notes are up: the first run of this AppImage.
                QTimer.singleShot(0, lambda: offer_app_list(entry, settings))
            if recovery is not None and connection is not None:
                repository = NoteRepository(connection)
                # The user's own setting up, as at the first start: not noted.
                assert unlock is not None and unlock.opened_key is not None
                opened_key, offering = unlock.opened_key, unlock
                offer = RecoveryOffer(
                    Settings(connection),
                    repository,
                    lambda: offering.make_recovery_key(opened_key),
                    secret_notes_on=lambda: offering.has_secret_notes,
                )
                # Out of the saving in progress: the offer is a window of its own.
                manager.note_created.connect(lambda: QTimer.singleShot(0, offer.check))
                if not (at_login or first_start):
                    QTimer.singleShot(0, offer.check)
        mark("notes")
        if perf is not None:
            # Runs once the queued show and paint events have been handled.
            QTimer.singleShot(0, lambda: report_ready(perf, manager))
        elif started is not None:
            # Includes any time spent at the password prompt.
            QTimer.singleShot(0, lambda: log.info("started: %s", ", ".join(timings)))
        if ended_early:
            log.info("asked to end while starting: quitting")
            manager.save_all()
            return 0
        main_loop_running = True
        QTimer.singleShot(0, read_appimage_files)
        if daily_backups is not None:
            daily_backups.start()
        if updates is not None:
            updates.start()
        try:
            return app.exec()
        finally:
            del sleep_watch, close_filter, unlock_watch
    finally:
        watcher.close()
        if connection is not None:
            connection.close()
        log.info("quit")
