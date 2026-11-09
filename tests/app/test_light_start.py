"""A run with sync off pays nothing for sync: the network libraries, which a
Python process never gives the memory of back, and sync itself stay out of it.
Seen in a process of its own, started as the app starts, up to its first note drawn."""

import json
import os
import subprocess
import sys
from pathlib import Path

NOT_IN_A_RUN = ("ssl", "_ssl", "http.client", "urllib.request", "truststore")

RUN = """
import json
import os
import sys
import time

from stickle.app import application
from stickle.app.perf import PerfMode
from stickle.platform.credentials import CredentialStoreUnavailableError


class NoKey:
    # So that this run leaves the computer's credential store alone.
    def key(self):
        raise CredentialStoreUnavailableError()


def drawn(perf, manager):
    names = [n for n in sys.modules if n in sys.argv[1:] or n.startswith("stickle.sync")]
    print("LOADED " + json.dumps(sorted(names)), flush=True)
    os._exit(0)


application.report_ready = drawn
application.run(["stickle"], PerfMode(1, False, time.perf_counter(), NoKey()))
"""


def test_a_run_with_sync_off_loads_neither_sync_nor_the_network_libraries(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", RUN, *NOT_IN_A_RUN],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "STICKLE_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    lines = [line for line in done.stdout.splitlines() if line.startswith("LOADED ")]
    assert lines, f"it did not get as far as a note drawn\n{done.stdout}\n{done.stderr}"
    assert json.loads(lines[0].removeprefix("LOADED ")) == []
