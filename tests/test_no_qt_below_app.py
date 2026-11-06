"""The notes' model, their storage, their encryption and sync use no Qt: they
are tested without it, and are the reference for doing the same in another
language. Every module of theirs is imported in a fresh interpreter, which
must then hold no PySide6."""

import subprocess
import sys
from pathlib import Path

import stickle

PACKAGES = ("core", "data", "crypto", "sync")


def test_core_data_crypto_and_sync_import_no_qt() -> None:
    root = Path(stickle.__file__).parent
    modules = sorted(
        "stickle."
        + ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
        for package in PACKAGES
        for path in (root / package).rglob("*.py")
    )
    assert any(name.startswith("stickle.sync.") for name in modules)
    script = (
        "import importlib, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'PySide6'))\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=True
    )

    assert done.stdout.strip() == "[]"
