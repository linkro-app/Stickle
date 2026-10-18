"""Application start-up."""

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
from stickle.app.instance_server import InstanceServer
from stickle.app.notes import NoteManager
from stickle.app.perf import SAMPLE_NOTE, PerfMode, open_storage_like_startup
from stickle.app.recovery_offer import RecoveryOffer
from stickle.app.shortcuts import GlobalShortcuts, no_portal
from stickle.app.signals import SignalWatcher
from stickle.app.startup import open_notes
from stickle.app.stickle_window import RecoveryKeys, StickleWindow
from stickle.app.tray import Tray
from stickle.app.update_check import UpdateCheck
from stickle.core.clock import Clock, utc_now
from stickle.data.backups import backups, cancel_restore, contents, stage_restore, take
from stickle.data.export import ExportResult, export_all
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version
from stickle.data.settings import RECOVERY_KEY_KEPT, Settings
from stickle.data.startup import StartupSettings
from stickle.platform.autostart import AUTOSTART_FLAG, Autostart, this_computers_autostart
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
    quit_app: Callable[[], object],
    clock: Clock = utc_now,
) -> bool:
    """Back up the notes as they are, stage the backup and quit to put it in place.
    False, with nothing changed, if either copy could not be made."""
    global _restore_staged
    manager.save_all()
    try:
        take(connection, database, clock, before_restore=True)
        stage_restore(database, backup)
    except (OSError, apsw.Error) as error:
        cancel_restore(database)
        log.error("could not go back to a backup: %s", type(error).__name__)
        return False
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
) -> DataAccess:
    """What the Data menu does with the notes database."""

    def export(folder: Path) -> ExportResult:
        manager.save_all()  # what is being typed goes out too
        return export_all(connection, folder, __version__)

    return DataAccess(
        backups=BackupAccess(
            available=lambda: backups(database),
            contents=lambda path: contents(path, key, latest_version()),
            restore=lambda path: go_back_to(path, manager, connection, database, quit_app),
        ),
        export=export,
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

    With a tray, the app stays in it when every note is hidden, and clicking
    the tray icon opens the window. Without one there would be no way back,
    so the window opens saying all notes are hidden, and closing it quits;
    closing it while the notes are set aside brings them back.
    """
    if tray is not None:

        def tray_clicked(reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                window.open()

        tray.activated.connect(tray_clicked)
        tray.open_window_action.triggered.connect(lambda: window.open())
        tray.guide_action.triggered.connect(window.guide.open)
        tray.about_action.triggered.connect(window.about.present)
        tray.feedback_action.triggered.connect(window.about.report)
        tray.find_action.triggered.connect(window.finder.open)
        tray.data_menu.menuAction().setVisible(not window.data_button.isHidden())
        tray.export_action.triggered.connect(window.export_all)
        tray.restore_backup_action.triggered.connect(window.restore_backup)
        # Switched from the tray while the window is open: it shows the change.
        tray.autostart_action.triggered.connect(lambda: window.refresh())
        tray.app_list_action.triggered.connect(lambda: window.refresh())
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


def recovery_keys(unlock: Unlock | None, settings: Settings | None) -> RecoveryKeys | None:
    """Making recovery keys for the key that opened the notes (none in measurement mode)."""
    if unlock is None or unlock.opened_key is None or settings is None:
        return None
    key = unlock.opened_key
    return RecoveryKeys(
        exists=lambda: unlock.has_recovery_key,
        make=lambda: unlock.make_recovery_key(key),
        kept=lambda: settings.set(RECOVERY_KEY_KEPT, True),
    )


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
        )
        manager.watch_quit(app)
        app.aboutToQuit.connect(manager.save_all)
        manager.empty_old_trash()

        # Logging out or shutting down: save first. No quitting yet, since another
        # program may still cancel the logout.
        def before_session_end(*_: object) -> None:
            log.info("saving before the session ends")
            manager.save_all()

        def before_sleep() -> None:
            log.info("saving before sleep")
            manager.save_all()

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
        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        autostart, app_list, make_portal = on_this_computer(perf is not None, portable)
        # Not when measuring, nor where the Store or Flatpak keep Stickle up to date.
        updates = (
            UpdateCheck(Settings(connection))
            if connection is not None and perf is None and not updated_by_its_store()
            else None
        )
        tray = Tray(manager.new_note, app.quit, translations, manager, autostart, app_list, updates)
        tray.show()
        recovery = recovery_keys(unlock, Settings(connection) if connection else None)
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
            access = data_access(manager, connection, unlock.database, unlock.opened_key, app.quit)
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
                )
            open_at_start(manager, stickle_window, tray_available, at_login)
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
                offer = RecoveryOffer(Settings(connection), repository, recovery.make)
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
            del sleep_watch, close_filter
    finally:
        watcher.close()
        if connection is not None:
            connection.close()
        log.info("quit")
