"""Starting at login from the Store package: its StartupTask, and the zip build's
shortcut cleared away. The task itself exists only inside a package, so these
play Windows' part."""

import sys
from pathlib import Path

import pytest

from stickle.platform.autostart import (
    AUTOSTART_FLAG,
    Autostart,
    Places,
    started_at_login,
    this_computers_autostart,
)

if sys.platform != "win32":
    pytest.skip("Windows", allow_module_level=True)
assert sys.platform == "win32"  # type checking on other systems passes over the rest


class Operation:
    def __init__(self, result: object) -> None:
        self.result = result

    def get(self) -> object:
        return self.result


class Task:
    """What Windows keeps for the package's StartupTask."""

    def __init__(self, state: str) -> None:
        from winrt.windows.applicationmodel import StartupTaskState

        self.states = StartupTaskState
        self.state = getattr(StartupTaskState, state)

    def request_enable_async(self) -> Operation:
        if self.state == self.states.DISABLED:
            self.state = self.states.ENABLED
        return Operation(self.state)  # turned off by the user: stays so

    def disable(self) -> None:
        self.state = self.states.DISABLED


def packaged(tmp_path: Path, task: Task, monkeypatch: pytest.MonkeyPatch) -> Autostart:
    from stickle.platform.windows.startup_task import PackagedAutostart

    autostart = PackagedAutostart(Places(home=tmp_path, config=tmp_path, appdata=tmp_path))
    monkeypatch.setattr(autostart, "_task", lambda: task)
    return autostart


def test_it_is_turned_on_and_off_through_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = Task("DISABLED")
    autostart = packaged(tmp_path, task, monkeypatch)
    assert not autostart.enabled

    autostart.enable()
    assert autostart.enabled
    autostart.disable()
    assert not autostart.enabled
    assert not any(tmp_path.rglob("*.lnk"))  # no shortcut: the task is the way


def test_turned_off_in_windows_settings_it_cannot_be_turned_on_from_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    autostart = packaged(tmp_path, Task("DISABLED_BY_USER"), monkeypatch)

    with pytest.raises(PermissionError):
        autostart.enable()
    assert not autostart.enabled


def test_windows_is_waited_for_off_the_interfaces_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The interface's thread may not wait for Windows ("Cannot call blocking method
    # from single-threaded apartment"): the packaged Stickle stopped at its start.
    import threading

    asked_on: list[threading.Thread] = []
    task = Task("DISABLED")

    def task_here() -> Task:
        asked_on.append(threading.current_thread())
        return task

    autostart = packaged(tmp_path, task, monkeypatch)
    monkeypatch.setattr(autostart, "_task", task_here)
    autostart.enable()
    assert autostart.enabled
    autostart.disable()
    assert asked_on and threading.current_thread() not in asked_on


def test_when_windows_fails_stickle_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused() -> Task:
        raise RuntimeError("Cannot call blocking method from single-threaded apartment.")

    autostart = packaged(tmp_path, Task("DISABLED"), monkeypatch)
    monkeypatch.setattr(autostart, "_task", refused)
    assert not autostart.enabled  # shown off, not a crash
    with pytest.raises(OSError):  # the switch tells the user it did not work
        autostart.enable()


def test_a_shortcut_left_by_the_zip_build_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    autostart = packaged(tmp_path, Task("DISABLED"), monkeypatch)
    legacy = Autostart(Places(home=tmp_path, config=tmp_path, appdata=tmp_path)).path
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"shortcut")

    assert autostart.refresh()
    assert not legacy.exists()
    assert not autostart.refresh()  # nothing left to remove


def test_outside_a_package_it_is_the_startup_folder_shortcut() -> None:
    from stickle.platform.windows.startup_task import PackagedAutostart

    chosen = this_computers_autostart()
    assert type(chosen) is Autostart and not isinstance(chosen, PackagedAutostart)


def test_started_at_login_by_its_flag_or_not_at_all_outside_a_package() -> None:
    assert started_at_login(["stickle", AUTOSTART_FLAG])
    assert not started_at_login(["stickle"])
