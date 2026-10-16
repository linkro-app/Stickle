import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_private_paths.py"

# Run from a hook (as with `git commit -a`), git sets GIT_INDEX_FILE and the like
# for the real repository; left in, they point the test repositories at it.
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

PRIVATE_PATHS = [
    "CLAUDE.md",
    "CLAUDE.local.md",
    "docs/CLAUDE.md",
    ".claude/settings.json",
    ".private/plan/notes.md",
    "nested/.private/notes.md",
    ".Private/notes.md",
]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True)


def check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def write(repo: Path, relative: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "core.ignorecase", "false")
    (tmp_path / ".gitignore").write_text("CLAUDE.md\n.claude/\n.private/\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.mark.parametrize("relative", PRIVATE_PATHS)
def test_force_added_private_file_is_blocked(repo: Path, relative: str) -> None:
    write(repo, relative)
    git(repo, "add", "-f", relative)

    result = check(repo)

    assert result.returncode == 1
    assert relative in result.stderr


def test_ordinary_files_pass(repo: Path) -> None:
    for relative in ["README.md", "src/stickle/claude.py", "docs/private/notes.md"]:
        write(repo, relative)
        git(repo, "add", relative)

    assert check(repo).returncode == 0


def test_unstaging_the_private_file_unblocks_the_commit(repo: Path) -> None:
    write(repo, ".private/notes.md")
    git(repo, "add", "-f", ".private/notes.md")
    git(repo, "restore", "--staged", ".private/notes.md")

    assert check(repo).returncode == 0


def test_deleting_a_tracked_private_file_is_allowed(repo: Path) -> None:
    write(repo, ".private/notes.md")
    git(repo, "add", "-f", ".private/notes.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "leak")
    git(repo, "rm", "-q", "--cached", ".private/notes.md")

    assert check(repo).returncode == 0


def test_all_mode_finds_already_committed_private_files(repo: Path) -> None:
    write(repo, ".claude/settings.json")
    git(repo, "add", "-f", ".claude/settings.json")
    git(repo, "commit", "-q", "--no-verify", "-m", "leak")

    assert check(repo).returncode == 0
    assert check(repo, "--all").returncode == 1
