import subprocess
from importlib.metadata import Distribution, PathDistribution
from pathlib import Path

import pytest
from notices import (
    NOTICES,
    PACKAGED,
    library_packages,
    python_components,
    runtime_distributions,
    write,
)


def make_dist(
    root: Path,
    name: str,
    requires: tuple[str, ...] = (),
    licenses: dict[str, str] | None = None,
    **meta: str,
) -> PathDistribution:
    """A dist-info folder as an installer leaves it: METADATA, RECORD and the
    license files under licenses/."""
    info = root / f"{name.replace('-', '_')}-1.0.dist-info"
    (info / "licenses").mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", "Version: 1.0"]
    lines += [f"{key.replace('_', '-')}: {value}" for key, value in meta.items()]
    lines += [f"Requires-Dist: {r}" for r in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    record = [f"{info.name}/METADATA,,"]
    for file, text in (licenses or {}).items():
        (info / "licenses" / file).write_text(text, encoding="utf-8")
        record.append(f"{info.name}/licenses/{file},,")
    (info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")
    return PathDistribution(info)


def finder(dists: list[PathDistribution]):
    by_name = {d.metadata["Name"].lower(): d for d in dists}

    def find(name: str) -> Distribution:
        return by_name[name.lower()]

    return find


def test_the_requirements_of_requirements_are_followed_once_each(tmp_path: Path) -> None:
    app = make_dist(tmp_path, "app", ("one", "two"))
    one = make_dist(tmp_path, "one", ("two",))
    two = make_dist(tmp_path, "two")

    found = runtime_distributions("app", finder([app, one, two]))

    assert [d.metadata["Name"] for d in found] == ["one", "two"]


def test_requirements_for_another_platform_or_an_extra_are_left_out(tmp_path: Path) -> None:
    app = make_dist(
        tmp_path, "app", ("here", "elsewhere ; sys_platform == 'nowhere'", "more ; extra == 'x'")
    )
    here = make_dist(tmp_path, "here")

    found = runtime_distributions("app", finder([app, here]))

    assert [d.metadata["Name"] for d in found] == ["here"]


def test_each_package_brings_its_license_text(tmp_path: Path) -> None:
    dist = make_dist(tmp_path, "pkg", licenses={"LICENSE": "MIT text"}, License_Expression="MIT")
    licenses = tmp_path / "out"

    components, missing = python_components(licenses, [dist])

    assert missing == []
    assert components[0].license == "MIT"
    assert components[0].files == ["python-packages/pkg/LICENSE"]
    assert (licenses / "python-packages" / "pkg" / "LICENSE").read_text() == "MIT text"


def test_a_package_without_a_license_text_is_named(tmp_path: Path) -> None:
    dist = make_dist(tmp_path, "bare", License_Expression="MIT")

    _components, missing = python_components(tmp_path / "out", [dist])

    assert missing == ["bare"]


def test_winrt_wheels_get_the_text_kept_beside_the_build(tmp_path: Path) -> None:
    dist = make_dist(tmp_path, "winrt-runtime", License_Expression="MIT")

    components, missing = python_components(tmp_path / "out", [dist])

    assert missing == []
    assert components[0].files == ["python-packages/winrt-runtime/pywinrt-LICENSE.txt"]


def test_qt_s_wheels_are_left_to_the_qt_entry(tmp_path: Path) -> None:
    dist = make_dist(tmp_path, "shiboken6", licenses={"LicenseRef-Qt-Commercial.txt": "x"})

    components, missing = python_components(tmp_path / "out", [dist])

    assert (components, missing) == ([], [])


def rpm_answers(
    owners: dict[str, str],
    licenses: dict[str, list[str]],
    docs: dict[str, list[str]] | None = None,
    sources: dict[str, str] | None = None,
):
    """A stand-in for rpm: -qf by library name, -q for version and license or
    source package, -qa, and -qL / -qd for the license and documentation files."""
    sources = sources or {}

    def source(package: str) -> str:
        return sources.get(package, f"{package}-1.2.src.rpm")

    def query(*args: str) -> str:
        if args[0] == "-qf":
            name = Path(args[-1]).name
            if name not in owners:
                raise subprocess.CalledProcessError(1, "rpm")
            return owners[name] + "\n"
        if args[0] == "-qa":
            return "".join(f"{p}\t{source(p)}\n" for p in licenses)
        if args[0] == "-q":
            return source(args[-1]) if "SOURCERPM" in args[2] else "1.2\tMIT"
        files = licenses if args[0] == "-qL" else docs or {}
        return "\n".join(files.get(args[-1], []))

    return query


def library_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> Path:
    """A build folder holding copies of these system libraries."""
    folder, system = tmp_path / "dist", tmp_path / "usr" / "lib64"
    folder.mkdir()
    system.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"")
        (system / name).write_bytes(b"")
    monkeypatch.setattr("notices.SYSTEM_LIBRARIES", (system,))
    return folder


def text_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "share" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("text")
    return path


def test_bundled_system_libraries_bring_their_package_s_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library_folder(tmp_path, monkeypatch, "libxcb-cursor.so.0")
    (folder / "libQt6Core.so.6").write_bytes(b"")
    license_file = text_file(tmp_path, "COPYING")
    query = rpm_answers(
        {"libxcb-cursor.so.0": "xcb-util-cursor"}, {"xcb-util-cursor": [str(license_file)]}
    )

    packages, missing = library_packages(folder, query)

    # Qt's own library is not on the system: it came with the wheel, under Qt's entry.
    assert [(p.name, p.license, p.files) for p in packages] == [
        ("xcb-util-cursor", "MIT", [license_file])
    ]
    assert missing == []


def test_a_license_kept_as_documentation_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library_folder(tmp_path, monkeypatch, "libXau.so.6")
    license_file = text_file(tmp_path, "COPYING")
    readme = text_file(tmp_path, "README")
    query = rpm_answers(
        {"libXau.so.6": "libXau"}, {"libXau": []}, docs={"libXau": [str(readme), str(license_file)]}
    )

    packages, missing = library_packages(folder, query)

    assert packages[0].files == [license_file]
    assert missing == []


def test_a_split_package_takes_the_texts_of_a_package_from_the_same_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library_folder(tmp_path, monkeypatch, "libpcre2-8.so.0")
    license_file = text_file(tmp_path, "LICENCE")
    other = text_file(tmp_path, "OTHER")
    query = rpm_answers(
        {"libpcre2-8.so.0": "pcre2"},
        {"pcre2": [], "pcre2-syntax": [str(license_file)], "zlib": [str(other)]},
        sources={"pcre2": "pcre2-10.40.src.rpm", "pcre2-syntax": "pcre2-10.40.src.rpm"},
    )

    packages, missing = library_packages(folder, query)

    assert packages[0].files == [license_file]
    assert missing == []


def test_a_package_without_any_license_file_takes_the_one_kept_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library_folder(tmp_path, monkeypatch, "libxcb-shm.so.0")

    packages, missing = library_packages(
        folder, rpm_answers({"libxcb-shm.so.0": "libxcb"}, {"libxcb": []})
    )

    assert packages[0].files == [PACKAGED / "libxcb-COPYING.txt"]
    assert packages[0].files[0].is_file()
    assert missing == []


def test_a_library_whose_package_has_no_license_file_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library_folder(tmp_path, monkeypatch, "libbare.so.1")

    _packages, missing = library_packages(
        folder, rpm_answers({"libbare.so.1": "bare"}, {"bare": []})
    )

    assert missing == ["libbare.so.1"]


def test_the_folder_lists_qt_python_and_every_shipped_package(tmp_path: Path) -> None:
    fcitx = tmp_path / "licenses" / "fcitx5-qt"
    fcitx.mkdir(parents=True)
    (fcitx / "BSD-3-Clause.txt").write_text("kept")
    (tmp_path / "licenses" / "stale.txt").write_text("from an earlier build")

    write(tmp_path, "9.9.9")

    licenses = tmp_path / "licenses"
    index = (licenses / NOTICES).read_text(encoding="utf-8")
    assert index.startswith("Stickle 9.9.9 is free software")
    assert "LGPL-3.0-only" in index and "qt-third-party.txt" in index
    for dist in runtime_distributions():
        assert f"\n{dist.metadata['Name']} {dist.version}\n" in index or "Qt for Python" in index
    for line in index.splitlines():
        if line.startswith("  Text: "):
            for path in line.removeprefix("  Text: ").split(", "):
                assert (licenses / path).is_file(), path
    assert (licenses / "GPL-3.0.txt").is_file()
    assert (fcitx / "BSD-3-Clause.txt").read_text() == "kept"
    assert not (licenses / "stale.txt").exists()
