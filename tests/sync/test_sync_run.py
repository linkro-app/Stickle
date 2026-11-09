"""The app itself, started as it starts with sync on, against a WebDAV server: a
note written before it started reaches the server, and it ends when told to.
In a process of its own, as a whole app is."""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from webdav_server import HOST, Server, webdav

from stickle.sync.format import VAULT_FILE, VaultInfo, vault_file
from stickle.sync.targets.base import Absent
from stickle.sync.targets.webdav import WebDav, parse_address

USER = "someone-signing-in"
PASSWORD = "the-password"
SHARE = "a-folder-of-theirs"

RUN = """
import json
import secrets
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from test_unlock import FAST, Store

from PySide6.QtCore import QCoreApplication, QMetaObject, Qt

from stickle.app import application
from stickle.app.startup import open_notes
from stickle.crypto.vault_keys import DataKeys
from stickle.data import sync_connection
from stickle.data.notes import NoteRepository
from stickle.data.sync_connection import WEBDAV, Login
from stickle.unlock import Unlock


def never(*_):
    raise AssertionError("asked")


folder, address, vault, user, password = (Path(sys.argv[2]), *sys.argv[3:7])
folder.mkdir(parents=True, exist_ok=True)
store = Store("empty")
first = Unlock(folder, store, FAST)
db = open_notes(first, never, never)
note = NoteRepository(db).create("written before it started")
keys = DataKeys({1: secrets.token_bytes(32)})
sync_connection.keep(db, Login(vault, WEBDAV, address, user, password, keys.keys))
db.close()


def end():
    time.sleep(8)
    app = QCoreApplication.instance()
    QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)


threading.Thread(target=end, daemon=True).start()
code = application.run(["stickle"], unlock=Unlock(folder, store, FAST), at_login=True)
said = {"code": code, "note": note.id, "sync": "stickle.sync.engine" in sys.modules}
print("ENDED " + json.dumps(said), flush=True)
"""


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    root = tmp_path / "dav"
    (root / SHARE).mkdir(parents=True)
    with webdav(root, PASSWORD, user=USER) as running:
        yield running


def test_a_note_reaches_the_server_from_the_app_itself(tmp_path: Path, server: Server) -> None:
    address = f"http://{HOST}:{server.port}/{SHARE}"
    vault = str(uuid.uuid4())
    target = WebDav(parse_address(address), USER, PASSWORD)
    target.put(VAULT_FILE, vault_file(VaultInfo(vault)), Absent())
    tests = str(Path(__file__).parent.parent)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            RUN,
            tests,
            str(tmp_path / "data"),
            address,
            vault,
            USER,
            PASSWORD,
        ],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "STICKLE_DATA_DIR": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=tmp_path,
    )

    lines = [line for line in done.stdout.splitlines() if line.startswith("ENDED ")]
    assert lines, f"it did not end as told\n{done.stdout}\n{done.stderr}"
    ended = json.loads(lines[0].removeprefix("ENDED "))
    assert ended["code"] == 0
    assert ended["sync"] is True
    listed = [entry.path for entry in target.list("notes")]
    target.close()
    assert f"notes/{ended['note']}.json" in listed
    assert PASSWORD not in done.stdout + done.stderr
    assert address not in done.stdout + done.stderr
