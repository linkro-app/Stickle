"""The app identifiers the clouds gave Stickle: put in by the build, taken from
the environment when run from the source (stickle.cloud_apps, scripts/build.py)."""

import ast
import sys
import types
from pathlib import Path

import pytest
from build import CLOUD_APPS_FILE, ROOT, nuitka_command, write_cloud_apps

from stickle.cloud_apps import (
    BUILT_IN,
    DROPBOX_VARIABLE,
    ONEDRIVE_VARIABLE,
    dropbox_app_key,
    is_app_key,
    is_client_id,
    onedrive_client_id,
)
from stickle.selftest import cloud_apps_lines

GIVEN = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"  # a test's, not the app's
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
KEY = "q7w8e9r0t1y2u3i"  # a test's, not the app's


@pytest.fixture(autouse=True)
def no_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """As run from the source, whatever this computer has."""
    monkeypatch.delenv(ONEDRIVE_VARIABLE, raising=False)
    monkeypatch.delenv(DROPBOX_VARIABLE, raising=False)
    monkeypatch.delitem(sys.modules, BUILT_IN, raising=False)
    assert not CLOUD_APPS_FILE.exists(), "a build left the app identifiers in the source"


def built_in(monkeypatch: pytest.MonkeyPatch, client_id: object, app_key: object = None) -> None:
    made = types.ModuleType(BUILT_IN)
    made.ONEDRIVE_CLIENT_ID = client_id  # pyright: ignore[reportAttributeAccessIssue]
    if app_key is not None:
        made.DROPBOX_APP_KEY = app_key  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, BUILT_IN, made)


def test_from_the_source_the_environment_gives_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert onedrive_client_id() is None
    monkeypatch.setenv(ONEDRIVE_VARIABLE, f" {GIVEN} ")
    assert onedrive_client_id() == GIVEN


@pytest.mark.parametrize(
    "given", ["", "stickle", f"{GIVEN}x", GIVEN.replace("-", ""), f"'{GIVEN}'"]
)
def test_what_is_no_application_id_is_none(monkeypatch: pytest.MonkeyPatch, given: str) -> None:
    monkeypatch.setenv(ONEDRIVE_VARIABLE, given)
    assert onedrive_client_id() is None


def test_what_the_build_put_in_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    built_in(monkeypatch, GIVEN)
    monkeypatch.setenv(ONEDRIVE_VARIABLE, OTHER)
    assert onedrive_client_id() == GIVEN


def test_a_built_in_module_without_it_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    built_in(monkeypatch, None)
    monkeypatch.setenv(ONEDRIVE_VARIABLE, OTHER)
    assert onedrive_client_id() is None


def test_the_check_says_whether_there_is_one_and_never_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cloud_apps_lines() == [
        ("INFO", "OneDrive app identifier: none"),
        ("INFO", "Dropbox app identifier: none"),
    ]
    monkeypatch.setenv(ONEDRIVE_VARIABLE, GIVEN)
    monkeypatch.setenv(DROPBOX_VARIABLE, KEY)
    lines = cloud_apps_lines()
    assert lines == [
        ("INFO", "OneDrive app identifier: given"),
        ("INFO", "Dropbox app identifier: given"),
    ]
    assert not any(GIVEN in line or KEY in line for _, line in lines)


def test_is_client_id() -> None:
    assert is_client_id(GIVEN) and is_client_id(GIVEN.upper())
    assert not is_client_id(f"{GIVEN}\n")


# Dropbox's app key


def test_from_the_source_the_environment_gives_the_app_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert dropbox_app_key() is None
    monkeypatch.setenv(DROPBOX_VARIABLE, f" {KEY} ")
    assert dropbox_app_key() == KEY
    assert onedrive_client_id() is None  # each cloud's its own


@pytest.mark.parametrize("given", ["", KEY[:-1], f"{KEY}x", KEY.upper(), f"'{KEY}'", GIVEN])
def test_what_is_no_app_key_is_none(monkeypatch: pytest.MonkeyPatch, given: str) -> None:
    monkeypatch.setenv(DROPBOX_VARIABLE, given)
    assert dropbox_app_key() is None


def test_the_app_key_the_build_put_in_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    built_in(monkeypatch, GIVEN, KEY)
    monkeypatch.setenv(DROPBOX_VARIABLE, "a1b2c3d4e5f6g7h")
    assert dropbox_app_key() == KEY


def test_a_build_with_onedrive_alone_has_no_app_key(monkeypatch: pytest.MonkeyPatch) -> None:
    built_in(monkeypatch, GIVEN)
    monkeypatch.setenv(DROPBOX_VARIABLE, KEY)
    assert dropbox_app_key() is None
    assert onedrive_client_id() == GIVEN


def test_is_app_key() -> None:
    assert is_app_key(KEY)
    assert not is_app_key(f"{KEY}\n") and not is_app_key(KEY.upper())


# The build


def written(file: Path) -> dict[str, object]:
    data = file.read_bytes()
    assert b"\r" not in data
    space: dict[str, object] = {}
    exec(compile(ast.parse(data), str(file), "exec"), space)
    return {name: value for name, value in space.items() if not name.startswith("__")}


def test_the_build_writes_the_module_nuitka_is_told_to_take(tmp_path: Path) -> None:
    file = tmp_path / "_cloud_apps.py"
    options = write_cloud_apps({ONEDRIVE_VARIABLE: GIVEN, DROPBOX_VARIABLE: f" {KEY}"}, file)
    assert options == [f"--include-module={BUILT_IN}"]
    assert written(file) == {"ONEDRIVE_CLIENT_ID": GIVEN, "DROPBOX_APP_KEY": KEY}


def test_the_build_writes_what_it_is_given(tmp_path: Path) -> None:
    file = tmp_path / "_cloud_apps.py"
    assert write_cloud_apps({DROPBOX_VARIABLE: KEY}, file) == [f"--include-module={BUILT_IN}"]
    assert written(file) == {"DROPBOX_APP_KEY": KEY}


def test_the_build_writes_it_beside_the_module_that_reads_it() -> None:
    assert CLOUD_APPS_FILE == ROOT / "src" / "stickle" / "_cloud_apps.py"
    assert BUILT_IN == "stickle._cloud_apps"


def test_without_it_the_build_has_no_onedrive(tmp_path: Path) -> None:
    file = tmp_path / "_cloud_apps.py"
    assert write_cloud_apps({}, file) == []
    assert write_cloud_apps({"STICKLE_RELEASE_BUILD": "false"}, file) == []
    assert not file.exists()


def test_a_release_is_not_built_without_it(tmp_path: Path) -> None:
    file = tmp_path / "_cloud_apps.py"
    with pytest.raises(SystemExit) as stopped:
        write_cloud_apps(
            {"STICKLE_RELEASE_BUILD": "true", ONEDRIVE_VARIABLE: " ", DROPBOX_VARIABLE: KEY}, file
        )
    assert ONEDRIVE_VARIABLE in str(stopped.value)
    assert not file.exists()


def test_a_release_is_not_built_without_the_app_key(tmp_path: Path) -> None:
    file = tmp_path / "_cloud_apps.py"
    with pytest.raises(SystemExit) as stopped:
        write_cloud_apps({"STICKLE_RELEASE_BUILD": "true", ONEDRIVE_VARIABLE: GIVEN}, file)
    assert DROPBOX_VARIABLE in str(stopped.value)
    assert not file.exists()
    release = {"STICKLE_RELEASE_BUILD": "true", ONEDRIVE_VARIABLE: GIVEN, DROPBOX_VARIABLE: KEY}
    assert write_cloud_apps(release, file) == [f"--include-module={BUILT_IN}"]


@pytest.mark.parametrize("variable", [ONEDRIVE_VARIABLE, DROPBOX_VARIABLE])
def test_what_is_no_identifier_stops_the_build_unsaid(tmp_path: Path, variable: str) -> None:
    file = tmp_path / "_cloud_apps.py"
    odd = 'x"\nimport os  # '
    given = {ONEDRIVE_VARIABLE: GIVEN, DROPBOX_VARIABLE: KEY, variable: odd}
    with pytest.raises(SystemExit) as stopped:
        write_cloud_apps(given, file)
    assert variable in str(stopped.value)
    assert odd.strip() not in str(stopped.value)
    assert not file.exists()


def test_nuitka_takes_the_options_before_what_it_builds() -> None:
    command = nuitka_command(["--include-module=stickle._cloud_apps"])
    assert command[-2] == "--include-module=stickle._cloud_apps"
    assert command[-1] == str(ROOT / "src" / "stickle")
    assert "--include-module=stickle._cloud_apps" not in nuitka_command()


def test_git_leaves_the_module_out() -> None:
    ignored = (ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert "src/stickle/_cloud_apps.py" in ignored
