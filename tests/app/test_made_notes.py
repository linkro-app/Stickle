"""The notes the app makes, as this version writes them, are known as the
app's own (stickle.core.made_notes) in every language: a text changed, or a
translation of one, fails here until its digest is added."""

import pytest

from stickle.app.first_run import sample_note
from stickle.app.i18n import Translations
from stickle.app.whats_new import news, news_note_id
from stickle.core import made_notes

LINES = [f"{major}.{minor}" for major in (0, 1) for minor in range(30)]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_this_versions_welcome_note_is_known(translations: Translations, language: str) -> None:
    translations.apply(language)
    assert made_notes.welcome(sample_note())


@pytest.mark.parametrize("language", ["en", "ko"])
def test_every_whats_new_note_this_version_writes_is_known(
    translations: Translations, language: str
) -> None:
    translations.apply(language)
    written = 0
    for line in LINES:
        text = news(f"{line}.0")
        if text is None:
            continue
        written += 1
        assert line in made_notes.NEWS, line
        assert made_notes.news(news_note_id(f"{line}.0"), text), line
    assert written >= 2


def test_a_welcome_note_changed_is_not_the_apps_own(translations: Translations) -> None:
    text = sample_note()
    assert not made_notes.welcome(text.replace("- [ ]", "- [x]", 1))
    assert not made_notes.welcome(text + "\n")


def test_whats_new_is_known_only_under_its_own_id(translations: Translations) -> None:
    text = news("0.6.0")
    assert text is not None
    assert not made_notes.news(news_note_id("0.5.0"), text)
    assert not made_notes.news("a70a61ea-4ead-4c2a-8970-551edc292be5", text)
    assert made_notes.is_news(news_note_id("0.6.0"))
