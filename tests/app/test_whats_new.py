"""After an update, a note on what changed: once per version, never at first start."""

import secrets
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from stickle.app.i18n import Translations
from stickle.app.whats_new import COLOR, give_news, news_note_id
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store
from stickle.data.settings import NEWS_SEEN, Settings

KEY = secrets.token_bytes(32)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[NoteRepository, Settings]]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield NoteRepository(connection), Settings(connection)
    connection.close()


def test_the_first_start_has_no_news_note(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store

    assert give_news(notes, settings, first_start=True, version="0.5.0") is None

    assert notes.all() == []
    assert settings.get(NEWS_SEEN) == "0.5.0"


def test_notes_from_before_0_5_get_the_news_once(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    notes.create("a note from 0.4")  # 0.4 kept no record of the version

    note_id = give_news(notes, settings, first_start=False, version="0.5.0")

    assert note_id is not None and note_id == news_note_id("0.5.0")
    note = notes.get(note_id)
    assert note is not None and note.color == COLOR and not note.hidden
    assert note.body.startswith("# What's new in Stickle 0.5")
    assert "https://stickle.linkro.co/en/guide" in note.body
    assert settings.get(NEWS_SEEN) == "0.5.0"
    # Started again: no second note.
    assert give_news(notes, settings, first_start=False, version="0.5.0") is None
    assert len(notes.all()) == 2


def test_a_deleted_news_note_does_not_come_back(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    note_id = give_news(notes, settings, first_start=False, version="0.5.0")
    assert note_id is not None
    notes.delete(note_id)

    assert give_news(notes, settings, first_start=False, version="0.5.0") is None
    # Even with the record lost: the note in the trash is the same note.
    settings.set(NEWS_SEEN, None)
    assert give_news(notes, settings, first_start=False, version="0.5.0") is None
    assert [n.id for n in notes.visible()] == []


def test_a_fix_release_brings_nothing_new(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    settings.set(NEWS_SEEN, "0.5.0")

    assert give_news(notes, settings, first_start=False, version="0.5.1") is None
    assert notes.all() == []


def test_an_older_stickle_on_newer_notes_says_nothing(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    settings.set(NEWS_SEEN, "0.6.0")

    assert give_news(notes, settings, first_start=False, version="0.5.0") is None
    assert notes.all() == []
    assert settings.get(NEWS_SEEN) == "0.6.0"


def test_a_version_with_nothing_to_tell_is_only_remembered(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    settings.set(NEWS_SEEN, "0.5.0")

    assert give_news(notes, settings, first_start=False, version="9.9.0") is None
    assert notes.all() == []
    assert settings.get(NEWS_SEEN) == "9.9.0"


def test_the_news_is_in_the_interface_language(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    translations.apply("ko")

    note_id = give_news(notes, settings, first_start=False, version="0.5.0")

    assert note_id is not None
    note = notes.get(note_id)
    assert note is not None
    assert note.body.startswith("# Stickle 0.5에서 바뀐 점")
    assert "https://stickle.linkro.co/ko/guide" in note.body


def test_from_0_5_to_0_6_the_news_of_0_6_comes_once(
    translations: Translations, store: tuple[NoteRepository, Settings]
) -> None:
    notes, settings = store
    settings.set(NEWS_SEEN, "0.5.1")
    translations.apply("ko")

    note_id = give_news(notes, settings, first_start=False, version="0.6.0")

    assert note_id is not None and note_id == news_note_id("0.6.0")
    assert note_id != news_note_id("0.5.1")
    note = notes.get(note_id)
    assert note is not None and note.color == COLOR
    assert note.body.startswith("# Stickle 0.6에서 바뀐 점")
    assert "Stickle 관리자" in note.body and "https://stickle.linkro.co/ko/guide" in note.body
    assert give_news(notes, settings, first_start=False, version="0.6.0") is None
    assert len(notes.all()) == 1


def test_the_same_version_has_the_same_note_on_every_device() -> None:
    assert news_note_id("0.5.0") == news_note_id("0.5.0-preview.1") == news_note_id("0.5.2")
    assert news_note_id("0.5.0") != news_note_id("0.6.0")


def test_a_database_error_does_not_stop_the_start(
    translations: Translations,
    store: tuple[NoteRepository, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes, settings = store

    def fail(*_args: object) -> None:
        raise apsw.FullError("disk full")

    monkeypatch.setattr(NoteRepository, "create", fail)

    assert give_news(notes, settings, first_start=False, version="0.5.0") is None
    # Not tried again at every start.
    assert settings.get(NEWS_SEEN) == "0.5.0"
