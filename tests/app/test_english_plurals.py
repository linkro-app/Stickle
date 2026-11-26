"""English is the source language, but its plurals still need a translation file:
"%n note(s)" reads "1 note" and "2 notes" only through i18n/stickle_en.ts."""

import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator
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
        # %Ln: the number as the language writes it (1,000).
        counted = ("%n" in form or "%Ln" in form for form in forms)
        assert all(counted) and all("(s)" not in form for form in forms), source


def counted(tmp_path: Path, language: str, numbers: tuple[int, ...]) -> list[str]:
    """The finder's count of notes, in that language, as its own locale writes numbers."""
    qm = tmp_path / f"stickle_{language}.qm"
    compile_to(TS_DIR / f"stickle_{language}.ts", qm)
    translator = QTranslator()
    assert translator.load(str(qm))
    QCoreApplication.installTranslator(translator)
    QLocale.setDefault(QLocale("en_US" if language == "en" else language))
    try:
        return [
            QCoreApplication.translate("DesktopFinder", "%Ln note(s)", "", number)
            for number in numbers
        ]
    finally:
        QCoreApplication.removeTranslator(translator)
        QLocale.setDefault(QLocale.system())


def test_one_note_and_two_notes(qtbot: QtBot, tmp_path: Path) -> None:
    assert counted(tmp_path, "en", (1, 2)) == ["1 note", "2 notes"]


def test_a_count_of_notes_is_written_as_the_language_writes_numbers(
    qtbot: QtBot, tmp_path: Path
) -> None:
    assert counted(tmp_path, "en", (1234,)) == ["1,234 notes"]
    assert counted(tmp_path, "ko", (1234,)) == ["메모 1,234개"]


def test_every_count_of_notes_is_written_as_the_language_writes_numbers() -> None:
    # A plain %n writes 1234: every note count goes as %Ln.
    for (_context, source), _translation in plural_messages(EN).items():
        if "note" in source and not any(
            small in source for small in ("earlier versions", "reminder", "colour")
        ):
            assert "%Ln" in source, source
