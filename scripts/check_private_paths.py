"""Refuse to commit files that must never reach the public repository.

The same paths are listed in .gitignore, but `git add -f` bypasses that.
This check runs as a pre-commit hook (staged files) and in CI (`--all`,
every tracked file).
"""

import subprocess
import sys

# _cloud_apps.py: the app identifiers a build writes for itself (scripts/build.py).
BLOCKED_NAMES = frozenset({"claude.md", "claude.local.md", "_cloud_apps.py"})
BLOCKED_DIRS = frozenset({".claude", ".private"})


def is_blocked(path: str) -> bool:
    parts = [part.lower() for part in path.replace("\\", "/").split("/")]
    return parts[-1] in BLOCKED_NAMES or any(part in BLOCKED_DIRS for part in parts[:-1])


def list_paths(all_tracked: bool) -> list[str]:
    if all_tracked:
        args = ["git", "ls-files", "-z"]
    else:
        # Every staged change except deletions: removing such a file is always fine.
        args = ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=d"]
    output = subprocess.run(args, capture_output=True, check=True).stdout
    return [path for path in output.decode("utf-8").split("\0") if path]


def main(argv: list[str]) -> int:
    blocked = [path for path in list_paths("--all" in argv) if is_blocked(path)]
    if not blocked:
        return 0
    print("These files are private and must not be committed:", file=sys.stderr)
    for path in blocked:
        print(f"  {path}", file=sys.stderr)
    print("Unstage them with: git restore --staged <file>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
