"""Every visible text must go through a translation function.

All strings are extracted afresh from the source and given a fake translation
that is longer and marked with brackets and accents. After switching to it at
run time, any visible text without the marker was either never passed through
tr() or is not re-applied when the language changes.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QAbstractButton, QLabel, QListWidget, QWidget
from pytestqt.qtbot import QtBot
from update_translations import compile_to, extract

from stickle.app.application import NoteManager
from stickle.app.first_run import FirstRunDialog
from stickle.app.i18n import Translations
from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import KINDS, Problem, RecoveryDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog, RecoveryKeyDialog
from stickle.app.stickle_window import StickleWindow
from stickle.app.tray import Tray
from stickle.data.schema import NotesDiff

ACCENTED = str.maketrans("aeiouAEIOUcnst", "åëïöüÅËÏÖÜçñşŧ")
# Texts that are not translated on purpose.
RECOVERY_KEY = "K7QM-2HXP-9RTV-4WJD-8FNB-3YCS-6GKA-M2Q7"
UNTRANSLATED = {"", "Stickle", "English", "한국어"}


def pseudo(text: str) -> str:
    # Placeholders (%1, %n) stay as they are, or Qt could no longer fill them in.
    parts = re.split(r"(%n|%\d)", text)
    body = "".join(p if re.fullmatch(r"%n|%\d", p) else p.translate(ACCENTED) for p in parts)
    return f"[{body}{'~' * (len(text) // 3 + 1)}]"


def make_pseudo_translation(folder: Path) -> None:
    ts_file = folder / "stickle_fr.ts"
    extract(ts_file)
    tree = ET.parse(ts_file)
    for message in tree.getroot().iter("message"):
        translation = message.find("translation")
        assert translation is not None
        translation.attrib.pop("type", None)
        text = pseudo(message.findtext("source") or "")
        if message.get("numerus") == "yes":
            for form in translation.findall("numerusform"):
                translation.remove(form)
            ET.SubElement(translation, "numerusform").text = text
        else:
            translation.text = text
    tree.write(ts_file, encoding="utf-8", xml_declaration=True)
    compile_to(ts_file, folder / "stickle_fr.qm")


def visible_texts(widget: QWidget) -> list[str]:
    texts: list[str] = []
    for item in [widget, *widget.findChildren(QWidget)]:
        texts += [item.windowTitle(), item.toolTip(), item.accessibleName()]
        texts += [action.text() for action in item.actions()]
        if isinstance(item, QLabel | QAbstractButton):
            texts.append(item.text())
        if isinstance(item, QListWidget):
            texts += [item.item(row).text() for row in range(item.count())]
    return texts


def test_every_visible_text_is_translated(qtbot: QtBot, tmp_path: Path) -> None:
    make_pseudo_translation(tmp_path)
    translations = Translations(tmp_path)
    translations.apply("en")
    manager = NoteManager()
    tray = Tray(manager.new_note, lambda: None, translations, manager)
    note = manager.new_note()
    stickle_window = StickleWindow(manager, translations, lambda: None)
    qtbot.addWidget(stickle_window)
    try:
        # Switched after the windows exist, so re-application is covered too.
        translations.apply("fr")
        menu = tray.contextMenu()
        assert menu is not None
        texts = visible_texts(note) + visible_texts(note.menu) + visible_texts(menu)
        texts += visible_texts(tray.language_menu) + visible_texts(tray.hidden_menu)
        texts += visible_texts(stickle_window) + visible_texts(stickle_window.about)
        box = stickle_window.language_box
        texts += [box.itemText(index) for index in range(box.count())]

        untranslated = [text for text in texts if text not in UNTRANSLATED and "~]" not in text]
        assert untranslated == []
    finally:
        note.close()
        translations.apply("en")
        QLocale.setDefault(QLocale.system())


def test_every_startup_dialog_text_is_translated(qtbot: QtBot, tmp_path: Path) -> None:
    make_pseudo_translation(tmp_path)
    translations = Translations(tmp_path)
    translations.apply("en")
    dialogs: list[QWidget] = [PasswordDialog(create, lambda _: None) for create in (True, False)]
    diff = NotesDiff(missing=["회의록"], changed=[""], added=["새 메모"])
    dialogs += [
        RecoveryDialog(Problem(kind, diff=diff), tmp_path, export=lambda _: None)  # pyright: ignore[reportArgumentType]
        for kind in KINDS
    ]
    dialogs += [
        PasswordDialog(True, lambda _: None, after_recovery=True),
        PasswordDialog(False, lambda _: None, can_recover=True),
        EnterRecoveryKeyDialog(lambda _: None),
        RecoveryKeyDialog(RECOVERY_KEY),
        FirstRunDialog(RECOVERY_KEY, True, True),
    ]
    try:
        translations.apply("fr")
        for dialog in dialogs:
            # The recovery key itself is not text to translate.
            texts = [t for t in visible_texts(dialog) if t not in UNTRANSLATED | {RECOVERY_KEY}]
            # Note titles in the list are the user's own text around a translated label.
            untranslated = [t for t in texts if "~]" not in t]
            assert untranslated == [], type(dialog).__name__
    finally:
        translations.apply("en")
        QLocale.setDefault(QLocale.system())
