"""Every sync window and state, in both languages, holds together as far as a
machine can tell. And the checks do find what they look for.

With STICKLE_SCREENS naming a folder, the windows are kept there as pictures and
compared with those of the last time. To see the system's own fonts, run it on the
system's own display too (QT_QPA_PLATFORM=windows, xcb or cocoa)."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QLocale, QPoint, Qt, QTranslator
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot
from screen_album import Album
from screen_checks import cut_off, out_of_order, outside, overlapping, problems, unreached
from sync_screens import SCENES, FakeSyncing, Scene, Stage, shown_by
from update_translations import TS_DIR, compile_to

from stickle.app.i18n import Translations
from stickle.app.stickle_window import ElidedButton
from stickle.sync.outcome import Outcome, Stopped

LANGUAGES = ("ko", "en")
ALBUM_FOLDER = os.environ.get("STICKLE_SCREENS")
TOOLTIP_LONGEST = 127  # characters Windows shows of a tray icon's
WINDOWS_FONTS = ("segoeui.ttf", "malgun.ttf")


@pytest.fixture(scope="module", autouse=True)
def letters_of_real_width(qapp: QApplication) -> Iterator[None]:
    """Off-screen on Windows, Qt finds no fonts: every letter is a wide box, and
    what fits on a screen cannot be told. The system's own are brought in."""
    if QFontDatabase.families():
        yield
        return
    fonts = Path(os.environ["WINDIR"]) / "Fonts"
    added = [QFontDatabase.addApplicationFont(str(fonts / name)) for name in WINDOWS_FONTS]
    assert -1 not in added
    before = QApplication.font()
    QApplication.setFont(QFont("Segoe UI", 9))
    yield
    QApplication.setFont(before)
    for font in added:
        QFontDatabase.removeApplicationFont(font)


@pytest.fixture(scope="module")
def both_languages(tmp_path_factory: pytest.TempPathFactory, translations_dir: Path) -> Path:
    """As the app has them: English too, whose file holds its plural forms."""
    folder = tmp_path_factory.mktemp("both_languages")
    for file in translations_dir.iterdir():
        shutil.copy2(file, folder)
    compile_to(TS_DIR / "stickle_en.ts", folder / "stickle_en.qm")
    return folder


@pytest.fixture
def translations(qtbot: QtBot, both_languages: Path) -> Iterator[Translations]:
    made = Translations(both_languages)
    yield made
    for translator in made.findChildren(QTranslator):
        QCoreApplication.removeTranslator(translator)
    QLocale.setDefault(QLocale.system())


@pytest.fixture(scope="module")
def album() -> Iterator[Album | None]:
    if ALBUM_FOLDER is None:
        yield None
        return
    kept = Album(Path(ALBUM_FOLDER))
    kept.begin()
    yield kept
    print(kept.end())


def on_screen(top: QWidget) -> None:
    """As it is once shown, laid out and all, without getting in anyone's way."""
    if ALBUM_FOLDER is not None:
        top.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    if isinstance(top, QMenu):
        top.popup(QPoint(0, 0))
    else:
        top.show()
    for _ in range(3):
        QApplication.processEvents()


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("scene", SCENES, ids=lambda scene: scene.name)
def test_a_sync_screen_holds_together(
    translations: Translations, album: Album | None, language: str, scene: Scene
) -> None:
    translations.apply(language)
    stage = Stage(translations)
    try:
        top = scene.build(stage)
        on_screen(top)
        if album is not None:
            album.take(f"{language}-{scene.name}", top)
        assert problems(top, scene.keyboard) == {}
    finally:
        stage.end()


def test_the_scenes_have_names_of_their_own() -> None:
    assert len({scene.name for scene in SCENES}) == len(SCENES)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("why", [*Stopped, None], ids=lambda why: getattr(why, "name", "full"))
def test_the_tray_tooltip_is_said_whole(
    translations: Translations, language: str, why: Stopped | None
) -> None:
    translations.apply(language)
    stage = Stage(translations)
    try:
        outcome = Outcome(full=True) if why is None else Outcome(why)
        tray, _ = shown_by(stage, FakeSyncing().round_was(outcome))
        assert 0 < len(tray.toolTip()) <= TOOLTIP_LONGEST
    finally:
        stage.end()


def window_of(qtbot: QtBot, width: int = 400, height: int = 200) -> QWidget:
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(width, height)
    return window


def shown(window: QWidget) -> QWidget:
    window.show()
    QApplication.processEvents()
    return window


LONG = "a line of text a good deal longer than the room it is given here"


def test_all_is_well_with_a_window_laid_out_as_it_should(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    layout = QVBoxLayout(window)
    label = QLabel(LONG, window)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(QPushButton("One", window))
    layout.addWidget(QPushButton("Two", window))
    assert problems(shown(window)) == {}


def test_a_label_too_narrow_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    QLabel(LONG, window).setGeometry(0, 0, 40, 30)
    assert cut_off(shown(window))


def test_a_wrapped_label_too_low_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    label = QLabel(LONG, window)
    label.setWordWrap(True)
    label.setGeometry(0, 0, 80, 20)
    assert cut_off(shown(window))


def test_a_wrapped_label_with_the_room_it_takes_is_let_be(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    label = QLabel(LONG, window)
    label.setWordWrap(True)
    label.setGeometry(0, 0, 80, 180)
    assert cut_off(shown(window)) == []


def test_a_word_wider_than_its_wrapped_label_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    label = QLabel("x" * 200, window)
    label.setWordWrap(True)
    label.setGeometry(0, 0, 80, 100)
    assert cut_off(shown(window))


def test_a_button_too_narrow_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    QPushButton(LONG, window).setGeometry(0, 0, 40, 30)
    assert cut_off(shown(window))


def test_a_combo_box_too_narrow_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    box = QComboBox(window)
    box.addItem(LONG)
    box.setGeometry(0, 0, 40, 30)
    assert cut_off(shown(window))


def test_a_button_that_shortens_its_own_text_is_let_be(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    button = ElidedButton(window)
    button.set_full_text(LONG)
    button.setGeometry(0, 0, 40, 30)
    assert cut_off(shown(window)) == []


def test_parts_on_top_of_one_another_are_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    QLabel("one", window).setGeometry(0, 0, 100, 30)
    QLabel("two", window).setGeometry(50, 10, 100, 30)
    assert overlapping(shown(window))


def test_parts_side_by_side_are_not(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    QLabel("one", window).setGeometry(0, 0, 100, 30)
    QLabel("two", window).setGeometry(100, 0, 100, 30)
    assert overlapping(shown(window)) == []


def test_a_part_past_the_edge_of_the_window_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    QLabel("one", window).setGeometry(380, 0, 100, 30)
    assert outside(shown(window))


def test_a_window_no_small_screen_shows_is_found(qtbot: QtBot) -> None:
    assert outside(shown(window_of(qtbot, 400, 900)))


def test_a_control_tab_does_not_come_to_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    button = QPushButton("mouse only", window)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    assert unreached(shown(window))


def test_a_disabled_control_is_not_counted(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    button = QPushButton("not now", window)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setEnabled(False)
    assert unreached(shown(window)) == []


def test_tab_going_otherwise_than_it_reads_is_found(qtbot: QtBot) -> None:
    window = window_of(qtbot)
    one, two, three = (QPushButton(name, window) for name in ("one", "two", "three"))
    for at, button in enumerate((one, two, three)):
        button.setGeometry(0, at * 40, 100, 30)
    assert out_of_order(shown(window)) == []
    QWidget.setTabOrder(one, three)
    assert out_of_order(window)


def filled(qtbot: QtBot, colour: str, width: int = 60) -> QWidget:
    window = window_of(qtbot, width, 40)
    window.setStyleSheet(f"background: {QColor(colour).name()}")
    window.setAutoFillBackground(True)
    return shown(window)


def test_the_first_album_has_nothing_to_compare_with(qtbot: QtBot, tmp_path: Path) -> None:
    album = Album(tmp_path)
    album.begin()
    album.take("one", filled(qtbot, "red"))
    assert album.end().startswith("First album: 1 picture")
    assert (tmp_path / "now" / "one.png").exists()
    assert not (tmp_path / "changed").exists()


def test_pictures_as_they_were_are_said_to_be(qtbot: QtBot, tmp_path: Path) -> None:
    window = filled(qtbot, "red")
    album = Album(tmp_path)
    album.begin()
    album.take("one", window)
    album.end()
    album.begin()
    album.take("one", window)
    assert album.end().startswith("1 the same, 0 changed, 0 new, 0 gone.")
    assert not (tmp_path / "changed").exists()


def test_a_changed_picture_is_shown_beside_the_old_one(qtbot: QtBot, tmp_path: Path) -> None:
    album = Album(tmp_path)
    album.begin()
    album.take("kept", filled(qtbot, "green"))
    album.take("changed", filled(qtbot, "red"))
    album.take("gone", filled(qtbot, "green"))
    album.end()

    album.begin()
    album.take("kept", filled(qtbot, "green"))
    album.take("changed", filled(qtbot, "blue", 80))
    album.take("new", filled(qtbot, "green"))
    said = album.end().splitlines()

    assert said[0] == "1 the same, 1 changed, 1 new, 1 gone."
    assert said[1:] == ["changed: changed", "new: new", "gone: gone"]
    assert (tmp_path / "report.txt").read_text("utf-8").splitlines() == [said[0], *said[1:]]
    pictures = list((tmp_path / "changed").glob("*.png"))
    assert [path.stem for path in pictures] == ["changed"]
    joined = QImage(str(pictures[0]))
    assert joined.width() == 60 + 12 + 80
    assert QColor(joined.pixel(10, 10)) == QColor("red")
    assert QColor(joined.pixel(60 + 12 + 10, 10)) == QColor("blue")
