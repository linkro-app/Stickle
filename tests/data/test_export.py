import secrets
from pathlib import Path

import pytest

from stickle.data.database import KEY_BYTES, open_database
from stickle.data.export import ExportError, export_markdown, file_stem
from stickle.data.notes import NoteRepository
from stickle.data.schema import open_store

KEY = secrets.token_bytes(KEY_BYTES)
NOW = "2026-09-26T10:45:12.000Z"


def files(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes().decode("utf-8")
        for p in sorted(folder.rglob("*.md"))
    }


def test_every_note_becomes_a_file_with_its_exact_text(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_store(db, KEY)
    repo = NoteRepository(connection)
    kept = repo.create("회의록\r\n- 내일까지\n😀")
    gone = repo.create("장보기")
    repo.delete(gone.id)
    connection.close()
    database_before = db.read_bytes()

    result = export_markdown(db, KEY, tmp_path / "out", clock=lambda: NOW)

    assert result.folder == tmp_path / "out" / "Stickle notes 20260926T104512"
    assert (result.notes, result.deleted) == (1, 1)
    assert files(result.folder) == {
        f"회의록 ({kept.id[:8]}).md": "회의록\r\n- 내일까지\n😀",
        f"deleted/장보기 ({gone.id[:8]}).md": "장보기",
    }
    assert db.read_bytes() == database_before


def test_a_secret_note_is_written_as_its_title_still_encrypted(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_store(db, KEY)
    repo = NoteRepository(connection)
    secret = repo.create("통장 비밀번호")
    repo.make_secret(secret.id, "은행", b"sealed")
    untitled = repo.create("다른 글")
    repo.make_secret(untitled.id, "", b"sealed")
    connection.close()

    result = export_markdown(db, KEY, tmp_path, clock=lambda: NOW, locked_line="암호화된 비밀 메모")

    assert files(result.folder) == {
        f"은행 ({secret.id[:8]}).md": "# 은행\n\n암호화된 비밀 메모\n",
        f"note ({untitled.id[:8]}).md": "암호화된 비밀 메모\n",
    }


def test_exporting_twice_never_overwrites(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_store(db, KEY)
    NoteRepository(connection).create("a")
    connection.close()

    first = export_markdown(db, KEY, tmp_path, clock=lambda: NOW)
    second = export_markdown(db, KEY, tmp_path, clock=lambda: NOW)

    assert first.folder != second.folder
    assert len(files(first.folder)) == len(files(second.folder)) == 1


def test_a_database_of_another_version_is_read_by_id_and_text(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_database(db, KEY)
    with connection:
        connection.execute("CREATE TABLE notes (id TEXT, body TEXT, future_column BLOB)")
        connection.execute("INSERT INTO notes VALUES ('n1', '미래의 메모', x'00')")
        connection.pragma("user_version", 42)
    connection.close()

    result = export_markdown(db, KEY, tmp_path, clock=lambda: NOW)

    assert files(result.folder) == {"미래의 메모 (n1).md": "미래의 메모"}


def test_a_database_without_notes_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_database(db, KEY)
    connection.execute("CREATE TABLE something (x)")
    connection.close()

    with pytest.raises(ExportError):
        export_markdown(db, KEY, tmp_path)


@pytest.mark.parametrize(
    ("body", "stem"),
    [
        ("회의록", "회의록 (abcdefgh)"),
        ("", "note (abcdefgh)"),
        ("\n\n  \n두 번째 줄이 제목", "두 번째 줄이 제목 (abcdefgh)"),
        ('a<b>c:d"e/f\\g|h?i*j', "a b c d e f g h i j (abcdefgh)"),
        ("CON", "CON (abcdefgh)"),
        ("끝에 점...", "끝에 점 (abcdefgh)"),
        ("가" * 80, "가" * 50 + " (abcdefgh)"),
        ("tab\there", "tab here (abcdefgh)"),
        # Not in Unicode yet: macOS refuses it in a name.
        ("가" + chr(0x12544) + "나", "가나 (abcdefgh)"),
        (chr(0x12544), "note (abcdefgh)"),
    ],
)
def test_file_names_are_safe_on_every_system(body: str, stem: str) -> None:
    assert file_stem(body, "abcdefgh-1234") == stem


def test_same_first_line_gets_distinct_files(tmp_path: Path) -> None:
    db = tmp_path / "notes.db"
    connection = open_database(db, KEY)
    with connection:
        connection.execute("CREATE TABLE notes (id TEXT, body TEXT)")
        connection.execute("INSERT INTO notes VALUES ('same', 'a'), ('same', 'a')")
    connection.close()

    result = export_markdown(db, KEY, tmp_path, clock=lambda: NOW)

    assert sorted(files(result.folder)) == ["a (same) 2.md", "a (same).md"]
