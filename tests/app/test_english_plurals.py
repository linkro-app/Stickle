"""English is the source language, but its plurals still need a translation file:
"%n note(s)" reads "1 note" and "2 notes" only through i18n/stickle_en.ts."""

import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTranslator
from pytestqt.qtbot import QtBot
from update_translations import TS_DIR, compile_to, extract

EN = TS_DIR / "stickle_en.ts"


def plural_messages(ts_file: Path) -> dict[tuple[str, str], ET.Element]:
    """(context, source) -> the translation element, for every plural message."""
    found: dict[tuple[str, str], ET.Element] = {}
    for context in ET.parse(ts_file).getroot().iter("context"):
        name = context.findtext("name") or ""
        for message in context.iter("message"):
            translation = message.find("translation")
            if message.get("numerus") == "yes" and translation is not None:
                found[(name, message.findtext("source") or "")] = translation
    return found


def test_the_english_file_holds_every_plural_text(tmp_path: Path) -> None:
    fresh = tmp_path / "stickle_en.ts"
    extract(fresh, plurals_only=True)
    # A new "%n …(s)" text needs `uv run python scripts/update_translations.py`
    # and its two English forms.
    assert set(plural_messages(EN)) == set(plural_messages(fresh))


def test_every_english_plural_has_a_singular_and_a_plural() -> None:
    for (_context, source), translation in plural_messages(EN).items():
        assert translation.get("type") is None, source
        forms = [form.text or "" for form in translation.findall("numerusform")]
        assert len(forms) == 2, source
        assert all("%n" in form and "(s)" not in form for form in forms), source


def test_one_note_and_two_notes(qtbot: QtBot, tmp_path: Path) -> None:
    qm = tmp_path / "stickle_en.qm"
    compile_to(EN, qm)
    translator = QTranslator()
    assert translator.load(str(qm))
    QCoreApplication.installTranslator(translator)
    try:
        one = QCoreApplication.translate("DesktopFinder", "%n note(s)", "", 1)
        two = QCoreApplication.translate("DesktopFinder", "%n note(s)", "", 2)
    finally:
        QCoreApplication.removeTranslator(translator)
    assert (one, two) == ("1 note", "2 notes")
