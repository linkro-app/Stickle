"""A note's earlier texts: kept once for each edit and before any other text
takes its place, 30 days and 50 a note, the same text once; a secret note's
encrypted, following the note when it is made secret or ordinary; gone with the
note, or when the user deletes them."""

import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import count
from pathlib import Path

import apsw
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stickle.core.note import Note, content_hash
from stickle.crypto.secret_notes import open_text, seal_text
from stickle.data.notes import NoteNotSecretError, NoteRepository
from stickle.data.schema import open_store
from stickle.data.versions import KEEP_DAYS, KEEP_PER_NOTE, VersionRepository

KEY = secrets.token_bytes(32)
SECRET_KEY = secrets.token_bytes(32)
MARK = "zqxjkv-plorbit"


class Clock:
    """Stickle's clock, moved on by the test: millisecond stamps, sortable as text."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)

    def __call__(self) -> str:
        return stamp(self.moment)

    def later(self, **delta: float) -> str:
        self.moment += timedelta(**delta)
        return self()


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(tmp_path / "notes.db", KEY)
    yield connection
    connection.close()


@pytest.fixture
def notes(connection: apsw.Connection, clock: Clock) -> NoteRepository:
    return NoteRepository(connection, clock)


@pytest.fixture
def kept(connection: apsw.Connection, clock: Clock) -> VersionRepository:
    return VersionRepository(connection, clock)


def texts(kept: VersionRepository, note_id: str) -> list[str | bytes | None]:
    return [kept.text(note_id, version.id) for version in kept.listed(note_id)]


def now_of(notes: NoteRepository, note_id: str) -> Note:
    note = notes.get(note_id)
    assert note is not None
    return note


def edit(notes: NoteRepository, note_id: str, *saves: str) -> None:
    """One edit, saved as often as autosave would: the first save keeps the text."""
    for number, body in enumerate(saves):
        notes.update_body(note_id, body, keep_earlier=number == 0)


def sealer(note_id: str) -> Callable[[str], bytes]:
    return partial(seal_text, SECRET_KEY, note_id)


def opener(note_id: str) -> Callable[[bytes], str]:
    return partial(open_text, SECRET_KEY, note_id)


def holds_anywhere(connection: apsw.Connection, word: str) -> list[str]:
    """The tables any value of which holds word: a secret note's text must be in
    none, the search index's segments included."""
    found: list[str] = []
    needle = word.encode("utf-8")
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for row in connection.execute(f'SELECT * FROM "{table}"').fetchall():
            for value in row:
                data = value if isinstance(value, bytes) else str(value).encode("utf-8")
                if needle in data:
                    found.append(str(table))
    return found


# One edit, one earlier text


def test_one_edit_keeps_the_text_it_had_once_however_often_it_is_saved(
    notes: NoteRepository, kept: VersionRepository
) -> None:
    note = notes.create("장보기: 우유")
    assert not kept.has_any(note.id)

    edit(
        notes, note.id, "장보기: 우유, 빵", "장보기: 우유, 빵, 달걀", "장보기: 우유, 빵, 달걀, 사과"
    )

    assert texts(kept, note.id) == ["장보기: 우유"]
    assert kept.has_any(note.id)
    edit(notes, note.id, "장보기 끝")
    assert texts(kept, note.id) == ["장보기: 우유, 빵, 달걀, 사과", "장보기: 우유"]


def test_saving_without_a_change_or_a_note_with_no_text_keeps_nothing(
    notes: NoteRepository, kept: VersionRepository
) -> None:
    note = notes.create("")
    edit(notes, note.id, "처음 쓴 글")  # from nothing: nothing to keep
    edit(notes, note.id, "처음 쓴 글")  # no change
    notes.update_body(note.id, "  \n", keep_earlier=True)
    edit(notes, note.id, "다시 쓴 글")  # from spaces only

    assert texts(kept, note.id) == ["처음 쓴 글"]


def test_each_says_when_it_became_the_text_and_where(
    notes: NoteRepository, kept: VersionRepository, clock: Clock
) -> None:
    note = notes.create("첫 글")
    clock.later(minutes=5)
    edit(notes, note.id, "고친 글", "고친 글, 더")
    finished = clock.later(minutes=1)
    notes.update_body(note.id, "고친 글, 더, 끝")  # still the same edit
    received = clock.later(hours=3)
    notes.replace_text(note.id, "노트북에서 쓴 글", came_from="laptop")
    clock.later(days=1)
    edit(notes, note.id, "다시 이 PC에서")

    listed = kept.listed(note.id)
    assert [(v.at, v.came_from, v.restored) for v in listed] == [
        (received, "laptop", False),
        (finished, None, False),  # when this computer's edit last saved it
        (note.updated_at, None, False),  # the first text: from the note's making
    ]
    assert [v.first_line for v in listed] == ["노트북에서 쓴 글", "고친 글, 더, 끝", "첫 글"]


def test_a_text_from_before_this_version_counts_from_the_notes_last_change(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection, clock: Clock
) -> None:
    # Notes written before earlier texts were kept have no time for their text.
    note = notes.create("0.6에서 쓴 글")
    clock.later(hours=1)
    changed = notes.set_color(note.id, "blue")
    connection.execute("UPDATE notes SET text_at = NULL, text_from = NULL")
    clock.later(days=2)

    edit(notes, note.id, "0.7에서 고친 글")

    [version] = kept.listed(note.id)
    assert (version.at, version.came_from) == (changed.updated_at, None)


def test_the_same_text_is_kept_once_and_the_one_it_has_now_is_not_shown(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection
) -> None:
    note = notes.create("가")
    edit(notes, note.id, "나")
    edit(notes, note.id, "가")
    edit(notes, note.id, "나")  # "가" kept again: once, as the newest

    assert texts(kept, note.id) == ["가"]  # "나" is its text now
    rows = connection.execute("SELECT body FROM note_versions ORDER BY id").fetchall()
    assert [body for (body,) in rows] == ["나", "가"]


def test_thirty_days_from_when_it_was_put_aside_and_fifty_a_note(
    notes: NoteRepository, kept: VersionRepository, clock: Clock
) -> None:
    old = notes.create("오래된 글")
    edit(notes, old.id, "그다음 글")
    clock.later(days=KEEP_DAYS - 1, hours=23)
    assert texts(kept, old.id) == ["오래된 글"]
    clock.later(hours=2)
    assert texts(kept, old.id) == []  # not shown past 30 days, put away at start
    assert kept.prune() == 1

    many = notes.create("0")
    for number in range(1, KEEP_PER_NOTE + 6):
        clock.later(minutes=1)
        edit(notes, many.id, str(number))
    shown = texts(kept, many.id)
    assert len(shown) == KEEP_PER_NOTE
    assert shown[0] == str(KEEP_PER_NOTE + 4) and shown[-1] == "5"  # the oldest went


def test_going_back_is_a_new_edit_that_keeps_the_text_put_aside(
    notes: NoteRepository, kept: VersionRepository, clock: Clock
) -> None:
    note = notes.create("처음")
    edit(notes, note.id, "둘째")
    edit(notes, note.id, "셋째")
    first = next(v for v in kept.listed(note.id) if v.first_line == "처음")
    back_at = clock.later(minutes=10)

    restored = notes.restore_version(note.id, first.id)

    assert restored.body == "처음" and restored.change_seq > note.change_seq
    top = kept.listed(note.id)[0]
    assert (top.first_line, top.restored, top.at) == ("셋째", True, back_at)
    notes.restore_version(note.id, top.id)  # and back again
    assert now_of(notes, note.id).body == "셋째"
    assert notes.restore_version(note.id, kept.listed(note.id)[0].id).body == "처음"
    with pytest.raises(KeyError):
        notes.restore_version(note.id, 99999)


def test_the_trash_keeps_them_and_emptying_it_takes_them(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection
) -> None:
    note = notes.create("버릴 글")
    edit(notes, note.id, "버릴 글, 고침")
    notes.delete(note.id)
    assert kept.has_any(note.id)
    notes.restore(note.id)
    assert texts(kept, note.id) == ["버릴 글"]

    notes.delete(note.id)
    notes.purge(note.id)

    assert connection.execute("SELECT count(*) FROM note_versions").fetchall()[0][0] == 0


def test_the_user_deletes_one_or_all(notes: NoteRepository, kept: VersionRepository) -> None:
    note = notes.create("하나")
    edit(notes, note.id, "둘")
    edit(notes, note.id, "셋")
    other = notes.create("다른 메모")
    edit(notes, other.id, "다른 메모, 고침")
    newest = kept.listed(note.id)[0]

    assert kept.delete(note.id, newest.id)
    assert not kept.delete(note.id, newest.id)  # gone already
    assert not kept.delete(note.id, kept.listed(other.id)[0].id)  # not this note's
    assert texts(kept, note.id) == ["하나"]
    assert kept.delete_all(note.id) == 1
    assert texts(kept, note.id) == [] and not kept.has_any(note.id)
    assert texts(kept, other.id) == ["다른 메모"]


def test_a_text_from_elsewhere_keeps_the_one_it_replaces(
    notes: NoteRepository, kept: VersionRepository
) -> None:
    note = notes.create("이 PC의 글")
    notes.replace_text(note.id, "이 PC의 글", came_from="laptop")  # the same: nothing
    assert texts(kept, note.id) == []
    after = notes.replace_text(note.id, "노트북의 글", came_from="laptop")
    assert after.body == "노트북의 글"
    assert texts(kept, note.id) == ["이 PC의 글"]
    with pytest.raises(NoteNotSecretError):
        notes.replace_text(note.id, b"sealed", came_from="laptop")


# Secret notes: their earlier texts follow them, never in the clear while secret


def test_made_secret_its_earlier_texts_are_sealed_and_their_plain_text_is_gone(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection
) -> None:
    note = notes.create(f"처음 {MARK}")
    edit(notes, note.id, f"둘째 {MARK}")
    edit(notes, note.id, f"셋째 {MARK}")
    plain_hashes = {content_hash(f"처음 {MARK}"), content_hash(f"둘째 {MARK}")}

    notes.make_secret(
        note.id, "은행", seal_text(SECRET_KEY, note.id, f"셋째 {MARK}"), sealer(note.id)
    )

    assert holds_anywhere(connection, MARK) == []
    hashes = {str(h) for (h,) in connection.execute("SELECT content_hash FROM note_versions")}
    assert not hashes & plain_hashes  # no hash a guess at the text could be checked against
    listed = kept.listed(note.id)
    assert [(v.sealed, v.first_line) for v in listed] == [(True, None), (True, None)]
    opened = [
        open_text(SECRET_KEY, note.id, t) for t in texts(kept, note.id) if isinstance(t, bytes)
    ]
    assert opened == [f"둘째 {MARK}", f"처음 {MARK}"]


def test_it_is_not_made_secret_with_earlier_texts_nothing_seals(
    notes: NoteRepository, kept: VersionRepository
) -> None:
    note = notes.create("처음")
    edit(notes, note.id, "둘째")

    with pytest.raises(ValueError, match="earlier texts"):
        notes.make_secret(note.id, "은행", seal_text(SECRET_KEY, note.id, "둘째"))

    assert not now_of(notes, note.id).secret
    assert texts(kept, note.id) == ["처음"]


def test_a_secret_notes_edits_are_kept_sealed_and_go_back_as_they_are(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection
) -> None:
    note = notes.create("")
    notes.make_secret(note.id, "은행", seal_text(SECRET_KEY, note.id, f"하나 {MARK}"))
    for text in (f"둘 {MARK}", f"셋 {MARK}"):
        notes.update_secret_body(note.id, seal_text(SECRET_KEY, note.id, text), keep_earlier=True)

    assert holds_anywhere(connection, MARK) == []
    first = kept.listed(note.id)[-1]
    restored = notes.restore_version(note.id, first.id)
    assert open_text(SECRET_KEY, note.id, restored.sealed or b"") == f"하나 {MARK}"


def test_made_ordinary_its_earlier_texts_are_opened_and_one_that_will_not_stays(
    notes: NoteRepository, kept: VersionRepository, connection: apsw.Connection
) -> None:
    note = notes.create("가")
    edit(notes, note.id, "나")
    edit(notes, note.id, "다")
    notes.make_secret(note.id, "제목", seal_text(SECRET_KEY, note.id, "다"), sealer(note.id))
    for text in ("가", "라"):  # "가" again, sealed apart from the first
        notes.update_secret_body(note.id, seal_text(SECRET_KEY, note.id, text), keep_earlier=True)
    connection.execute(
        "INSERT INTO note_versions (note_id, sealed, content_hash, made_at, kept_at)"
        " VALUES (?, ?, 'x', '2026-11-06T09:00:00.000Z', '2026-11-06T09:00:00.000Z')",
        (note.id, seal_text(secrets.token_bytes(32), note.id, "다른 키로 잠긴 글")),
    )

    ordinary = notes.make_ordinary(note.id, "라", opener(note.id))

    assert ordinary.body == "라" and not ordinary.secret
    shown = texts(kept, note.id)
    assert [t for t in shown if isinstance(t, str)] == ["가", "다", "나"]  # "가" once
    assert len([t for t in shown if isinstance(t, bytes)]) == 1  # sealed with another key


# Any order of edits, texts from elsewhere, going back, deleting, secret and
# ordinary, and days going by: what is shown is what the rules say, row for row.


@dataclass(frozen=True)
class Kept:
    token: object  # the text, or (secret) what its encrypted bytes are
    text: str
    made_at: str
    came_from: str | None
    restored: bool
    kept_at: str
    order: int

    @property
    def shown_at(self) -> str:
        return self.kept_at if self.restored else self.made_at


@dataclass
class Model:
    text: str
    token: object
    text_at: str | None
    updated_at: str
    came_from_now: str | None = None
    secret: bool = False
    kept: list[Kept] = field(default_factory=list[Kept])
    order: Iterator[int] = field(default_factory=count)
    deleted: set[str] = field(default_factory=set[str])
    put_aside: dict[str, str] = field(default_factory=dict[str, str])

    def keep(self, now: str, restored: bool = False) -> None:
        if not self.text.strip():
            return
        self.kept = [entry for entry in self.kept if entry.token != self.token]
        self.kept.append(
            Kept(
                self.token,
                self.text,
                self.text_at or self.updated_at,
                self.came_from_now,
                restored,
                now,
                next(self.order),
            )
        )
        self.put_aside[self.text] = now
        self.prune(now)
        self.kept = sorted(self.kept, key=lambda e: (e.kept_at, e.order))[-KEEP_PER_NOTE:]

    def prune(self, now: str) -> None:
        cutoff = cutoff_of(now)
        self.kept = [entry for entry in self.kept if entry.kept_at >= cutoff]

    def listed(self, now: str) -> list[Kept]:
        cutoff = cutoff_of(now)
        shown = [e for e in self.kept if e.kept_at >= cutoff and e.token != self.token]
        return sorted(shown, key=lambda e: (e.shown_at, e.order), reverse=True)

    def changed(self, text: str, token: object, now: str, came_from: str | None = None) -> None:
        self.text, self.token, self.text_at, self.updated_at = text, token, now, now
        self.came_from_now = came_from


def cutoff_of(now: str) -> str:
    return stamp(datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=KEEP_DAYS))


STEPS = st.lists(
    st.one_of(
        st.tuples(st.just("edit"), st.integers(1, 3)),
        st.tuples(st.just("receive"), st.just(0)),
        st.tuples(st.just("back"), st.integers(0, 60)),
        st.tuples(st.just("delete"), st.integers(0, 60)),
        st.tuples(st.just("secret"), st.just(0)),
        st.tuples(st.just("ordinary"), st.just(0)),
        st.tuples(st.just("days"), st.integers(1, 20)),
    ),
    max_size=40,
)


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(plan=STEPS)
def test_any_order_shows_what_the_rules_say(tmp_path: Path, plan: list[tuple[str, int]]) -> None:
    clock = Clock()
    path = tmp_path / f"notes-{secrets.token_hex(4)}.db"
    connection = open_store(path, KEY)
    try:
        run(
            plan,
            NoteRepository(connection, clock),
            VersionRepository(connection, clock),
            connection,
            clock,
        )
    finally:
        connection.close()
        path.unlink()


def run(
    plan: list[tuple[str, int]],
    notes: NoteRepository,
    kept: VersionRepository,
    connection: apsw.Connection,
    clock: Clock,
) -> None:
    note = notes.create(f"시작 {MARK}")
    model = Model(note.body, note.body, None, note.updated_at)
    made = (f"글 {number} {MARK}" for number in count())

    def sealed(text: str) -> bytes:
        return seal_text(SECRET_KEY, note.id, text)

    for kind, number in plan:
        now = clock.later(minutes=1)
        if kind == "edit":
            for save in range(number):
                now = clock.later(seconds=1)
                text = next(made)
                if save == 0:
                    model.keep(now)
                if model.secret:
                    data = sealed(text)
                    notes.update_secret_body(note.id, data, keep_earlier=save == 0)
                    model.changed(text, data, now)
                else:
                    notes.update_body(note.id, text, keep_earlier=save == 0)
                    model.changed(text, text, now)
        elif kind == "receive":
            text = next(made)
            data = sealed(text) if model.secret else text
            notes.replace_text(note.id, data, came_from="laptop")
            model.keep(now)
            model.changed(text, data, now, came_from="laptop")
        elif kind in ("back", "delete"):
            listed, expected = kept.listed(note.id), model.listed(now)
            if listed:
                chosen, entry = listed[number % len(listed)], expected[number % len(expected)]
                if kind == "back":
                    notes.restore_version(note.id, chosen.id)
                    model.keep(now, restored=True)
                    model.changed(entry.text, entry.token, now)
                else:
                    kept.delete(note.id, chosen.id)
                    model.kept.remove(entry)
                    model.deleted.add(entry.text)
        elif kind == "secret" and not model.secret:
            data = sealed(model.text)
            notes.make_secret(note.id, "비밀", data, sealer(note.id))
            model.kept = [replace(entry, token=object()) for entry in model.kept]
            model.secret, model.token, model.updated_at = True, data, now
        elif kind == "ordinary" and model.secret:
            notes.make_ordinary(note.id, model.text, opener(note.id))
            newest: dict[str, Kept] = {}
            for entry in sorted(model.kept, key=lambda e: (e.kept_at, e.order)):
                newest[entry.text] = replace(entry, token=entry.text)
            model.kept = sorted(newest.values(), key=lambda e: (e.kept_at, e.order))
            model.secret, model.token, model.updated_at = False, model.text, now
        elif kind == "days":
            now = clock.later(days=number)
            kept.prune()
            model.prune(now)
        check(model, kept, connection, note.id, clock())


def check(
    model: Model, kept: VersionRepository, connection: apsw.Connection, note_id: str, now: str
) -> None:
    shown: list[tuple[str, str, str | None, bool]] = []
    for version in kept.listed(note_id):
        stored = kept.text(note_id, version.id)
        assert version.sealed == model.secret == isinstance(stored, bytes)
        text = open_text(SECRET_KEY, note_id, stored) if isinstance(stored, bytes) else str(stored)
        shown.append((text, version.at, version.came_from, version.restored))
    assert shown == [(e.text, e.shown_at, e.came_from, e.restored) for e in model.listed(now)]
    # Every text put aside within the 30 days is still there, or is the text now,
    # unless the user deleted it (fewer than 50 are kept here).
    there = {text for text, *_ in shown} | {model.text}
    cutoff = cutoff_of(now)
    for text, when in model.put_aside.items():
        if when >= cutoff and text not in model.deleted:
            assert text in there
    if model.secret:  # never in the clear while secret, anywhere
        assert holds_anywhere(connection, MARK) == []
