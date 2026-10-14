"""Check that storage, search, the credential store and Korean fonts work here.

    stickle --self-test

The build pipeline runs this against the packaged app, because a native
library missing from a package only shows up at run time. Prints one line
per check (never paths, note text or keys) and returns 0 unless a check
fails. A missing credential store is reported as UNAVAILABLE, not as a
failure: build containers have none, and the app will ask for a password.
"""

import hashlib
import importlib
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from stickle.data.database import KEY_BYTES, WrongKeyError, open_database
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.search import search
from stickle.platform.credentials import (
    DATABASE_KEY,
    CredentialStore,
    CredentialStoreUnavailableError,
    bundled_support_modules,
)

SAMPLE = "회의록을 내일까지 작성"


def run_checks() -> list[tuple[str, str]]:
    """(status, name) pairs; status is PASS, FAIL, UNAVAILABLE or INFO."""
    results: list[tuple[str, str]] = []

    def check(name: str, test: Callable[[], bool]) -> None:
        try:
            ok = test()
        except Exception:
            ok = False
        results.append(("PASS" if ok else "FAIL", name))

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path = folder / "selftest.db"
        key = secrets.token_bytes(KEY_BYTES)
        connection = open_store(path, key)
        note_id = NoteRepository(connection).create(SAMPLE).id

        check("encrypted database opens and stores text", lambda: True)
        # A particle attached (회의록을) and a term below the trigram length (회의).
        check("trigram search", lambda: search(connection, "회의록") == [note_id])
        check("short-term search", lambda: search(connection, "회의") == [note_id])

        def no_plaintext() -> bool:
            return all(SAMPLE.encode() not in f.read_bytes() for f in folder.iterdir())

        check("no plaintext on disk", no_plaintext)
        connection.close()

        def wrong_key_rejected() -> bool:
            try:
                open_database(path, secrets.token_bytes(KEY_BYTES)).close()
            except WrongKeyError:
                return True
            return False

        check("wrong key is rejected", wrong_key_rejected)

    results.append(password_check())
    results += credential_checks()
    results += font_checks()
    results.append(icon_check())
    results += input_method_checks()
    results.append(start_at_login_check())
    results += store_package_check()
    return results


# The input method frameworks Linux desktops use for Korean, Chinese and Japanese.
# Qt picks a plugin by the name the desktop sets (QT_IM_MODULE); without one that
# loads, typing in these languages does nothing.
INPUT_METHODS = ("ibus", "fcitx")


def input_method_checks() -> list[tuple[str, str]]:
    """Each input method's plugin is shipped and loads into this Qt (Linux only)."""
    if sys.platform != "linux":
        return []
    from PySide6.QtCore import QCoreApplication, QLibraryInfo, QPluginLoader

    # Where Qt itself looks: a build sets these paths when it starts.
    folders = {Path(folder) for folder in QCoreApplication.libraryPaths()}
    folders.add(Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)))
    files = {path for folder in folders for path in folder.glob("platforminputcontexts/*.so")}
    loaded: set[str] = set()
    for path in sorted(files):
        loader = QPluginLoader(str(path))
        if loader.load():
            meta = cast(dict[str, Any], loader.metaData()).get("MetaData", {})
            loaded.update(str(key).lower() for key in cast(dict[str, Any], meta).get("Keys", []))
    # A development environment has only the plugins PySide6 comes with; a build must
    # have them all.
    packaged = "__compiled__" in globals()
    results: list[tuple[str, str]] = []
    for name in INPUT_METHODS:
        if name in loaded:
            results.append(("PASS", f"{name} input method plugin loads"))
        else:
            results.append(("FAIL" if packaged else "INFO", f"no {name} input method plugin"))
    return results


def start_at_login_check() -> tuple[str, str]:
    """The program a login entry would start exists (a build once named one that did not)."""
    from stickle.platform.autostart import launch_command

    program = Path(launch_command()[0])
    return ("PASS" if program.is_file() else "FAIL", "start at login names this program")


def store_package_check() -> list[tuple[str, str]]:
    """Windows: what the Store package needs to start at login is in the build
    (pywinrt's modules load one another at run time, which bundling misses)."""
    if sys.platform != "win32":
        return []
    try:
        import winrt.windows.foundation  # noqa: F401  # pyright: ignore[reportUnusedImport]

        from stickle.platform.windows import (
            startup_task,  # noqa: F401  # pyright: ignore[reportUnusedImport]
        )
    except ImportError:
        results = [("FAIL", "the Store package's start at login is in the build")]
    else:
        results = [("PASS", "the Store package's start at login is in the build")]
    # Imported only when Support Stickle is chosen, which bundling may not follow.
    try:
        from stickle.platform.windows import (
            store_purchase,  # noqa: F401  # pyright: ignore[reportUnusedImport]
        )
    except ImportError:
        results.append(("FAIL", "the Store package's support purchase is in the build"))
    else:
        results.append(("PASS", "the Store package's support purchase is in the build"))
    return results


def font_checks() -> list[tuple[str, str]]:
    """Korean must be drawn with a real font, not the empty-box "missing glyph"."""
    # No window is shown. Without a display (Linux servers, build containers) Qt needs its
    # display-less mode; elsewhere the normal platform finds the system's fonts, whereas
    # the display-less mode on Windows looks for fonts in its own folder.
    if sys.platform == "linux" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication

        from stickle.app.fonts import ensure_korean_font, korean_is_drawable

        _app = QApplication.instance() or QApplication([])
        bundled = ensure_korean_font()
        drawable = korean_is_drawable()
        from PySide6.QtGui import QFont

        from stickle.app.fonts import CODE_FONT, code_family, lines_up

        family = code_family()
        aligned = lines_up(QFont(family))
    except Exception as error:
        return [("FAIL", f"Korean font check could not run ({type(error).__name__}: {error})")]
    source = f"the bundled {bundled}" if bundled else "a system font"
    return [
        ("PASS" if drawable else "FAIL", "Korean text has a font"),
        ("INFO", f"Korean drawn with {source}"),
        (
            "PASS" if aligned else "FAIL" if CODE_FONT.exists() else "UNAVAILABLE",
            f"code has a fixed-pitch font ({family})",  # builds bring one
        ),
    ]


def icon_check() -> tuple[str, str]:
    """The icons users choose are SVG files kept with the app: they must be there,
    and Qt's SVG support with them, or categories and marks show as nothing."""
    try:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication

        from stickle.app.label_icons import state_pixmap
        from stickle.core.labels import LABEL_ICONS

        _app = QApplication.instance() or QApplication([])
        image = state_pixmap(LABEL_ICONS[-1], QColor(32, 32, 32), None, 24).toImage()
        inked = sum(
            image.pixelColor(x, y).alpha() > 60
            for x in range(image.width())
            for y in range(image.height())
        )
    except Exception as error:
        return ("FAIL", f"icon check could not run ({type(error).__name__}: {error})")
    return ("PASS" if inked > 20 else "FAIL", "category and mark icons are drawn")


def credential_checks() -> list[tuple[str, str]]:
    # A missing store is fine (the app asks for a password); a missing module is a
    # packaging bug and must fail even where no store is running.
    missing: list[str] = []
    for module in bundled_support_modules():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    detail = f" (missing: {', '.join(missing)})" if missing else ""
    results = [("FAIL" if missing else "PASS", f"credential store support bundled{detail}")]
    try:
        store = CredentialStore()
    except CredentialStoreUnavailableError as error:
        return [*results, ("UNAVAILABLE", f"credential store ({error})")]
    name = f"self-test-{secrets.token_hex(4)}"
    secret = secrets.token_bytes(KEY_BYTES)
    try:
        store.write(name, secret)
        round_trip = store.read(name) == secret
        store.delete(name)
        gone = store.read(name) is None
        # Only read: creating the key is the app's decision (never while notes exist).
        key = store.read(DATABASE_KEY)
    except CredentialStoreUnavailableError as error:
        return [*results, ("UNAVAILABLE", f"credential store ({error})")]
    # The first bytes of a hash identify the key across runs without revealing it.
    fingerprint = hashlib.sha256(key).hexdigest()[:8] if key else "none yet"
    return [
        *results,
        ("PASS" if round_trip and gone else "FAIL", "credential store round trip"),
        ("INFO", f"database key fingerprint {fingerprint}"),
    ]


def password_check() -> tuple[str, str]:
    """The password mode needs libsodium, a native library that must be in the package."""
    from stickle.crypto.keyfile import unwrap_with_password, wrap_with_password

    def works() -> bool:
        key = secrets.token_bytes(KEY_BYTES)
        # The lowest strength libsodium allows: this checks the library, not the setting.
        slot = wrap_with_password(key, "비밀번호 test", opslimit=1, memlimit=8192)
        return unwrap_with_password(slot, "비밀번호 test") == key

    try:
        ok = works()
    except Exception:
        ok = False
    return ("PASS" if ok else "FAIL", "password protection (libsodium)")


def main() -> int:
    results = run_checks()
    for status, name in results:
        print(f"{status:<11} {name}")
    return 1 if any(status == "FAIL" for status, _ in results) else 0
