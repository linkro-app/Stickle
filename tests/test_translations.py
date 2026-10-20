import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator
from pytestqt.qtbot import QtBot

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_translations.py"
KOREAN = ROOT / "i18n" / "stickle_ko.ts"


def run_script(*args: str, root: Path) -> None:
    subprocess.run([sys.executable, str(root / "scripts" / SCRIPT.name), *args], check=True)


def copy_project(tmp_path: Path) -> Path:
    for part in ("scripts", "src", "i18n"):
        shutil.copytree(ROOT / part, tmp_path / part, ignore=shutil.ignore_patterns("*.qm"))
    return tmp_path


def test_korean_translations_are_not_garbled() -> None:
    # UTF-8 Korean read as Latin-1 turns into letters such as "ë" mixed with
    # invisible control characters (U+0080-U+009F).
    garbled = [
        text
        for element in ET.parse(KOREAN).getroot().iter("translation")
        if (text := "".join(element.itertext()))
        and any("\x80" <= char <= "\x9f" or "\xc0" <= char <= "\xff" for char in text)
    ]
    assert garbled == []


def test_every_ui_string_has_a_korean_translation(tmp_path: Path) -> None:
    # Re-extract from the current source into a copy: a new string without Korean shows up here.
    project = copy_project(tmp_path)
    run_script(root=project)

    messages = ET.parse(project / "i18n" / KOREAN.name).getroot().iter("message")
    missing = [
        message.findtext("source")
        for message in messages
        if (translation := message.find("translation")) is None
        or translation.get("type") is not None
        # Plural messages keep their text in one or more numerusform elements.
        or not "".join(translation.itertext()).strip()
    ]
    assert missing == []


def test_committed_korean_file_is_up_to_date(tmp_path: Path) -> None:
    project = copy_project(tmp_path)
    run_script(root=project)

    assert (project / "i18n" / KOREAN.name).read_bytes() == KOREAN.read_bytes()


def test_compiled_korean_translation_is_used(qtbot: QtBot, tmp_path: Path) -> None:
    project = copy_project(tmp_path)
    run_script("--compile", root=project)

    translations = str(project / "src/stickle/translations")
    korean = QLocale(QLocale.Language.Korean)
    translators: list[QTranslator] = []
    for name in ("qtbase", "stickle"):
        translator = QTranslator()
        assert translator.load(korean, name, "_", translations)
        QCoreApplication.installTranslator(translator)
        translators.append(translator)
    try:
        assert QCoreApplication.translate("Tray", "&New note") == "새 메모(&N)"
        assert QCoreApplication.translate("QWidgetTextControl", "&Copy") != "&Copy"
    finally:
        for translator in translators:
            QCoreApplication.removeTranslator(translator)
