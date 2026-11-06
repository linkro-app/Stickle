"""The fake clouds' troubles, each as the real thing goes wrong: late in lists,
half read, a copy under another name, an answer lost, too many requests, no
answer, an expired or withdrawn sign-in, no room, the folder gone or gone back
to an earlier state, another vault's file in its place. Whatever goes wrong
comes up as one of sync's errors only."""

import pytest
from fake_cloud import (
    DROPBOX,
    KINDS,
    LATE_BY,
    ONEDRIVE,
    WEBDAV,
    WEBDAV_WEAK,
    FakeCloud,
    Kind,
    Trouble,
    by_number,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from peek import describe_target

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    FullError,
    NotThereError,
    RefusedError,
    SignInNeededError,
    TargetError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


def troubled(kind: Kind, *troubles: Trouble | None) -> FakeCloud:
    return FakeCloud(kind, trouble=by_number(troubles))


def test_late_in_lists_yet_read_by_its_path_at_once() -> None:
    cloud = troubled(WEBDAV, Trouble.LATE)
    target = cloud.target("laptop")

    target.put(NOTE, b"late", Absent())

    assert list(target.list("notes")) == []
    assert target.get(NOTE).data == b"late"
    for _ in range(LATE_BY):
        target.get(NOTE)
    assert [e.path for e in target.list("notes")] == [NOTE]


def test_a_file_read_half() -> None:
    cloud = troubled(DROPBOX, None, Trouble.HALF)
    target = cloud.target("laptop")
    target.put(NOTE, b"0123456789", Absent())

    assert target.get(NOTE).data == b"01234"
    assert target.get(NOTE).data == b"0123456789"


def test_a_copy_under_another_name() -> None:
    cloud = FakeCloud(ONEDRIVE)
    target = cloud.target("laptop")
    target.put(NOTE, b"note", Absent())
    copy = NOTE.replace(".json", " (1).json")

    cloud.copy_by_hand(NOTE, copy)

    assert {e.path for e in target.list("notes")} == {NOTE, copy}
    assert target.get(copy).data == b"note"


def test_an_answer_lost_the_write_done_once() -> None:
    cloud = troubled(DROPBOX, Trouble.LOST)
    target = cloud.target("laptop")

    with pytest.raises(UnavailableError):
        target.put(NOTE, b"once", Absent())

    with pytest.raises(RefusedError):  # sent again: it is there already
        target.put(NOTE, b"once", Absent())
    assert cloud.files() == {NOTE: b"once"}


@pytest.mark.parametrize("trouble", [Trouble.UNAVAILABLE, Trouble.TOO_BUSY])
def test_no_answer_or_too_busy_and_nothing_done(trouble: Trouble) -> None:
    cloud = troubled(ONEDRIVE, trouble)
    target = cloud.target("laptop")

    with pytest.raises(
        UnavailableError if trouble is Trouble.UNAVAILABLE else TooBusyError
    ) as raised:
        target.put(NOTE, b"no", Absent())

    assert cloud.files() == {}
    if isinstance(raised.value, TooBusyError):
        assert raised.value.retry_after == 1.0
    target.put(NOTE, b"yes", Absent())
    assert cloud.files() == {NOTE: b"yes"}


def test_an_expired_token_is_refreshed_and_a_withdrawn_one_stops_it() -> None:
    cloud = FakeCloud(DROPBOX)
    target = cloud.target("laptop")
    cloud.expire_token("laptop")

    with pytest.raises(TokenExpiredError):
        target.get(NOTE)
    target.refresh()
    with pytest.raises(NotThereError):
        target.get(NOTE)

    cloud.refuse_refresh("laptop")
    with pytest.raises(SignInNeededError):
        target.get(NOTE)
    with pytest.raises(SignInNeededError):
        target.refresh()
    cloud.target("desktop").put(NOTE, b"others go on", Absent())  # that device only


def test_no_room_stops_writing_not_reading() -> None:
    cloud = FakeCloud(ONEDRIVE)
    target = cloud.target("laptop")
    target.put(NOTE, b"there", Absent())
    cloud.full = True

    with pytest.raises(FullError):
        target.put("notes/other.json", b"no room", Absent())
    assert target.get(NOTE).data == b"there"
    assert cloud.files() == {NOTE: b"there"}


def test_the_folder_vanishes() -> None:
    cloud = FakeCloud(DROPBOX)
    target = cloud.target("laptop")
    target.put("vault.json", b"{}", Absent())
    target.put(NOTE, b"note", Absent())
    pages = target.changes(None)
    assert pages is not None
    cursor = [page.cursor for page in pages][-1]

    cloud.vanish()

    with pytest.raises(NotThereError):
        target.get("vault.json")
    after = target.changes(cursor)
    assert after is not None
    assert {path for page in after for path in page.gone} == {"vault.json", NOTE}


def test_the_folder_goes_back_to_an_earlier_state_under_new_tags() -> None:
    cloud = FakeCloud(WEBDAV)
    target = cloud.target("laptop")
    target.put(NOTE, b"then", Absent())
    then = cloud.requests
    old_tag = target.get(NOTE).tag
    target.put(NOTE, b"now", AsRead(old_tag))
    target.put("notes/later.json", b"later", Absent())

    cloud.go_back_to(then)

    read = target.get(NOTE)
    assert (read.data, cloud.files()) == (b"then", {NOTE: b"then"})
    assert read.tag != old_tag  # written again, as a restore does


def test_another_vaults_file_in_its_place() -> None:
    cloud = FakeCloud(ONEDRIVE)
    target = cloud.target("laptop")
    target.put("vault.json", b'{"vault": "one"}', Absent())
    before = target.get("vault.json").tag

    cloud.put_by_hand("vault.json", b'{"vault": "another"}')

    read = target.get("vault.json")
    assert (read.data, read.tag != before) == (b'{"vault": "another"}', True)


def test_onedrive_shows_changes_a_little_late() -> None:
    cloud = FakeCloud(ONEDRIVE)
    target = cloud.target("laptop")
    pages = target.changes(None)
    assert pages is not None
    cursor = [page.cursor for page in pages][-1]
    target.put(NOTE, b"note", Absent())

    first = target.changes(cursor)
    assert first is not None and [p.changed for p in first] == [()]
    for _ in range(ONEDRIVE.changes_late):
        target.get(NOTE)
    again = target.changes(cursor)
    assert again is not None and [e.path for p in again for e in p.changed] == [NOTE]


def test_written_again_as_it_was_dropbox_keeps_its_rev_and_onedrive_does_not() -> None:
    for kind, same in ((DROPBOX, True), (ONEDRIVE, False)):
        target = FakeCloud(kind).target("laptop")
        first = target.put(NOTE, b"same", Absent())
        assert (target.put(NOTE, b"same", AsRead(first)) == first) is same


def test_a_weak_tag_holds_no_condition_until_the_second_is_over() -> None:
    target = FakeCloud(WEBDAV_WEAK).target("laptop")
    target.put(NOTE, b"one", Absent())
    weak = target.get(NOTE).tag
    assert weak is not None and weak.startswith("W/")

    with pytest.raises(RefusedError):  # unchanged, yet refused, as Apache does
        target.put(NOTE, b"two", AsRead(weak))

    strong = weak
    for _ in range(4):
        strong = target.get(NOTE).tag
    assert strong is not None and not strong.startswith("W/")
    target.put(NOTE, b"two", AsRead(strong))


def test_as_wsgidav_two_writes_of_one_size_within_a_second_keep_the_tag() -> None:
    target = FakeCloud(WEBDAV).target("laptop")
    target.put(NOTE, b"aaaa", Absent())
    first = target.get(NOTE).tag

    target.put(NOTE, b"bbbb", AsRead(first))

    assert target.get(NOTE).tag == first  # changed, yet the tag is the same
    target.put(NOTE, b"cccc", AsRead(first))  # the condition cannot tell: through
    assert target.get(NOTE).data == b"cccc"
    later = first
    for _ in range(4):
        later = target.get(NOTE).tag
    target.put(NOTE, b"dddd", AsRead(later))
    assert target.get(NOTE).tag != first  # a second later, another tag


def test_described_for_a_failed_test() -> None:
    cloud = troubled(DROPBOX, None, Trouble.UNAVAILABLE)
    target = cloud.target("laptop")
    target.put(NOTE, b"note", Absent())
    with pytest.raises(UnavailableError):
        target.get(NOTE)

    text = cloud.describe()

    assert f"{NOTE}  000000001  4 bytes, written at 1" in text
    assert f"1 laptop put {NOTE}: ok" in text
    assert f"2 laptop get {NOTE}: UnavailableError" in text


OPS = st.sampled_from(["list", "get", "put-new", "put-over", "delete", "changes", "wait"])
TROUBLES = st.sampled_from([None, *Trouble])


@settings(max_examples=150, deadline=None)
@given(
    kind=st.sampled_from(KINDS),
    steps=st.lists(st.tuples(OPS, st.sampled_from([NOTE, "vault.json"]), TROUBLES), max_size=30),
    full=st.booleans(),
)
def test_whatever_goes_wrong_comes_up_as_one_of_syncs_errors(
    kind: Kind, steps: list[tuple[str, str, Trouble | None]], full: bool
) -> None:
    troubles = [trouble for _op, _path, trouble in steps]
    cloud = FakeCloud(kind, trouble=by_number(troubles))
    target = cloud.target("laptop")
    for number, (op, path, _trouble) in enumerate(steps):
        cloud.full = full and number % 3 == 0
        try:
            if op == "list":
                list(target.list("notes"))
            elif op == "get":
                target.get(path)
            elif op == "put-new":
                target.put(path, b"new", Absent())
            elif op == "put-over":
                target.put(path, b"over", AsRead(f'"{number}"'))
            elif op == "delete":
                target.delete(path, AsRead(f'"{number}"'))
            elif op == "changes":
                list(target.changes(None) or [])
            else:
                target.wait_for_change(f"0:{number}", 1)
        except TargetError:
            pass  # any of sync's errors; anything else fails the test


def test_a_look_into_any_targets_folder() -> None:
    target = FakeCloud(DROPBOX).target("laptop")
    target.put("vault.json", b"{}", Absent())
    target.put(NOTE, b"note", Absent())

    text = describe_target(target)

    assert text.splitlines() == [
        "vault.json  000000001  2 bytes",
        "notes/  1 files",
        f"  {NOTE}  000000002  4 bytes",
        "records/  0 files",
        "devices/  0 files",
        "keys/  0 files",
    ]
