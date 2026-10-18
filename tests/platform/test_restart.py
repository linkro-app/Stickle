import subprocess
import sys

import pytest

from stickle.platform import restart


def test_it_starts_a_stickle_that_outlives_this_one(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **options: object) -> None:
        started.append((command, options))

    monkeypatch.setattr(restart.subprocess, "Popen", popen)
    monkeypatch.setattr(restart, "launch_command", lambda: ["stickle"])

    restart.start_again()

    assert len(started) == 1
    command, options = started[0]
    assert command == ["stickle"]
    assert options["stdin"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert options["creationflags"] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        assert options["start_new_session"] is True
