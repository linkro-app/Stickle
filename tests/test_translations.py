import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
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


def test_stickle_manager_goes_by_its_name_with_the_right_particles() -> None:
    # Called "the Stickle window" until 0.6: every window of Stickle is one. In
    # Korean the particle after 관리자 (ending in a vowel) is not the one after 창.
    root = ET.parse(KOREAN).getroot()
    sources = ["".join(element.itertext()) for element in root.iter("source")]
    korean = ["".join(element.itertext()) for element in root.iter("translation")]
    english = ET.parse(KOREAN.with_name("stickle_en.ts")).getroot()
    plurals = ["".join(element.itertext()) for element in english.iter("translation")]
    assert [text for text in sources + plurals if "Stickle window" in text] == []
    assert [text for text in korean if "Stickle 창" in text] == []
    wrong = re.compile(r"Stickle 관리자(을|은|이|과|으로)(?![가-힣])")
    assert [text for text in korean if wrong.search(text)] == []
    assert any("Stickle 관리자" in text for text in korean)


def test_korean_says_pc_not_computer() -> None:
    # The word Koreans use for their own machine, shorter and as easy for children;
    # the English stays "computer". The label icon of a computer keeps its name.
    root = ET.parse(KOREAN).getroot()
    korean = ["".join(element.itertext()) for element in root.iter("translation")]
    assert [text for text in korean if "컴퓨터" in text and text != "컴퓨터"] == []
    for page in ("README.ko.md", "PRIVACY.md"):
        assert "컴퓨터" not in (ROOT / page).read_text(encoding="utf-8"), page
    assert any("이 PC" in text for text in korean)


# Words that make a reader stop, with the plain ones to say instead: the Korean
# is written so that a child in the last years of primary school can read it.
HARD_WORDS = {
    "갱신": "바뀜·받음·보임",
    "반영": "보임·들어감",
    "적용": "씀·바꿈",
    "해제": "풀기·끄기",
    "초기화": "처음으로 되돌리기",
    "기본값": "처음 값",
    "항목": "the thing's own name",
    "지정": "정하기",
    "실행": "켜기·시작",
}
# Other makers' names for their products: never a name for ours.
TRADEMARKS = ("포스트잇", "Post-it", "Sticky Notes", "스티키 노트", "스티키노트")
# Strings written before the rule, to be reworded later, and an app named as the
# place notes come from: (context, start of the English, word). Each one must
# still be there, so the list can only shrink.
ALLOWED = (
    ("AppList", "Add Stickle to your list of applications", "실행"),
    ("LoginNote", "Windows Security has been seen to block Stickle", "항목"),
    ("PasswordDialog", "These notes are kept in the stickle-data folder", "실행"),
    ("PasswordDialog", "Unlock your notes", "해제"),
    ("PasswordDialog", "Unlock", "해제"),
    ("Portable", "Stickle cannot save notes in the folder next to it", "실행"),
    ("RecoveryDialog", "Stickle keeps the key to your notes in the system keychain", "해제"),
    ("RecoveryDialog", "Stickle checks each update on a copy of your notes first", "적용"),
    ("RecoveryKeyPanel", "If the key that opens your notes is ever lost", "초기화"),
    ("ShortcutGuide", "Next list item, or end the list", "항목"),
    ("ShortcutGuide", "Move a list item in or out", "항목"),
    ("ShortcutRows", "Stickle cannot set shortcuts on this desktop", "지정"),
    ("ShortcutRows", "Stickle cannot set shortcuts on this desktop", "실행"),
    ("StickleWindow", "Closing this window quits Stickle", "실행"),
    ("WhatsNew", "# What's new in Stickle 0.5", "Sticky Notes"),
)


def korean_messages() -> list[tuple[str, str, str]]:
    """(context, English, Korean) of every string in use."""
    found: list[tuple[str, str, str]] = []
    for context in ET.parse(KOREAN).getroot().iter("context"):
        name = context.findtext("name") or ""
        for message in context.iter("message"):
            translation = message.find("translation")
            if translation is None or translation.get("type") in ("obsolete", "vanished"):
                continue
            found.append((name, message.findtext("source") or "", "".join(translation.itertext())))
    return found


def test_korean_uses_plain_words_and_no_one_elses_trademark() -> None:
    found = [
        (context, source, word)
        for context, source, text in korean_messages()
        for word in (*HARD_WORDS, *TRADEMARKS)
        if word in text or (word in TRADEMARKS and word in source)
    ]

    def same(source: str, listed: str) -> bool:
        # A long string by its start; a short one whole ("Unlock" is not "Unlock your notes").
        return source == listed or (len(listed) >= 25 and source.startswith(listed))

    def allowed(context: str, source: str, word: str) -> bool:
        return any(c == context and same(source, s) and w == word for c, s, w in ALLOWED)

    assert [(c, s[:60], w) for c, s, w in found if not allowed(c, s, w)] == []
    gone = [
        (context, listed, word)
        for context, listed, word in ALLOWED
        if not any(c == context and same(s, listed) and w == word for c, s, w in found)
    ]
    assert gone == []  # reworded: take it off the list


@pytest.fixture(scope="module")
def extracted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the project with its strings extracted afresh from the current
    source, once for the tests here: the script starts several programs."""
    project = copy_project(tmp_path_factory.mktemp("project"))
    run_script(root=project)
    return project


def test_every_ui_string_has_a_korean_translation(extracted: Path) -> None:
    # A new string without Korean shows up here.
    messages = ET.parse(extracted / "i18n" / KOREAN.name).getroot().iter("message")
    missing = [
        message.findtext("source")
        for message in messages
        if (translation := message.find("translation")) is None
        or translation.get("type") is not None
        # Plural messages keep their text in one or more numerusform elements.
        or not "".join(translation.itertext()).strip()
    ]
    assert missing == []


def test_committed_korean_file_is_up_to_date(extracted: Path) -> None:
    assert (extracted / "i18n" / KOREAN.name).read_bytes() == KOREAN.read_bytes()


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
