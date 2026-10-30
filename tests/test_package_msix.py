import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from package_msix import (
    PACKAGE_NAMES,
    render_manifest,
    render_resw,
    windows_sdk_tool,
    without_packaging,
    write_resources,
)

FOUNDATION = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}"
UAP = "{http://schemas.microsoft.com/appx/manifest/uap/windows10}"
DESKTOP = "{http://schemas.microsoft.com/appx/manifest/desktop/windows10}"


def test_the_package_name_comes_from_its_resources() -> None:
    package = ET.fromstring(render_manifest("0.6.0.0", "x64"))
    name = package.find(f"{FOUNDATION}Properties/{FOUNDATION}DisplayName")
    assert name is not None and name.text == "ms-resource:PackageDisplayName"


def test_the_app_and_its_startup_task_are_still_called_stickle() -> None:
    package = ET.fromstring(render_manifest("0.6.0.0", "x64"))
    visual = package.find(f".//{UAP}VisualElements")
    startup = package.find(f".//{DESKTOP}StartupTask")
    assert visual is not None and visual.get("DisplayName") == "Stickle"
    assert startup is not None and startup.get("DisplayName") == "Stickle"


def test_the_package_languages_are_those_with_a_name() -> None:
    package = ET.fromstring(render_manifest("0.6.0.0", "x64"))
    languages = [r.get("Language") for r in package.iter(f"{FOUNDATION}Resource")]
    assert languages == list(PACKAGE_NAMES)
    assert languages[0] == "en-us"  # the default, for every other language


def test_every_name_says_stickle_first() -> None:
    assert all(name.startswith("Stickle") for name in PACKAGE_NAMES.values())


def test_a_name_is_kept_as_written_in_its_resource_file() -> None:
    name = "Stickle & <Notes> 메모"
    data = ET.fromstring(render_resw(name)).find("data")
    assert data is not None and data.get("name") == "PackageDisplayName"
    assert data.findtext("value") == name


def test_the_resource_config_keeps_every_language_in_one_file() -> None:
    index = "<index root='\\' startIndexAt='\\'/>"
    packaging = "<packaging><autoResourcePackage qualifier='Language'/></packaging>"
    config = f"<resources>{index}{packaging}</resources>"
    assert without_packaging(config) == f"<resources>{index}</resources>"


def test_a_resource_config_without_packaging_is_refused() -> None:
    with pytest.raises(SystemExit):
        without_packaging("<resources></resources>")


def makepri() -> str:
    if sys.platform != "win32":
        pytest.skip("the Windows SDK's makepri")
    try:
        return windows_sdk_tool("makepri.exe")
    except SystemExit:
        pytest.skip("no Windows SDK")


def test_the_resource_file_has_every_name_and_the_logos(tmp_path: Path) -> None:
    tool = makepri()
    layout = tmp_path / "layout"
    (layout / "Assets").mkdir(parents=True)
    for logo in ("Square44x44Logo.png", "Square150x150Logo.png", "StoreLogo.png"):
        (layout / "Assets" / logo).write_bytes(b"png")
    (layout / "AppxManifest.xml").write_text(render_manifest("0.6.0.0", "x64"), encoding="utf-8")

    resources = write_resources(layout, tmp_path / "work")

    assert sorted(p.name for p in layout.glob("*.pri")) == ["resources.pri"]
    dump = tmp_path / "dump.xml"
    subprocess.run(
        [tool, "dump", "/if", str(resources), "/of", str(dump), "/o"],
        check=True,
        capture_output=True,
    )
    tree = ET.parse(dump)
    names = {
        c.get("qualifiers", "").lower(): c.findtext("Value")
        for r in tree.iter("NamedResource")
        if r.get("name") == "PackageDisplayName"
        for c in r.iter("Candidate")
    }
    assert names == {f"language-{lang}": name for lang, name in PACKAGE_NAMES.items()}
    files = {r.get("name") for r in tree.iter("NamedResource")}
    assert {"Square44x44Logo.png", "Square150x150Logo.png", "StoreLogo.png"} <= files
