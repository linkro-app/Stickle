import argparse
import os
import sys
import time

from stickle import __version__


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    args = sys.argv if argv is None else argv
    from stickle.platform.credential_helper import HELPER_FLAG

    if args[1:2] == [HELPER_FLAG]:
        # Started by Stickle to reach the credential store: before anything else
        # (no Qt, no log, no single-instance lock), and only that.
        from stickle.platform.credential_helper import serve
        from stickle.platform.credentials import platform_backend

        return serve(sys.stdin, sys.stdout, platform_backend)
    parser = argparse.ArgumentParser(prog="stickle")
    parser.add_argument("--version", action="version", version=f"Stickle {__version__}")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--perf-notes", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--perf-blur", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screens", action="store_true", help=argparse.SUPPRESS)
    # Started at login (see stickle.platform.autostart): no window of its own at start.
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    # For a keyboard shortcut set in the desktop's own settings. Sent to Stickle
    # if it runs already; otherwise it starts, then does it.
    requests = parser.add_mutually_exclusive_group()
    requests.add_argument("--new-note", action="store_true", help="open a new note to type in")
    requests.add_argument("--show", action="store_true", help="open Stickle Manager")
    requests.add_argument(
        "--hide-all", action="store_true", help="hide all notes for now, or show them again"
    )
    requests.add_argument(
        "--next-category",
        action="store_true",
        help="show only the notes of the next category, or all of them again",
    )
    requests.add_argument(
        "--find", action="store_true", help="find notes on the desktop by their text"
    )
    # Unknown options are left for Qt (for example -platform).
    options, _ = parser.parse_known_args(args[1:])
    if options.self_test:
        from stickle.selftest import main as self_test

        return self_test()
    if options.screens:
        from stickle.app.screens import report

        return report(args)

    if options.perf_notes is not None:
        from stickle.platform.credentials import KeyRequest

        # Before Qt is loaded below, so that the two overlap. A key of its own, so
        # measuring never creates or touches the key of the real notes.
        key_request = KeyRequest("perf-database-key")
        from stickle.app.perf import PerfMode

        perf = PerfMode(options.perf_notes, options.perf_blur, started, key_request)
        from stickle.app.application import run

        return run(args, perf)

    import logging

    from stickle.logs import setup_logging
    from stickle.platform.paths import data_place, ensure_private_dir, writable
    from stickle.unlock import Unlock

    place = data_place()
    folder = place.folder
    if place.portable and not writable(folder):
        # Never elsewhere instead: the notes would be split, and thought safe on the stick.
        from stickle.app.portable import tell_not_writable

        return tell_not_writable(args, folder)
    warnings = ensure_private_dir(folder)
    from stickle.platform.instance import (
        FIND,
        NEW_NOTE,
        NEXT_VIEW,
        SET_ASIDE,
        SHOW,
        InstanceLock,
        ask,
    )

    chosen = (
        (options.new_note, NEW_NOTE),
        (options.show, SHOW),
        (options.hide_all, SET_ASIDE),
        (options.next_category, NEXT_VIEW),
        (options.find, FIND),
    )
    request = next((asked for on, asked in chosen if on), None)
    # Before anything else touches the data folder: one Stickle per user and folder.
    lock = InstanceLock(folder)
    if not lock.acquire():
        if ask(folder, request or SHOW):
            return 0
        # Nobody answered: the holder has gone (Windows lets go of a crashed
        # process's lock a moment later), so this one starts instead.
        if not lock.acquire():
            return 1
    setup_logging(folder / "logs", data=folder)
    log = logging.getLogger("stickle")
    log.info("Stickle %s starting%s", __version__, " (portable)" if place.portable else "")
    for warning in warnings:
        log.warning(warning)
    # Before Qt is loaded below, so that a credential store lookup overlaps it.
    unlock = Unlock(folder, portable=place.portable)

    from stickle.app.application import ended_by_signal, restore_staged, run
    from stickle.data.backups import cancel_restore, finish_restore
    from stickle.data.startup import StartupSettings
    from stickle.platform.autostart import started_at_login

    # A backup staged by a run that ended before it could be put in place is
    # not put in place now, unasked: going back is only done when chosen.
    cancel_restore(unlock.database)
    restored = False
    try:
        code = run(
            args,
            unlock=unlock,
            started=started,
            startup=StartupSettings(folder),
            instance=folder,
            at_login=started_at_login(args),
            request=request,
            portable=place.portable,
        )
        if restore_staged():
            # The database is closed by now, and the lock still held.
            try:
                restored = finish_restore(unlock.database)
            except OSError as error:
                log.error("could not put the backup in place: %s", type(error).__name__)
                cancel_restore(unlock.database)
            log.info("backup %s", "put in place" if restored else "not put in place")
    finally:
        lock.release()
    if restore_staged():
        from stickle.platform.restart import start_again

        try:
            start_again()
        except OSError as error:
            log.error("could not start again: %s", type(error).__name__)
    if os.environ.get("APPIMAGE") and ended_by_signal():
        # An AppImage runs from files its launcher serves; a shutdown ends the
        # launcher at the same moment, and the usual clean-up would then read
        # code that is no longer there and crash. The notes are saved and
        # closed by now, so end here.
        log.info("ending at once: the AppImage's files may be gone")
        logging.shutdown()
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
