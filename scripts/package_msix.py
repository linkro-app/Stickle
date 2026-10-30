"""Package the Windows build (build/stickle.dist) as an MSIX, for the Microsoft Store.

    uv run python scripts/package_msix.py --arch x64 [--sign]

The package, unsigned, is what goes to the Store, which signs what it
publishes. --sign also writes a copy signed with a test certificate made for
the purpose (-test.msix), only so it can be installed to try it out (the
certificate must then be trusted on that computer; see the -test.cer written
next to it).

Notes are written to the real %APPDATA%\\Stickle, as the other Windows builds
do: MSIX would otherwise keep what the app writes there in a folder of the
package's own and delete it when the app is removed, notes and all. Starting
at login is a StartupTask of the package, off until the user turns it on.

The package's name, which the Store shows as the title of each language's
listing, says what Stickle is, in English and in Korean; the app itself is
still called Stickle in the Start menu and in Settings.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "stickle.dist"
OUT = ROOT / "build" / "msix"
# The package identity the Store assigned to Stickle (Partner Center, Product identity).
IDENTITY_NAME = "Linkro.Stickle"
PUBLISHER = "CN=FEE816EB-1C57-4160-B4F2-6D4C924676A5"
PUBLISHER_NAME = "Linkro"
STARTUP_TASK = "StickleStartup"
LOGOS = {"Square44x44Logo.png": 44, "Square150x150Logo.png": 150, "StoreLogo.png": 50}
# The package's name in each of its languages, English first (the default); each
# must be a name reserved for the app in Partner Center.
PACKAGE_NAMES = {"en-us": "Stickle - Desktop Notes", "ko-kr": "Stickle - 바탕화면 메모"}

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"
  xmlns:desktop6="http://schemas.microsoft.com/appx/manifest/desktop/windows10/6"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap desktop desktop6 rescap">
  <Identity Name="{identity_name}" Publisher="{publisher}" Version="{version}"
    ProcessorArchitecture="{arch}"/>
  <Properties>
    <DisplayName>ms-resource:PackageDisplayName</DisplayName>
    <PublisherDisplayName>{publisher_name}</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
    <desktop6:FileSystemWriteVirtualization>disabled</desktop6:FileSystemWriteVirtualization>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0"
      MaxVersionTested="10.0.26100.0"/>
  </Dependencies>
  <Resources>
{resources}
  </Resources>
  <Applications>
    <Application Id="Stickle" Executable="stickle.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="Stickle" Description="Sticky notes that follow you"
        BackgroundColor="transparent"
        Square150x150Logo="Assets\\Square150x150Logo.png"
        Square44x44Logo="Assets\\Square44x44Logo.png"/>
      <Extensions>
        <desktop:Extension Category="windows.startupTask" Executable="stickle.exe"
          EntryPoint="Windows.FullTrustApplication">
          <desktop:StartupTask TaskId="{startup_task}" Enabled="false" DisplayName="Stickle"/>
        </desktop:Extension>
      </Extensions>
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust"/>
    <rescap:Capability Name="unvirtualizedResources"/>
  </Capabilities>
</Package>
"""

RESW = """<?xml version="1.0" encoding="utf-8"?>
<root>
  <resheader name="resmimetype"><value>text/microsoft-resx</value></resheader>
  <resheader name="version"><value>2.0</value></resheader>
  <data name="PackageDisplayName" xml:space="preserve"><value>{name}</value></data>
</root>
"""


def render_manifest(version: str, arch: str) -> str:
    return MANIFEST.format(
        identity_name=IDENTITY_NAME,
        publisher=escape(PUBLISHER, {'"': "&quot;"}),
        publisher_name=escape(PUBLISHER_NAME),
        version=version,
        arch=arch,
        startup_task=STARTUP_TASK,
        resources="\n".join(f'    <Resource Language="{language}"/>' for language in PACKAGE_NAMES),
    )


def render_resw(name: str) -> str:
    return RESW.format(name=escape(name))


def without_packaging(config: str) -> str:
    """The SDK's default resource config without its <packaging> section, which
    splits each language into a file of its own: only a bundle's resource packs
    read those, so one package would fall back to English everywhere."""
    start, end = config.find("<packaging>"), config.find("</packaging>")
    if start < 0 or end < start:
        raise SystemExit("makepri's config has no <packaging> section to take out")
    return config[:start] + config[end + len("</packaging>") :]


def write_resources(layout: Path, work: Path) -> Path:
    """resources.pri in layout, with the package's names and the logos. Indexed from
    a folder of its own rather than the whole program, so that nothing else in it
    is taken for a resource."""
    shutil.rmtree(work, ignore_errors=True)
    project = work / "project"
    shutil.copytree(layout / "Assets", project / "Assets")
    for language, name in PACKAGE_NAMES.items():
        folder = project / "strings" / language
        folder.mkdir(parents=True)
        (folder / "Resources.resw").write_text(render_resw(name), encoding="utf-8")
    makepri = windows_sdk_tool("makepri.exe")
    config = work / "priconfig.xml"
    subprocess.run(
        [makepri, "createconfig", "/cf", str(config), "/dq", "en-US", "/pv", "10.0.0", "/o"],
        check=True,
    )
    config.write_text(without_packaging(config.read_text(encoding="utf-8")), encoding="utf-8")
    resources = layout / "resources.pri"
    subprocess.run(
        [makepri, "new", "/pr", str(project), "/cf", str(config),
         "/mn", str(layout / "AppxManifest.xml"), "/of", str(resources), "/o"],
        check=True,
    )  # fmt: skip
    return resources


def package_version() -> str:
    """The Store wants four numbers: 0.2.0 becomes 0.2.0.0."""
    sys.path.insert(0, str(ROOT / "src"))
    from stickle import __version__

    numbers = [int(part) for part in __version__.split(".")[:3]]
    return ".".join(str(n) for n in [*numbers, 0])


def write_logos(folder: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    from stickle.app.app_icon import make_icon

    app = QGuiApplication.instance() or QGuiApplication(["package_msix", "-platform", "offscreen"])
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in LOGOS.items():
        pixmap = make_icon().pixmap(size, size)
        pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio).save(str(folder / name))
    del app


def windows_sdk_tool(name: str) -> str:
    """makeappx.exe or signtool.exe from the newest Windows SDK installed."""
    found = shutil.which(name)
    if found:
        return found
    kits = Path("C:/Program Files (x86)/Windows Kits/10/bin")
    # x64 tools also run on ARM (emulated); an ARM-only SDK has its own.
    candidates = sorted(kits.glob(f"10.*/x64/{name}")) or sorted(kits.glob(f"10.*/arm64/{name}"))
    if not candidates:
        raise SystemExit(f"{name} not found: install the Windows SDK")
    return str(candidates[-1])


def sign_test_copy(package: Path, publisher: str) -> Path:
    """A copy signed with a test certificate for publisher, the certificate next to it."""
    signed = package.with_name(f"{package.stem}-test.msix")
    shutil.copy2(package, signed)
    pfx = signed.with_suffix(".pfx")
    cer = signed.with_suffix(".cer")
    script = (
        f"$cert = New-SelfSignedCertificate -Type Custom -Subject '{publisher}'"
        " -KeyUsage DigitalSignature -FriendlyName 'Stickle test'"
        " -CertStoreLocation Cert:\\CurrentUser\\My"
        " -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3', '2.5.29.19={text}');"
        " $password = ConvertTo-SecureString -String 'stickle-test' -Force -AsPlainText;"
        f" Export-PfxCertificate -Cert $cert -FilePath '{pfx}' -Password $password | Out-Null;"
        f" Export-Certificate -Cert $cert -FilePath '{cer}' | Out-Null;"
        # Kept only in the files: nothing is left in this computer's certificate store.
        " Remove-Item -Path ('Cert:\\CurrentUser\\My\\' + $cert.Thumbprint)"
    )
    # Started from PowerShell 7 (as in CI), Windows PowerShell would inherit its
    # module path and not find its own certificate module (the Cert: drive).
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env=env)
    signtool = windows_sdk_tool("signtool.exe")
    subprocess.run(
        [signtool, "sign", "/fd", "SHA256", "/f", str(pfx), "/p", "stickle-test", str(signed)],
        check=True,
    )
    pfx.unlink()
    return signed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["x64", "arm64"], default="x64")
    parser.add_argument("--sign", action="store_true", help="also a copy signed for testing")
    options = parser.parse_args()
    if not (DIST / "stickle.exe").exists():
        raise SystemExit(f"{DIST} has no stickle.exe: run scripts/build.py first")

    layout = OUT / "layout"
    shutil.rmtree(layout, ignore_errors=True)
    shutil.copytree(DIST, layout)
    write_logos(layout / "Assets")
    manifest = render_manifest(package_version(), options.arch)
    (layout / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    write_resources(layout, OUT / "resources")
    package = OUT / f"stickle-windows-{options.arch}.msix"
    package.unlink(missing_ok=True)
    makeappx = windows_sdk_tool("makeappx.exe")
    subprocess.run([makeappx, "pack", "/o", "/d", str(layout), "/p", str(package)], check=True)
    print(package)
    if options.sign:
        print(sign_test_copy(package, PUBLISHER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
