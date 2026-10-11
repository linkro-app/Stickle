"""The icons on a note's left: categories', the app's own, and the tape."""

from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot

from stickle.app.category_icons import ICONS, state_pixmap
from stickle.core.colors import ICON_CONTRAST, Rgb, clear_on, contrast
from stickle.core.labels import CATEGORY_ICONS

INK = QColor("#202020")


def inked(image: QImage) -> int:
    return sum(
        image.pixelColor(x, y).alpha() > 60
        for x in range(image.width())
        for y in range(image.height())
    )


def test_every_category_icon_is_kept_with_the_app_and_draws(qtbot: QtBot) -> None:
    assert (ICONS / "LICENSE").is_file()  # Lucide's, kept with its icons
    for name in CATEGORY_ICONS:
        assert (ICONS / f"{name}.svg").is_file(), name
        assert inked(state_pixmap(name, INK, None, 16, 2.0).toImage()) > 20, name


def test_an_icon_this_version_lacks_shows_as_the_tag(qtbot: QtBot) -> None:
    unknown = state_pixmap("hologram", INK, None, 16, 2.0).toImage()
    assert unknown == state_pixmap("tag", INK, None, 16, 2.0).toImage()


def test_no_category_shows_the_apps_icon_and_tape_adds_to_either(qtbot: QtBot) -> None:
    app_mark = state_pixmap(None, INK, None, 16, 2.0).toImage()
    assert inked(app_mark) > 20
    assert app_mark != state_pixmap("tag", INK, None, 16, 2.0).toImage()
    for category in (None, "house"):
        bare = inked(state_pixmap(category, INK, None, 16, 2.0).toImage())
        taped = inked(state_pixmap(category, INK, INK, 16, 2.0).toImage())
        assert taped > bare


def test_a_pale_colour_is_deepened_until_it_shows_and_keeps_its_hue() -> None:
    pale, note = Rgb(170, 220, 200), Rgb(195, 215, 255)
    shown = clear_on(pale, note)
    assert contrast(shown, note) >= ICON_CONTRAST
    assert shown.green > shown.red  # still a green
    dark = Rgb(58, 58, 58)
    assert contrast(clear_on(Rgb(90, 80, 40), dark), dark) >= ICON_CONTRAST
    ink = Rgb(32, 32, 32)
    assert clear_on(ink, note) == ink  # clear already: unchanged
