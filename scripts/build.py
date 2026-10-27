"""Build a standalone Stickle folder with Nuitka.

    uv run python scripts/build.py

The result is build/stickle.dist/ (on Linux also build/Stickle-x86_64.AppImage).
The folder is deliberately plain: no self-extracting single file and no
executable packing, both of which make antivirus products suspicious of
Python applications.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from build_appimage import fetch

from stickle import __version__

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
DIST_DIR = BUILD_DIR / "stickle.dist"
FONTS_DIR = ROOT / "src" / "stickle" / "fonts"
ICON_FILE = BUILD_DIR / "stickle.ico"

# Fallback font for Linux systems without a Korean font (SIL Open Font License,
# which must ship next to it). Pinned release, checked against its digest.
NOTO_CJK = "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004"
NOTO = "https://raw.githubusercontent.com/notofonts/notofonts.github.io/e3ff34c3178cb4012124c9e6390b9a3535ff2c3f"
LINUX_FONT_FILES = [
    (
        f"{NOTO_CJK}/Sans/OTF/Korean/NotoSansCJKkr-Regular.otf",
        "6bcb2a0703aa137e874fc2dffa85f6c21ba9a67fa329e81b8c801663af7e992a",
        "NotoSansCJKkr-Regular.otf",
    ),
    (
        f"{NOTO_CJK}/LICENSE",
        "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
        "NotoSansCJK-OFL.txt",
    ),
    # For code blocks, on systems with no fixed-pitch font (a minimal Fedora).
    (
        f"{NOTO}/fonts/NotoSansMono/hinted/ttf/NotoSansMono-Regular.ttf",
        "65b5e2b2c4a1fba9ae8be1f026cb35b03dcb8886d9b2a4147054fde12f7e767d",
        "NotoSansMono-Regular.ttf",
    ),
    (
        f"{NOTO}/fonts/LICENSE",
        "f2095b08bed08b23a6fe26112fcd679a2bee3f002eef077eb05d215ed1051bd8",
        "NotoSansMono-OFL.txt",
    ),
]


def fetch_linux_fonts() -> None:
    FONTS_DIR.mkdir(exist_ok=True)
    for url, sha256, name in LINUX_FONT_FILES:
        shutil.copy2(fetch(url, sha256), FONTS_DIR / name)


def write_icon(path: Path, kind: str, size: int) -> None:
    """The app's icon as a file (Windows .ico, a PNG for macOS), from the same
    drawing the tray uses."""
    subprocess.run(
        [sys.executable, "-c", ICON_SCRIPT, str(path), kind, str(size)],
        check=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


ICON_SCRIPT = """
import sys
from PySide6.QtWidgets import QApplication
from stickle.app.app_icon import make_icon
app = QApplication([])
icon = make_icon()
size = int(sys.argv[3])
if not icon.pixmap(size, size).save(sys.argv[1], sys.argv[2]):
    raise SystemExit("could not write the icon")
"""
MAC_ICON_FILE = BUILD_DIR / "stickle.icns"
MAC_APP = BUILD_DIR / "stickle.app"


def write_mac_icon() -> None:
    """The app's icon as macOS's .icns, made by its own iconutil from each size."""
    iconset = BUILD_DIR / "stickle.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        write_icon(iconset / f"icon_{size}x{size}.png", "PNG", size)
        write_icon(iconset / f"icon_{size}x{size}@2x.png", "PNG", size * 2)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(MAC_ICON_FILE)], check=True)
    shutil.rmtree(iconset)


DPI_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
</assembly>
"""


def declare_dpi_awareness() -> None:
    """Declare in stickle.exe's manifest the DPI awareness Qt sets when it starts,
    so Windows (and the Store's certification checks) know it before then."""
    from package_msix import windows_sdk_tool

    mt = windows_sdk_tool("mt.exe")
    exe = DIST_DIR / "stickle.exe"
    addition = BUILD_DIR / "dpi.manifest"
    merged = BUILD_DIR / "merged.manifest"
    addition.write_text(DPI_MANIFEST, encoding="utf-8")
    subprocess.run(
        [mt, "-nologo", "-manifest", str(addition), f"-updateresource:{exe};#1"], check=True
    )
    subprocess.run([mt, "-nologo", f"-inputresource:{exe};#1", f"-out:{merged}"], check=True)
    if "PerMonitorV2" not in merged.read_text(encoding="utf-8-sig"):
        raise SystemExit("stickle.exe's manifest does not declare its DPI awareness")
    addition.unlink()
    merged.unlink()


def nuitka_command() -> list[str]:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        # A folder (standalone), or on macOS the app bundle Qt runs from there.
        "--mode=app" if sys.platform == "darwin" else "--mode=standalone",
        "--enable-plugin=pyside6",
        # Needs Qt6Pdf, which is not shipped; notes never show PDFs.
        "--noinclude-dlls=PySide6/qt-plugins/imageformats/qpdf*",
        "--include-package-data=stickle",
        # The time zone data reminders ring by (Windows has none of its own).
        "--include-package-data=tzdata",
        # Optional SQLite extensions that ship with apsw; notes use none of them.
        "--noinclude-dlls=apsw/sqlite_extra_binaries/*",
        "--noinclude-data-files=apsw/sqlite_extra_binaries/*",
        f"--output-dir={BUILD_DIR}",
        "--assume-yes-for-downloads",
        "--remove-output",
        "--product-name=Stickle",
        f"--product-version={__version__}",
        f"--file-version={__version__}",
        "--file-description=Stickle",
        "--copyright=Stickle contributors, GPL-3.0-or-later",
    ]
    if sys.platform == "win32":
        command += [
            "--msvc=latest",
            # Ship the Visual C++ runtime so nothing has to be installed.
            "--include-windows-runtime-dlls=yes",
            # No console window when started from Explorer, but --version still
            # prints when started from a terminal.
            "--windows-console-mode=attach",
            "--output-filename=stickle",
            # The program's own icon: shown for it in Explorer and in the list of
            # apps started at login, which showed none.
            f"--windows-icon-from-ico={ICON_FILE}",
            # The Store package's StartupTask (pywinrt): its modules load one
            # another at run time, where Nuitka does not follow them.
            "--include-package=winrt",
        ]
    elif sys.platform == "linux":
        command += [
            # Input method plugins (ibus, compose) are not in Nuitka's default set,
            # and without them there is no Korean, Chinese or Japanese input. The
            # fcitx5 one is built and added afterwards (build_fcitx5_qt.py).
            "--include-qt-plugins=sensible,platforminputcontexts",
            # Plugins for things notes never use, whose dependencies are not shipped:
            # printing (CUPS), embedded displays (EGLFS), the GTK 3 theme bridge,
            # the virtual keyboard (Qt Quick) and PDF images.
            "--noinclude-qt-plugins=printsupport,egldeviceintegrations",
            "--noinclude-dlls=PySide6/qt-plugins/platformthemes/libqgtk3*",
            "--noinclude-dlls=PySide6/qt-plugins/platforms/libqeglfs*",
            "--noinclude-dlls=PySide6/qt-plugins/platforms/libqminimalegl*",
            "--noinclude-dlls=PySide6/qt-plugins/platforminputcontexts/libqtvirtualkeyboard*",
            "--noinclude-dlls=PySide6/qt-plugins/imageformats/libqpdf*",
            "--noinclude-dlls=libQt6EglFs*",
            "--noinclude-dlls=libQt6Quick*",
            "--noinclude-dlls=libQt6Qml*",
            # The executable keeps Nuitka's default name, stickle.bin: a plain
            # "stickle" would collide with the stickle/ data folder next to it.
        ]
    elif sys.platform == "darwin":
        command += [
            # Built for the runner's own processor (arm64 and x64 are built
            # apart, so neither needs Rosetta).
            f"--macos-target-arch={platform.machine()}",  # arm64 or x86_64
            # Not "stickle": the stickle/ data folder sits next to it (as on
            # Linux), and the file system does not tell "Stickle" apart either.
            "--output-filename=stickle.bin",
            "--macos-app-name=Stickle",
            "--macos-app-mode=ui-element",  # a menu bar app, as on the other systems
            f"--macos-app-version={__version__}",
            f"--macos-app-icon={MAC_ICON_FILE}",
            "--noinclude-qt-plugins=printsupport",
            "--noinclude-dlls=PySide6/qt-plugins/imageformats/libqpdf*",
        ]
    command.append(str(ROOT / "src" / "stickle"))
    return command


def main() -> int:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "update_translations.py"), "--compile"], check=True
    )
    if sys.platform == "linux":
        fetch_linux_fonts()
    if sys.platform == "win32":
        BUILD_DIR.mkdir(exist_ok=True)
        write_icon(ICON_FILE, "ICO", 256)
    if sys.platform == "darwin":
        write_mac_icon()
    subprocess.run(nuitka_command(), check=True, cwd=ROOT)
    if sys.platform == "win32":
        declare_dpi_awareness()
    print(f"Built {MAC_APP if sys.platform == 'darwin' else DIST_DIR}")
    notices = [sys.executable, str(ROOT / "scripts" / "notices.py")]
    if sys.platform == "win32":
        subprocess.run([*notices, str(DIST_DIR)], check=True)
    if sys.platform == "darwin":
        subprocess.run([*notices, str(MAC_APP / "Contents" / "MacOS")], check=True)
    if sys.platform == "linux":
        # The fcitx5 plugin first, so its libraries are bundled with the rest, and
        # the licenses once everything is in the folder.
        for script in (
            "build_fcitx5_qt.py",
            "bundle_linux_libs.py",
            "notices.py",
            "build_appimage.py",
        ):
            script_path = ROOT / "scripts" / script
            subprocess.run([sys.executable, str(script_path), str(DIST_DIR)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
