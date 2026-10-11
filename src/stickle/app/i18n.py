"""Switch the UI language while the app runs.

Installing or removing a translator makes Qt send a LanguageChange event to
every widget, and each window re-applies its texts. Objects that are not
widgets (the tray icon) listen to the changed signal instead.
"""

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QLocale, QObject, QTranslator, Signal

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"

# Language names stay in their own language, so a user stuck in a language they
# cannot read still finds their own. None follows the system.
LANGUAGES: list[tuple[str | None, str]] = [(None, ""), ("en", "English"), ("ko", "한국어")]


class Translations(QObject):
    changed = Signal()

    def __init__(self, directory: Path = TRANSLATIONS_DIR) -> None:
        super().__init__()
        self._directory = directory
        self._installed: list[QTranslator] = []
        self._language: str | None = None

    @property
    def language(self) -> str | None:
        """The chosen language code, or None when following the system."""
        return self._language

    def apply(self, language: str | None) -> None:
        for translator in self._installed:
            QCoreApplication.removeTranslator(translator)
        self._installed.clear()

        locale = QLocale.system() if language is None else QLocale(language)
        QLocale.setDefault(locale)
        # Qt's own strings (context menus, dialogs) first, then ours; English is
        # the source language, and its file only holds the plural forms.
        for name in ("qtbase", "stickle"):
            translator = QTranslator(self)
            if translator.load(locale, name, "_", str(self._directory)):
                QCoreApplication.installTranslator(translator)
                self._installed.append(translator)
        self._language = language
        # Qt queues the LanguageChange events; deliver them now, so every window
        # has switched by the time this returns.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LanguageChange)
        self.changed.emit()
