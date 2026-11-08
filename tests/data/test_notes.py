import secrets
import tempfile
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from stickle.core.note import content_hash
from stickle.data.database import KEY_BYTES
from stickle.data.notes import NoteDeletedError, NoteNotFoundError, NoteRepository
from stickle.data.schema import open_store, search_index_is_consistent
from stickle.data.search import search

KEY = secrets.token_bytes(KEY_BYTES)


class FakeClock:
    """Returns the time it is set to; tests can move it backwards."""

    def __init__(self) -> None:
        self.now = "2026-09-26T10:00:00.000Z"

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "notes.db"


@pytest.fixture
def db(path: Path) -> Iterator[apsw.Connection]:
    connection = open_store(path, KEY)
    yield connection
    connection.close()


@pytest.fixture
def repo(db: apsw.Connection, clock: FakeClock) -> NoteRepository:
    return NoteRepository(db, clock)


def test_new_note_has_defaults_and_is_visible(repo: NoteRepository) -> None:
    note = repo.create("회의록")

    assert note.body == "회의록"
    assert note.color == "yellow"
    assert note.always_on_top
    assert not note.hidden and not note.deleted
    assert note.created_at == note.updated_at == "2026-09-26T10:00:00.000Z"
    assert note.content_hash == content_hash("회의록")
    assert repo.visible() == [note]


def test_ids_are_random_uuids(repo: NoteRepository) -> None:
    ids = {repo.create().id for _ in range(20)}
    assert len(ids) == 20
    assert all(len(i) == 36 and i[14] == "4" for i in ids)


def test_notes_survive_reopening(path: Path, clock: FakeClock) -> None:
    connection = open_store(path, KEY)
    note = NoteRepository(connection, clock).create("장보기\n😀")
    connection.close()

    connection = open_store(path, KEY)
    assert NoteRepository(connection, clock).get(note.id) == note
    connection.close()


def test_editing_updates_text_hash_time_and_search(repo: NoteRepository, clock: FakeClock) -> None:
    db_note = repo.create("회의록을 내일까지")
    clock.now = "2026-09-26T11:00:00.000Z"

    edited = repo.update_body(db_note.id, "장보기 목록")

    assert edited.body == "장보기 목록"
    assert edited.content_hash == content_hash("장보기 목록")
    assert edited.updated_at == "2026-09-26T11:00:00.000Z"
    assert edited.created_at == db_note.created_at
    assert edited.change_seq > db_note.change_seq


def test_saving_the_same_text_is_not_a_change(repo: NoteRepository, clock: FakeClock) -> None:
    note = repo.create("회의록")
    clock.now = "2026-09-26T11:00:00.000Z"

    assert repo.update_body(note.id, "회의록") == note


def test_hidden_note_leaves_the_desktop_but_stays_listed(repo: NoteRepository) -> None:
    kept = repo.create("보이는 메모")
    note = repo.create("숨길 메모")

    repo.set_hidden(note.id, True)

    assert [n.id for n in repo.visible()] == [kept.id]
    assert [n.id for n in repo.hidden()] == [note.id]
    shown = repo.set_hidden(note.id, False)
    assert not shown.hidden
    assert repo.hidden() == []


def test_hidden_list_puts_the_latest_first(repo: NoteRepository) -> None:
    a, b, c = (repo.create(t) for t in "abc")
    for note in (b, a, c):
        repo.set_hidden(note.id, True)

    assert [n.id for n in repo.hidden()] == [c.id, a.id, b.id]


def test_live_notes_are_shown_and_hidden_ones_latest_change_first(repo: NoteRepository) -> None:
    a, b, c, gone = (repo.create(t) for t in "abcd")
    repo.set_hidden(a.id, True)
    repo.update_body(b.id, "b, changed")
    repo.delete(gone.id)

    assert [n.id for n in repo.live()] == [b.id, a.id, c.id]


def test_matching_finds_notes_shown_hidden_or_in_the_trash_but_not_emptied(
    repo: NoteRepository,
) -> None:
    shown = repo.create("회의록을 정리")
    hidden = repo.set_hidden(repo.create("주간 회의 메모").id, True)
    trashed = repo.delete(repo.create("지난 회의").id)
    emptied = repo.delete(repo.create("취소된 회의").id)
    repo.purge(emptied.id)
    repo.create("장보기")

    assert repo.matching("회의") == {shown.id, hidden.id, trashed.id}
    assert repo.matching("장보기 목록") == set()


def test_delete_only_marks_and_can_be_undone(repo: NoteRepository, db: apsw.Connection) -> None:
    note = repo.create("회의록을 내일까지")

    deleted = repo.delete(note.id)

    assert deleted.deleted
    assert deleted.body == "회의록을 내일까지"  # the text is kept
    assert repo.visible() == [] and repo.hidden() == []
    assert repo.last_deleted() == deleted
    assert search(db, "회의록") == [note.id]  # the search layer filters deleted notes later

    restored = repo.restore(note.id)
    assert not restored.deleted
    assert restored.body == note.body
    assert repo.visible() == [restored]
    assert repo.last_deleted() is None


def test_last_deleted_is_the_most_recent_deletion(repo: NoteRepository) -> None:
    a, b = repo.create("a"), repo.create("b")
    repo.delete(b.id)
    repo.delete(a.id)

    last = repo.last_deleted()
    assert last is not None and last.id == a.id


def test_hidden_then_deleted_note_comes_back_hidden(repo: NoteRepository) -> None:
    note = repo.create("a")
    repo.set_hidden(note.id, True)
    repo.delete(note.id)

    assert repo.restore(note.id).hidden


def test_deleted_note_cannot_be_edited(repo: NoteRepository) -> None:
    note = repo.create("a")
    repo.delete(note.id)

    with pytest.raises(NoteDeletedError):
        repo.update_body(note.id, "b")
    with pytest.raises(NoteDeletedError):
        repo.set_hidden(note.id, True)
    with pytest.raises(NoteDeletedError):
        repo.set_color(note.id, "sky")
    with pytest.raises(NoteDeletedError):
        repo.delete(note.id)


def test_colour_is_stored_by_key_and_is_a_change(repo: NoteRepository, clock: FakeClock) -> None:
    note = repo.create("장보기")
    clock.now = "2026-09-26T11:00:00.000Z"

    changed = repo.set_color(note.id, "sky")

    assert changed.color == "sky"
    assert changed.body == "장보기"
    assert changed.updated_at == clock.now
    assert changed.change_seq > note.change_seq
    assert repo.set_color(note.id, "sky") == changed  # the same colour is not a change


def test_folding_is_stored_and_is_a_change(repo: NoteRepository, clock: FakeClock) -> None:
    note = repo.create("접을 메모")
    clock.now = "2026-09-26T12:00:00.000Z"

    folded = repo.set_collapsed(note.id, True)

    assert folded.collapsed
    assert folded.body == "접을 메모"
    assert folded.change_seq > note.change_seq
    assert repo.set_collapsed(note.id, True) == folded  # already folded: not a change
    assert not repo.set_collapsed(note.id, False).collapsed


def test_a_colour_this_version_does_not_know_is_kept(repo: NoteRepository) -> None:
    note = repo.create("from a newer version", "teal2")

    repo.update_body(note.id, "edited here")

    stored = repo.get(note.id)
    assert stored is not None
    assert stored.color == "teal2"


def test_a_note_dropped_leaves_no_trace(repo: NoteRepository, db: apsw.Connection) -> None:
    kept = repo.create("남는 메모")
    gone = repo.create("그대로인 환영 메모")
    trashed = repo.create("휴지통의 메모")
    repo.delete(trashed.id)

    repo.drop(gone.id)
    repo.drop(trashed.id)

    assert [note.id for note in repo.all()] == [kept.id]
    assert repo.deletion_records() == []


def test_a_note_handed_over_gives_its_place_size_and_earlier_texts(
    repo: NoteRepository, db: apsw.Connection, clock: FakeClock
) -> None:
    own = repo.create("같은 글")
    repo.update_body(own.id, "고치던 글", keep_earlier=True)
    repo.update_body(own.id, "같은 글", keep_earlier=True)
    there = repo.create("같은 글")
    now = clock()
    for note_id, x in ((own.id, 0.1), (there.id, 0.5)):
        db.execute(
            "INSERT INTO note_layouts (note_id, config_key, monitor_key, rel_x, rel_y, updated_at)"
            " VALUES (?, ?, '[]', ?, 0.2, ?)",
            (note_id, "main" if note_id == there.id else "spare", x, now),
        )
    db.execute(
        "INSERT INTO note_sizes (note_id, width, height, updated_at) VALUES (?, 300, 200, ?)",
        (own.id, now),
    )

    repo.hand_over(own.id, there.id)

    assert repo.get(own.id) is None and repo.deletion_records() == []
    layouts = db.execute(
        "SELECT config_key, rel_x FROM note_layouts WHERE note_id = ? ORDER BY config_key",
        (there.id,),
    ).fetchall()
    assert layouts == [("main", 0.5), ("spare", 0.1)]  # its own kept, the other's taken
    sizes = db.execute("SELECT width FROM note_sizes WHERE note_id = ?", (there.id,)).fetchall()
    assert sizes == [(300,)]
    texts = db.execute("SELECT body FROM note_versions WHERE note_id = ?", (there.id,)).fetchall()
    assert ("고치던 글",) in texts


def test_unknown_note_is_reported(repo: NoteRepository) -> None:
    assert repo.get("missing") is None
    with pytest.raises(NoteNotFoundError):
        repo.update_body("missing", "a")


def test_change_order_survives_a_clock_going_backwards(
    repo: NoteRepository, clock: FakeClock
) -> None:
    note = repo.create("a")
    clock.now = "2020-01-01T00:00:00.000Z"

    edited = repo.update_body(note.id, "b")

    assert edited.change_seq > note.change_seq
    assert edited.updated_at == "2020-01-01T00:00:00.000Z"  # time is recorded as told


def test_nul_is_refused_and_nothing_is_saved(repo: NoteRepository) -> None:
    note = repo.create("a")
    with pytest.raises(apsw.ConstraintError):
        repo.update_body(note.id, "a\x00b")
    with pytest.raises(apsw.ConstraintError):
        repo.create("x\x00")

    assert [n.body for n in repo.all()] == ["a"]


def test_failed_change_does_not_consume_a_change_number(repo: NoteRepository) -> None:
    note = repo.create("a")
    with pytest.raises(apsw.ConstraintError):
        repo.update_body(note.id, "\x00")

    assert repo.update_body(note.id, "b").change_seq == note.change_seq + 1


# Invariants over any sequence of operations, including closing and reopening:
# no note is ever lost, what comes back matches what was written, change
# numbers only grow, and the search index agrees with the notes.

TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=20)


class NoteStore(RuleBasedStateMachine):
    notes = Bundle("notes")

    def __init__(self) -> None:
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "notes.db"
        self.clock = FakeClock()
        self.db = open_store(self.path, KEY)
        self.repo = NoteRepository(self.db, self.clock)
        # id -> (body, hidden, deleted)
        self.model: dict[str, tuple[str, bool, bool]] = {}
        self.last_seq = 0

    def teardown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _saw(self, seq: int) -> None:
        assert seq > self.last_seq
        self.last_seq = seq

    @rule(target=notes, body=TEXT)
    def create(self, body: str) -> str:
        note = self.repo.create(body)
        self._saw(note.change_seq)
        self.model[note.id] = (body, False, False)
        return note.id

    @rule(note_id=notes, body=TEXT)
    def edit(self, note_id: str, body: str) -> None:
        old, hidden, deleted = self.model[note_id]
        if deleted:
            with pytest.raises(NoteDeletedError):
                self.repo.update_body(note_id, body)
            return
        note = self.repo.update_body(note_id, body)
        if body != old:
            self._saw(note.change_seq)
        self.model[note_id] = (body, hidden, False)

    @rule(note_id=notes, hidden=st.booleans())
    def hide(self, note_id: str, hidden: bool) -> None:
        body, _, deleted = self.model[note_id]
        if deleted:
            return
        self.repo.set_hidden(note_id, hidden)
        self.model[note_id] = (body, hidden, False)

    @rule(note_id=notes)
    def delete(self, note_id: str) -> None:
        body, hidden, deleted = self.model[note_id]
        if not deleted:
            self._saw(self.repo.delete(note_id).change_seq)
            self.model[note_id] = (body, hidden, True)

    @rule(note_id=notes)
    def restore(self, note_id: str) -> None:
        body, hidden, _ = self.model[note_id]
        self.repo.restore(note_id)
        self.model[note_id] = (body, hidden, False)

    @rule(time=st.sampled_from(["2020-01-01T00:00:00.000Z", "2030-01-01T00:00:00.000Z"]))
    def move_clock(self, time: str) -> None:
        self.clock.now = time

    @rule()
    def reopen(self) -> None:
        self.db.close()
        self.db = open_store(self.path, KEY)
        self.repo = NoteRepository(self.db, self.clock)

    @invariant()
    def nothing_is_lost(self) -> None:
        stored = {n.id: (n.body, n.hidden, n.deleted) for n in self.repo.all()}
        assert stored == self.model

    @invariant()
    def lists_agree_with_the_model(self) -> None:
        visible = {n.id for n in self.repo.visible()}
        hidden = {n.id for n in self.repo.hidden()}
        assert visible == {i for i, (_, h, d) in self.model.items() if not h and not d}
        assert hidden == {i for i, (_, h, d) in self.model.items() if h and not d}

    @invariant()
    def hashes_match_bodies(self) -> None:
        assert all(n.content_hash == content_hash(n.body) for n in self.repo.all())

    @invariant()
    def search_index_is_consistent(self) -> None:
        assert search_index_is_consistent(self.db)


NoteStore.TestCase.settings = settings(  # pyright: ignore[reportUnknownMemberType]
    max_examples=40, stateful_step_count=25, deadline=None
)
test_note_store = NoteStore.TestCase  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
