"""Put the licenses of everything shipped with Stickle into the build folder.

    uv run python scripts/notices.py <folder with the program>

Writes <folder>/licenses/: THIRD-PARTY-NOTICES.txt, which lists every component
with its license and where its text is, and the texts themselves. The Python
packages come from the environment the build ran in (Stickle's runtime
requirements and theirs); on Linux, the system libraries bundled into the folder
come with their package's license files. Fails, naming them, if any shipped
Python package or bundled library has no license text to go with it.
"""

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "packaging" / "licenses"
SRC = ROOT / "src" / "stickle"
NOTICES = "THIRD-PARTY-NOTICES.txt"
# Qt's wheels name only the commercial license in their metadata; the builds use
# Qt under the LGPL, whose text (with the GPL it builds on) goes instead.
QT = {"pyside6-essentials", "shiboken6"}
# Compiled into the program itself, so not among the runtime requirements.
COMPILER = "nuitka"
# Packages whose wheels carry no license file: the text from their repository
# (pywinrt v3.2.1, MIT), kept in packaging/licenses.
NO_FILE_IN_WHEEL = {"winrt-": "pywinrt-LICENSE.txt"}
# Code a wheel builds in whose license its own file leaves out: SQLite3 Multiple
# Ciphers 2.4.0 (MIT) in apsw-sqlite3mc, beside SQLite, which is public domain.
ALSO_INSIDE = {"apsw-sqlite3mc": "sqlite3mc-LICENSE.txt"}
# What a package's metadata says too vaguely.
LICENSES = {
    "apsw-sqlite3mc": "zlib-style (apsw); SQLite3 Multiple Ciphers: MIT; SQLite: public domain",
    COMPILER: "AGPL-3.0 with the Nuitka Runtime Library Exception (LICENSE-RUNTIME.txt)",
}
# Where the system keeps the libraries bundle_linux_libs.py copied in.
SYSTEM_LIBRARIES = (Path("/usr/lib64"), Path("/lib64"), Path("/usr/lib"))
LICENSE_NAME = re.compile(r"(licen[cs]e|copying|notice|authors)", re.I)
DOC_LICENSE = re.compile(r"(licen[cs]e|copying)", re.I)
# Packages of the build image that install no license text at all: the text from
# their source package (libxcb 1.13.1's COPYING; xcb-util-keysyms 0.4.0 has only
# the notice at the top of keysyms.c), kept in packaging/licenses.
NO_FILE_IN_RPM = {
    "libxcb": "libxcb-COPYING.txt",
    "xcb-util-keysyms": "xcb-util-keysyms-COPYING.txt",
}


@dataclass
class Component:
    name: str
    version: str
    license: str
    files: list[str] = field(default_factory=list[str])  # inside licenses/
    note: str = ""


def runtime_distributions(
    root: str = "stickle", find: Callable[[str], Distribution] = distribution
) -> list[Distribution]:
    """root's runtime requirements, theirs and so on, for this platform."""
    seen: dict[str, Distribution] = {}
    pending = list(find(root).requires or [])
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in seen:
            continue
        seen[name] = find(requirement.name)
        pending.extend(seen[name].requires or [])
    return sorted(seen.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def license_expression(dist: Distribution) -> str:
    meta = dist.metadata
    if canonicalize_name(meta["Name"]) in LICENSES:
        return LICENSES[canonicalize_name(meta["Name"])]
    expression = meta.get("License-Expression") or ""
    if expression:
        return expression
    short = (meta.get("License") or "").strip()
    if short and "\n" not in short and len(short) < 80:
        return short
    classifiers = [
        c.rsplit(" :: ", 1)[-1]
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    return ", ".join(classifiers) or "see the license text"


def license_files(dist: Distribution) -> list[Path]:
    """The license texts a distribution installed: its dist-info's licenses folder,
    the files its metadata names, or a LICENSE/COPYING/NOTICE file beside them."""
    named = set(dist.metadata.get_all("License-File") or [])
    found: list[Path] = []
    for file in dist.files or []:
        parts = file.parts
        in_info = len(parts) >= 2 and parts[0].endswith(".dist-info")
        if not in_info:
            continue
        if (len(parts) >= 3 and parts[1] == "licenses") or (
            len(parts) == 2 and (file.name in named or LICENSE_NAME.search(file.name))
        ):
            path = Path(str(file.locate()))
            if path.is_file():
                found.append(path)
    return found


def python_license() -> Path:
    """The license of the Python the program was built with (its LICENSE.txt
    also lists what its own build includes: OpenSSL, libffi, zlib, ...)."""
    base = Path(sys.base_prefix)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for candidate in (base / "LICENSE.txt", base / "lib" / version / "LICENSE.txt"):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"no LICENSE.txt in {base}")


def copy_into(folder: Path, sources: Iterable[Path], licenses: Path) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for source in sources:
        target = folder / source.name
        n = 2
        while target.exists():  # two files with one name (LICENSE in two places)
            target = folder / f"{source.stem}-{n}{source.suffix}"
            n += 1
        shutil.copy2(source, target)
        names.append(target.relative_to(licenses).as_posix())
    return names


def python_components(
    licenses: Path, dists: list[Distribution]
) -> tuple[list[Component], list[str]]:
    """One component per package, its texts in python-packages/<name>/; and the
    names of those with no text."""
    components: list[Component] = []
    missing: list[str] = []
    for dist in dists:
        name = dist.metadata["Name"]
        key = canonicalize_name(name)
        if key in QT:
            continue  # with Qt below
        files = license_files(dist) or [
            PACKAGED / text for prefix, text in NO_FILE_IN_WHEEL.items() if key.startswith(prefix)
        ]
        if not files:
            missing.append(name)
            continue
        if key in ALSO_INSIDE:
            files.append(PACKAGED / ALSO_INSIDE[key])
        copied = copy_into(licenses / "python-packages" / key, files, licenses)
        components.append(Component(name, dist.version, license_expression(dist), copied))
    return components, missing


@dataclass
class LibraryPackage:
    name: str
    version: str
    license: str
    files: list[Path]


def rpm(*args: str) -> str:
    return subprocess.run(["rpm", *args], capture_output=True, text=True, check=True).stdout


def library_packages(
    folder: Path, query: Callable[..., str] = rpm
) -> tuple[list[LibraryPackage], list[str]]:
    """The system packages of the shared libraries bundled into folder (Linux,
    built where rpm knows them), with their license files; and the libraries a
    package owns that has no license file."""
    owners: dict[str, list[str]] = {}
    for library in sorted(folder.glob("*.so*")):
        for system in SYSTEM_LIBRARIES:
            original = system / library.name
            if original.exists():
                try:
                    owner = query("-qf", "--queryformat", "%{NAME}\n", str(original)).split()[0]
                except subprocess.CalledProcessError:
                    break  # built here (Qt's, Python's), not a system package
                owners.setdefault(owner, []).append(library.name)
                break
    packages: list[LibraryPackage] = []
    missing: list[str] = []
    for owner, libraries in sorted(owners.items()):
        version, license = query("-q", "--queryformat", "%{VERSION}\t%{LICENSE}", owner).split(
            "\t", 1
        )
        texts = package_licenses(owner, query)
        if not texts:
            missing.extend(libraries)
        packages.append(LibraryPackage(owner, version, license, texts))
    return packages, missing


def package_licenses(package: str, query: Callable[..., str]) -> list[Path]:
    """The license files of an installed package, looked for where packagers put them."""

    def existing(*args: str) -> list[Path]:
        return [Path(p) for p in query(*args).splitlines() if Path(p).is_file()]

    def own(name: str) -> list[Path]:
        # Older packages mark the license as documentation (libXau, lz4-libs).
        return existing("-qL", name) or [
            p for p in existing("-qd", name) if DOC_LICENSE.match(p.name)
        ]

    texts = own(package)
    if not texts:
        # Split packages can keep it in another package built from the same
        # source: libX11-xcb in libX11-common, pcre2 in pcre2-syntax.
        source = query("-q", "--queryformat", "%{SOURCERPM}", package)
        for line in query("-qa", "--queryformat", "%{NAME}\t%{SOURCERPM}\n").splitlines():
            name, _, other = line.partition("\t")
            if other == source and name != package:
                texts += own(name)
    if not texts and package in NO_FILE_IN_RPM:
        texts = [PACKAGED / NO_FILE_IN_RPM[package]]
    return texts


def render(version: str, components: list[Component]) -> str:
    lines = [
        f"Stickle {version} is free software under the GNU General Public License,",
        "version 3 or later (GPL-3.0.txt in this folder). It includes the software",
        "below, each under its own license. The license texts are in this folder,",
        "at the paths given.",
        "",
    ]
    for c in components:
        lines.append(f"{c.name} {c.version}".rstrip())
        lines.append(f"  License: {c.license}")
        if c.files:
            lines.append("  Text: " + ", ".join(c.files))
        if c.note:
            lines.extend("  " + line for line in c.note.splitlines())
        lines.append("")
    return "\n".join(lines)


def qt_component(licenses: Path, dists: list[Distribution]) -> Component:
    qt = next(d for d in dists if canonicalize_name(d.metadata["Name"]) == "pyside6-essentials")
    files = copy_into(
        licenses, [PACKAGED / "LGPL-3.0.txt", PACKAGED / "qt-third-party.txt"], licenses
    )
    minor = ".".join(qt.version.split(".")[:2])
    return Component(
        "Qt and Qt for Python (PySide6, Shiboken6)",
        qt.version,
        "LGPL-3.0-only",
        files,
        "Qt's libraries are separate files next to the program and can be replaced\n"
        "by another build of the same Qt version. Their source:\n"
        f"https://download.qt.io/official_releases/qt/{minor}/{qt.version}/\n"
        "https://download.qt.io/official_releases/QtForPython/\n"
        "The third-party code inside Qt is listed in qt-third-party.txt.",
    )


def write(folder: Path, version: str) -> None:
    licenses = folder / "licenses"
    keep = licenses / "fcitx5-qt"  # written by build_fcitx5_qt.py, before this
    old: list[Path] = list(licenses.iterdir()) if licenses.is_dir() else []
    for item in old:
        if item == keep:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", licenses / "GPL-3.0.txt")

    dists = runtime_distributions()
    python, missing = python_components(licenses, dists)
    components = [qt_component(licenses, dists)]
    components.append(
        Component(
            "Python",
            sys.version.split()[0],
            "PSF-2.0 (with the licenses of the libraries its build includes)",
            copy_into(licenses / "python", [python_license()], licenses),
        )
    )
    components += python
    try:
        compiler = distribution(COMPILER)
    except PackageNotFoundError:
        raise SystemExit("the build environment has no Nuitka") from None
    components.append(
        Component(
            "Nuitka (its runtime, compiled into the program)",
            compiler.version,
            license_expression(compiler),
            copy_into(licenses / "nuitka", license_files(compiler), licenses),
        )
    )
    components.append(
        Component(
            "Lucide icons (category and mark icons)",
            "",
            "ISC",
            copy_into(
                licenses / "lucide", [SRC / "app" / "icons" / "lucide" / "LICENSE"], licenses
            ),
        )
    )
    fonts = sorted((SRC / "fonts").glob("*-OFL.txt")) if (SRC / "fonts").is_dir() else []
    if fonts:
        components.append(
            Component(
                "Noto Sans CJK KR and Noto Sans Mono fonts",
                "",
                "OFL-1.1",
                copy_into(licenses / "fonts", fonts, licenses),
            )
        )
    if sys.platform == "win32":
        components.append(
            Component(
                "Microsoft Visual C++ runtime (vcruntime140*.dll, msvcp140*.dll)",
                "",
                "Microsoft Visual Studio redistributable code",
                note="Redistributed as Microsoft's license terms for Visual Studio allow.",
            )
        )
    if keep.is_dir():
        components.append(
            Component(
                "fcitx5-qt (Linux input method plugin)",
                "",
                "LGPL-2.1-or-later and BSD-3-Clause",
                sorted(p.relative_to(licenses).as_posix() for p in keep.rglob("*") if p.is_file()),
            )
        )
    if sys.platform == "linux" and shutil.which("rpm"):
        packages, unlicensed = library_packages(folder)
        missing += unlicensed
        for package in packages:
            components.append(
                Component(
                    f"{package.name} (system library)",
                    package.version,
                    package.license,
                    copy_into(licenses / "libraries" / package.name, package.files, licenses),
                )
            )
    if missing:
        raise SystemExit("no license text for: " + ", ".join(sorted(missing)))
    (licenses / NOTICES).write_text(render(version, components), encoding="utf-8")
    print(f"{len(components)} components in {licenses / NOTICES}")


def main() -> int:
    from stickle import __version__

    write(Path(sys.argv[1]), __version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
