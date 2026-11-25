"""Every window shows the app's icon on its title bar, whatever program runs the
app: seen in a process of its own, started as the app starts, up to its first
note drawn."""

import json
import os
import subprocess
import sys
from pathlib import Path

from stickle.app.app_icon import SIZES

RUN = """
import json
import os
import time

from PySide6.QtWidgets import QApplication

from stickle.app import application
from stickle.app.perf import PerfMode
from stickle.platform.credentials import CredentialStoreUnavailableError


class NoKey:
    # So that this run leaves the computer's credential store alone.
    def key(self):
        raise CredentialStoreUnavailableError()


def drawn(perf, manager):
    icon = QApplication.windowIcon()
    windows = [w for w in QApplication.topLevelWidgets() if w.isWindow() and w.isVisible()]
    seen = {
        "sizes": sorted(size.width() for size in icon.availableSizes()),
        "windows": len(windows),
        "windows_iconless": sum(1 for w in windows if w.windowIcon().isNull()),
    }
    print("ICON " + json.dumps(seen), flush=True)
    os._exit(0)


application.report_ready = drawn
application.run(["stickle"], PerfMode(1, False, time.perf_counter(), NoKey()))
"""


def test_every_window_has_the_app_icon(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", RUN],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "STICKLE_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    lines = [line for line in done.stdout.splitlines() if line.startswith("ICON ")]
    assert lines, f"it did not get as far as a note drawn\n{done.stdout}\n{done.stderr}"
    seen = json.loads(lines[0].removeprefix("ICON "))
    assert seen["sizes"] == sorted(SIZES)
    assert seen["windows"] > 0
    assert seen["windows_iconless"] == 0
