"""A look into a target's Stickle folder for a test that failed: every file, its
tag and size, through the requests any target answers (stickle.sync.targets.base)
— a fake cloud's, or a real one's in the tests run on this computer. A fake
cloud also tells its requests (FakeCloud.describe). Opening what the files hold
comes with the vault's format."""

from stickle.sync.targets.base import FOLDERS, NotThereError, Target


def describe_target(target: Target) -> str:
    lines: list[str] = []
    try:
        vault = target.get("vault.json")
        lines.append(f"vault.json  {vault.tag}  {len(vault.data)} bytes")
    except NotThereError:
        lines.append("vault.json  (none)")
    for folder in FOLDERS:
        entries = list(target.list(folder))
        lines.append(f"{folder}/  {len(entries)} files")
        lines += [f"  {entry.path}  {entry.tag}  {entry.size} bytes" for entry in entries]
    return "\n".join(lines)
