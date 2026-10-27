"""Exporting everything: Markdown files to read anywhere, and JSON with all the data."""

import base64
import errno
import json
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import apsw
import pytest

from stickle.core.layout import Place
from stickle.crypto.secret_notes import SecretNoteError, open_text, seal_text
from stickle.data.database import KEY_BYTES
from stickle.data.export import JSON_NAME, SecretExport, export_all
from stickle.data.labels import LabelRepository
from stickle.data.layouts import LayoutRepository
from stickle.data.notes import NoteRepository
from stickle.data.schema import latest_version, open_store
from stickle.data.settings import DEFAULT_NOTE_COLOR, Settings

KEY = secrets.token_bytes(KEY_BYTES)
NOW = "2026-10-18T03:15:00.000Z"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


def markdown(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes().decode("utf-8")
        for p in sorted(folder.rglob("*.md"))
    }


type Rows = dict[str, list[dict[str, object]]]


def exported(folder: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads((folder / JSON_NAME).read_text(encoding="utf-8")))


def tables_of(document: dict[str, object]) -> Rows:
    return cast(Rows, document["tables"])


def test_notes_become_files_with_their_exact_text(
    connection: apsw.Connection, tmp_path: Path
) -> None:
    notes = NoteRepository(connection)
    kept = notes.create("회의록\r\n- [ ] 내일까지\n😀")
    gone = notes.create("장보기")
    notes.delete(gone.id)

    result = export_all(connection, tmp_path / "out", "0.5.0", clock=lambda: NOW)

    assert (result.notes, result.deleted) == (1, 1)
    assert result.folder.name.startswith("Stickle export 2026-10-")
    assert markdown(result.folder) == {
        f"회의록 ({kept.id[:8]}).md": "회의록\r\n- [ ] 내일까지\n😀",
        f"trash/장보기 ({gone.id[:8]}).md": "장보기",
    }


def test_the_json_holds_every_table_of_the_users_data(
    connection: apsw.Connection, tmp_path: Path
) -> None:
    """A table added by a later version is exported without anyone remembering to."""
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }

    document = exported(export_all(connection, tmp_path, "0.5.0", clock=lambda: NOW).folder)

    data = tables_of(document)
    assert set(data) == {t for t in tables if not t.startswith(("notes_fts", "sqlite_"))}
    assert {"notes", "note_layouts", "categories", "marks", "note_marks"} <= set(data)
    assert document["schema_version"] == latest_version()
    assert document["app_version"] == "0.5.0"
    assert document["exported_at"] == NOW


def test_the_json_keeps_what_the_notes_carry(connection: apsw.Connection, tmp_path: Path) -> None:
    notes = NoteRepository(connection)
    labels = LabelRepository(connection)
    note = notes.create("일정", color="sky")
    category = labels.create_category("회사", "blue", "briefcase")
    notes.set_category(note.id, category.id)
    notes.set_mark(note.id, "urgent", True)
    notes.set_opacity(note.id, 0.8)
    LayoutRepository(connection).save(
        note.id, {"main": Place(1, "DELL U2720Q", 0.25, 0.5, 300, 200)}
    )
    Settings(connection).set(DEFAULT_NOTE_COLOR, "pink")

    data = tables_of(exported(export_all(connection, tmp_path, "0.5.0", clock=lambda: NOW).folder))

    (row,) = [r for r in data["notes"] if r["id"] == note.id]
    assert (row["body"], row["color"], row["label"], row["opacity"]) == (
        "일정",
        "sky",
        category.id,
        0.8,
    )
    assert {"note_id": note.id, "mark_id": "urgent"} in data["note_marks"]
    assert [c["name"] for c in data["categories"]] == ["회사"]
    (layout,) = data["note_layouts"]
    assert (layout["rel_x"], layout["width"]) == (0.25, 300)
    assert any(s["key"] == "default_color" for s in data["shared_settings"])


def test_nothing_is_overwritten(connection: apsw.Connection, tmp_path: Path) -> None:
    first = export_all(connection, tmp_path, "0.5.0", clock=lambda: NOW).folder
    second = export_all(connection, tmp_path, "0.5.0", clock=lambda: NOW).folder

    assert first != second
    assert second.name == f"{first.name} 2"


def test_a_name_the_file_system_refuses_gives_way_to_a_plain_one(
    connection: apsw.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = NoteRepository(connection).create("회의록\n내일까지")
    real_open: Callable[..., Any] = Path.open

    def refusing(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name.startswith("회의록"):
            raise OSError(errno.EILSEQ, "Illegal byte sequence", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refusing)
    result = export_all(connection, tmp_path, "0.5.0", clock=lambda: NOW)

    assert markdown(result.folder) == {f"note ({note.id[:8]}).md": "회의록\n내일까지"}


# Secret notes: as they are stored, or opened with their key

SECRET_KEY = secrets.token_bytes(32)
SECRET_TEXT = "통장 비밀번호 힌트 zqxjkv"
KEY_COPIES: dict[str, object] = {"format": 1, "slots": {"secret_password": {"sealed": "…"}}}


def with_a_secret_note(connection: apsw.Connection) -> str:
    notes = NoteRepository(connection)
    note = notes.create(SECRET_TEXT)
    notes.make_secret(note.id, "은행", seal_text(SECRET_KEY, note.id, SECRET_TEXT))
    notes.create("장보기")
    return note.id


def everything_written(folder: Path) -> str:
    return "".join(p.read_bytes().decode("utf-8") for p in folder.rglob("*") if p.is_file())


def test_left_encrypted_only_its_title_shows_and_its_key_goes_along(
    connection: apsw.Connection, tmp_path: Path
) -> None:
    note_id = with_a_secret_note(connection)
    secret = SecretExport(locked_line="잠긴 비밀 메모", key_copies=KEY_COPIES)

    result = export_all(connection, tmp_path, "0.6.0", clock=lambda: NOW, secret=secret)

    assert result.still_encrypted == 1
    files = markdown(result.folder)
    assert files[f"은행 ({note_id[:8]}).md"] == "# 은행\n\n잠긴 비밀 메모\n"
    assert "zqxjkv" not in everything_written(result.folder)
    document = exported(result.folder)
    assert document["secret_notes_key"] == KEY_COPIES
    (row,) = [r for r in tables_of(document)["notes"] if r["id"] == note_id]
    assert row["body"] == "" and row["secret_title"] == "은행"
    sealed = base64.b64decode(cast(dict[str, str], row["secret_body"])["base64"])
    assert open_text(SECRET_KEY, note_id, sealed) == SECRET_TEXT  # opens later as stored


def test_opened_its_text_is_written_in_the_clear(
    connection: apsw.Connection, tmp_path: Path
) -> None:
    note_id = with_a_secret_note(connection)

    result = export_all(
        connection, tmp_path, "0.6.0", clock=lambda: NOW, secret=SecretExport(key=SECRET_KEY)
    )

    assert result.still_encrypted == 0
    assert markdown(result.folder)[f"은행 ({note_id[:8]}).md"] == SECRET_TEXT
    document = exported(result.folder)
    assert "secret_notes_key" not in document
    (row,) = [r for r in tables_of(document)["notes"] if r["id"] == note_id]
    assert row["body"] == SECRET_TEXT and row["secret_body"] is None
    assert row["secret_title"] == "은행"  # it was a secret note


def test_with_the_wrong_key_nothing_is_written(connection: apsw.Connection, tmp_path: Path) -> None:
    with_a_secret_note(connection)

    with pytest.raises(SecretNoteError):
        export_all(
            connection,
            tmp_path / "out",
            "0.6.0",
            clock=lambda: NOW,
            secret=SecretExport(key=secrets.token_bytes(32)),
        )

    assert not (tmp_path / "out").exists()
