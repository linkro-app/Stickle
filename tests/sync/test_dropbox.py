"""The Dropbox target (stickle.sync.targets.dropbox) against Dropbox in the test,
over real HTTPS: what it asks, what it does with each answer, where its tokens
go, and its feed's long poll. What every target answers alike is in
test_target_contract."""

import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

import pytest
from dropbox_server import (
    API,
    APP_KEY,
    CONTENT,
    FIRST_REFRESH,
    HOST,
    LONGPOLL,
    NOTIFY,
    TOKEN_PATH,
    Answer,
    DropboxState,
    dropbox_server,
)

from stickle.sync.targets.base import (
    Absent,
    AsRead,
    CursorExpiredError,
    FullError,
    NoAccessError,
    NotThereError,
    RefusedError,
    SignInNeededError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.dropbox import WAIT, Dropbox, Place

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"
MINE = "devices/aaaaaaaa.json"
THEIRS = "devices/bbbbbbbb.json"


@dataclass
class Told:
    renewed: list[str] = field(default_factory=list[str])
    heard: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])


@pytest.fixture
def box() -> Iterator[DropboxState]:
    with dropbox_server() as running:
        yield running


@pytest.fixture
def told() -> Told:
    return Told()


def drive_of(
    box: DropboxState,
    told: Told,
    *,
    folder: str = "",
    public: Callable[[], ssl.SSLContext] | None = None,
    page: int = 2000,
) -> Dropbox:
    return Dropbox(
        APP_KEY,
        FIRST_REFRESH,
        told.renewed.append,
        place=box.place,
        folder=folder,
        public=box.context if public is None else public,
        heard=lambda date, took: told.heard.append((date, took)),
        page=page,
    )


@pytest.fixture
def drive(box: DropboxState, told: Told) -> Iterator[Dropbox]:
    made = drive_of(box, told)
    yield made
    made.close()


def failure(summary: str, error: Mapping[str, object]) -> dict[str, object]:
    return {"error_summary": summary, "error": dict(error)}


# Listing


def test_a_folder_not_made_yet_lists_nothing(drive: Dropbox) -> None:
    assert list(drive.list("notes")) == []
    assert list(drive.list("")) == []


def test_a_long_listing_comes_page_by_page(box: DropboxState, told: Told) -> None:
    drive = drive_of(box, told, page=100)
    names = {f"notes/{number:04}.json" for number in range(250)}
    for name in names:
        box.put_by_hand(name, b"n")

    listed = {entry.path: entry for entry in drive.list("notes")}

    assert set(listed) == names
    assert all(entry.tag == box.files[path].rev for path, entry in listed.items())
    assert len(box.requests(API, "/2/files/list_folder")) == 1
    assert len(box.requests(API, "/2/files/list_folder/continue")) == 2
    drive.close()


def test_folders_and_odd_names_are_left_out(box: DropboxState, drive: Dropbox) -> None:
    box.put_by_hand("notes/a.json", b"aa")
    box.put_by_hand("notes/inside/b.json", b"b")
    assert [(e.path, e.size) for e in drive.list("notes")] == [("notes/a.json", 2)]

    odd: list[object] = [
        {".tag": "file", "name": "..", "rev": "0123456789", "size": 1},
        {".tag": "file", "name": "c\\d.json", "rev": "0123456789", "size": 1},
        {".tag": "file", "rev": "0123456789", "size": 1},
        {".tag": "deleted", "name": "gone.json"},
        {".tag": "folder", "name": "inside"},
        "not an entry",
        {".tag": "file", "name": "e.json", "rev": "0123456789", "size": 3},
    ]
    box.answers.append(Answer(200, body={"entries": odd, "cursor": "AAH", "has_more": False}))

    assert [(e.path, e.tag, e.size) for e in drive.list("notes")] == [
        ("notes/e.json", "0123456789", 3)
    ]


def test_a_listing_whose_cursor_runs_out_fails_for_now(box: DropboxState, told: Told) -> None:
    drive = drive_of(box, told, page=1)
    for name in ("a", "b", "c"):
        box.put_by_hand(f"notes/{name}.json", b"n")
    listing = drive.list("notes")
    next(listing)
    box.expire_cursors()

    with pytest.raises(UnavailableError):
        list(listing)
    drive.close()


# Reading and writing


def test_a_read_gives_the_file_and_its_rev(box: DropboxState, drive: Dropbox) -> None:
    rev = box.put_by_hand(NOTE, b"from elsewhere")

    read = drive.get(NOTE)

    assert (read.data, read.tag) == (b"from elsewhere", rev)
    [download] = box.requests(CONTENT, "/2/files/download")
    assert download.arg == {"path": f"/{NOTE}"} and download.body == b""


def test_a_folder_is_no_file(box: DropboxState, drive: Dropbox) -> None:
    box.put_by_hand(NOTE, b"n")

    with pytest.raises(NotThereError):
        drive.get("notes")
    with pytest.raises(NotThereError):
        drive.get("notes/none.json")


def test_a_file_made_fails_if_one_is_there_even_of_the_same_bytes(
    box: DropboxState, drive: Dropbox
) -> None:
    rev = box.put_by_hand(NOTE, b"same")

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"same", Absent())

    assert box.files[NOTE].rev == rev
    [made] = box.requests(CONTENT, "/2/files/upload")
    assert made.arg == {
        "path": f"/{NOTE}",
        "mode": "add",
        "autorename": False,
        "mute": True,
        "strict_conflict": True,
    }


def test_a_file_written_over_only_as_read(box: DropboxState, drive: Dropbox) -> None:
    first = drive.put(NOTE, b"1", Absent())
    box.put_by_hand(NOTE, b"from elsewhere")

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(first))

    assert box.files[NOTE].data == b"from elsewhere"
    now = drive.get(NOTE).tag
    again = drive.put(NOTE, b"22", AsRead(now))
    assert box.files[NOTE].data == b"22" and again == box.files[NOTE].rev != now
    last = box.requests(CONTENT, "/2/files/upload")[-1]
    assert last.arg is not None and last.arg["mode"] == {".tag": "update", "update": now}


def test_a_file_written_over_deleted_since_is_refused(box: DropboxState, drive: Dropbox) -> None:
    rev = drive.put(NOTE, b"1", Absent())
    box.delete_by_hand(NOTE)

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(rev))

    assert NOTE not in box.files


@pytest.mark.parametrize("tag", [None, "0", '"{A},1"', "0123456789ABC"])
def test_a_file_written_over_without_a_rev_is_refused_unsent(
    box: DropboxState, drive: Dropbox, tag: str | None
) -> None:
    drive.put(NOTE, b"1", Absent())

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(tag))

    assert len(box.requests(CONTENT, "/2/files/upload")) == 1
    assert box.files[NOTE].data == b"1"


def test_the_same_bytes_again_keep_the_rev(box: DropboxState, drive: Dropbox) -> None:
    rev = drive.put(NOTE, b"1", Absent())

    assert drive.put(NOTE, b"1", AsRead(rev)) == rev
    assert drive.put(NOTE, b"22", AsRead(rev)) != rev


def test_a_delete_without_a_tag_deletes_as_it_is(box: DropboxState, drive: Dropbox) -> None:
    # As a device takes its own file off when it leaves: only it writes that file.
    box.put_by_hand(MINE, b"d")

    drive.delete(MINE, AsRead(None))

    assert box.files == {}
    [deleted] = box.requests(API, "/2/files/delete_v2")
    assert deleted.args == {"path": f"/{MINE}"}


@pytest.mark.parametrize("not_found", [False, True], ids=["conflict", "said-not-found"])
def test_a_delete_with_a_tag_only_as_read(
    box: DropboxState, drive: Dropbox, not_found: bool
) -> None:
    box.mismatch_not_found = not_found  # Dropbox's answer to that is not documented
    first = box.put_by_hand(NOTE, b"1")
    now = box.put_by_hand(NOTE, b"22")

    with pytest.raises(RefusedError):
        drive.delete(NOTE, AsRead(first))
    assert NOTE in box.files

    drive.delete(NOTE, AsRead(now))
    assert NOTE not in box.files
    assert box.requests(API, "/2/files/delete_v2")[-1].args == {
        "path": f"/{NOTE}",
        "parent_rev": now,
    }


def test_a_delete_of_what_is_not_there(box: DropboxState, drive: Dropbox) -> None:
    box.mismatch_not_found = True
    with pytest.raises(NotThereError):
        drive.delete(NOTE, AsRead("0123456789"))
    with pytest.raises(NotThereError):
        drive.delete(NOTE, AsRead(None))


def test_a_delete_with_a_tag_dropbox_never_gives_is_not_sent(
    box: DropboxState, drive: Dropbox
) -> None:
    with pytest.raises(NotThereError):
        drive.delete(NOTE, AsRead("0"))
    box.put_by_hand(NOTE, b"1")
    with pytest.raises(RefusedError):
        drive.delete(NOTE, AsRead("0"))

    assert NOTE in box.files
    assert box.requests(API, "/2/files/delete_v2") == []


def test_nothing_is_no_file_to_write(drive: Dropbox) -> None:
    with pytest.raises(ValueError):
        drive.put(NOTE, b"", Absent())


def test_paths_stay_in_its_folder(box: DropboxState, told: Told) -> None:
    drive = drive_of(box, told, folder="a-test")
    drive.put(NOTE, b"1", Absent())

    assert set(box.files) == {f"a-test/{NOTE}"}
    assert [entry.path for entry in drive.list("notes")] == [NOTE]
    for elsewhere in ("../vault.json", "notes/../../x", "notes\\x", "/vault.json", "notes//x"):
        with pytest.raises(ValueError):
            drive.get(elsewhere)
    with pytest.raises(ValueError):
        drive.get("notes/a\x7f.json")
    with pytest.raises(ValueError):
        drive_of(box, told, folder="..")
    with pytest.raises(ValueError):
        drive.delete("", AsRead(None))  # the folder itself
    assert set(box.files) == {f"a-test/{NOTE}"}
    drive.close()


# Tokens


def test_an_access_token_turned_down_is_made_again_once(
    box: DropboxState, drive: Dropbox, told: Told
) -> None:
    list(drive.list(""))
    box.expire_access_tokens()

    drive.put(NOTE, b"1", Absent())

    assert box.files[NOTE].data == b"1"
    assert len(box.requests(API, TOKEN_PATH)) == 2
    assert told.renewed == []  # Dropbox gives no new refresh token


def test_an_access_token_turned_down_again_is_out_of_date(
    box: DropboxState, drive: Dropbox
) -> None:
    box.turning_down_access = True

    with pytest.raises(TokenExpiredError):
        drive.get(NOTE)

    assert len(box.requests(CONTENT)) == 2


def test_a_refresh_turned_down_stops_to_sign_in(box: DropboxState, drive: Dropbox) -> None:
    box.refusing_refresh = True

    with pytest.raises(SignInNeededError):
        list(drive.list("notes"))

    assert box.requests(API, "/2/files/list_folder") == []


def test_the_app_turned_away_stops(box: DropboxState, drive: Dropbox) -> None:
    box.app_turned_away = True

    with pytest.raises(NoAccessError):
        drive.get(NOTE)


def test_a_permission_never_given_stops_without_a_new_token(
    box: DropboxState, drive: Dropbox
) -> None:
    list(drive.list(""))
    error = {".tag": "missing_scope", "required_scope": "files.content.write"}
    box.answers.append(Answer(401, body=failure("missing_scope/", error)))

    with pytest.raises(NoAccessError):
        drive.put(NOTE, b"1", Absent())

    assert len(box.requests(API, TOKEN_PATH)) == 1


def test_the_tokens_go_only_where_they_belong(box: DropboxState, drive: Dropbox) -> None:
    box.hold = 0.1
    tag = drive.put(NOTE, b"1", Absent())
    drive.put(NOTE, b"22", AsRead(tag))
    read = drive.get(NOTE)
    list(drive.list("notes"))
    box.expire_access_tokens()
    drive.put(MINE, b"d", Absent())
    feed = drive.feed("devices")
    feed.wait(feed.latest())
    drive.delete(NOTE, AsRead(read.tag))
    feed.close()

    for asked in box.asked:
        carried = f"{asked.authorization} {asked.path} {asked.arg} {asked.body!r}"
        if asked.path == TOKEN_PATH:
            assert asked.authorization is None and FIRST_REFRESH in asked.body.decode()
            assert not any(token in carried for token in box.issued)
        elif asked.host == NOTIFY:
            assert asked.authorization is None
            assert not any(token in carried for token in {FIRST_REFRESH, *box.issued})
        else:
            assert (asked.authorization or "").removeprefix("Bearer ") in box.issued
            assert FIRST_REFRESH not in carried


def test_nothing_it_says_tells_a_token_or_a_path(box: DropboxState, drive: Dropbox) -> None:
    drive.put(NOTE, b"1", Absent())
    feed = drive.feed("notes")
    cursor = feed.latest()
    box.turning_down_access = True
    with pytest.raises(TokenExpiredError) as raised:
        drive.get(NOTE)

    said = f"{drive!r} {feed!r} {raised.value!r} {raised.value}"
    for secret in (FIRST_REFRESH, *box.issued, NOTE, cursor):
        assert secret not in said
    feed.close()


# Answers


def busy(tag: str, after: int) -> dict[str, object]:
    return failure(f"{tag}/", {"reason": {".tag": tag}, "retry_after": after})


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        (Answer(429, {"Retry-After": "7"}, busy("too_many_requests", 7)), 7.0),
        (Answer(429, {}, busy("too_many_requests", 9)), 9.0),
        (Answer(429, {"Retry-After": "4"}, busy("too_many_write_operations", 4)), 4.0),
        (Answer(429, {}, b"too many"), None),
        (
            Answer(
                409,
                body=failure(
                    "path/too_many_write_operations/",
                    {".tag": "path", "reason": {".tag": "too_many_write_operations"}},
                ),
            ),
            None,
        ),
        (Answer(503, {"Retry-After": "3"}), 3.0),
    ],
)
def test_too_busy_waits_as_asked(
    box: DropboxState, drive: Dropbox, answer: Answer, wait: float | None
) -> None:
    list(drive.list(""))  # signed in
    box.answers.append(answer)

    with pytest.raises(TooBusyError) as raised:
        drive.put(NOTE, b"1", Absent())

    assert raised.value.retry_after == wait
    assert NOTE not in box.files


def test_a_delete_with_too_many_writes_waits(box: DropboxState, drive: Dropbox) -> None:
    list(drive.list(""))
    error = {".tag": "too_many_write_operations"}
    box.answers.append(Answer(409, body=failure("too_many_write_operations/", error)))

    with pytest.raises(TooBusyError):
        drive.delete(NOTE, AsRead(None))


@pytest.mark.parametrize("status", [500, 502, 503, 504, 400, 302, 404])
def test_a_failing_answer_is_for_now(box: DropboxState, drive: Dropbox, status: int) -> None:
    list(drive.list(""))
    box.answers.append(Answer(status, body=b"no"))

    with pytest.raises(UnavailableError) as raised:
        drive.get(NOTE)

    assert raised.value.answered


def test_a_conflict_nobody_foresaw_is_for_now(box: DropboxState, drive: Dropbox) -> None:
    list(drive.list(""))
    box.answers.append(Answer(409, body=failure("other/", {".tag": "other"})))

    with pytest.raises(UnavailableError) as raised:
        drive.get(NOTE)

    assert raised.value.answered


def test_forbidden_stops(box: DropboxState, drive: Dropbox) -> None:
    list(drive.list(""))
    box.answers.append(Answer(403, body=failure("no_permission/", {".tag": "no_permission"})))

    with pytest.raises(NoAccessError):
        drive.get(NOTE)


def test_no_room_left_stops_the_writes(box: DropboxState, drive: Dropbox) -> None:
    rev = drive.put(NOTE, b"1", Absent())
    box.full = True

    with pytest.raises(FullError):
        drive.put("notes/another.json", b"2", Absent())
    with pytest.raises(FullError):
        drive.put(NOTE, b"22", AsRead(rev))
    assert drive.get(NOTE).data == b"1"  # reading goes on


def test_no_answer_is_for_now(told: Told) -> None:
    nowhere = Dropbox(
        APP_KEY,
        FIRST_REFRESH,
        told.renewed.append,
        place=Place(HOST, HOST, HOST, ports=(9, 9, 9)),
        timeout=0.5,
    )

    with pytest.raises(UnavailableError) as raised:
        nowhere.get(NOTE)

    assert not raised.value.answered


def test_a_certificate_no_public_authority_issued_is_sent_nothing(
    box: DropboxState, told: Told
) -> None:
    drive = drive_of(box, told, public=ssl.create_default_context)

    with pytest.raises(UnavailableError):
        drive.get(NOTE)

    assert box.asked == []


def test_the_cloud_time_is_told(drive: Dropbox, told: Told) -> None:
    list(drive.list(""))

    assert told.heard
    date, took = told.heard[-1]
    assert abs(date - time.time() * 1000) < 60_000 and took >= 0


def test_each_request_goes_to_its_host(box: DropboxState, drive: Dropbox) -> None:
    box.hold = 0.1
    drive.put(MINE, b"1", Absent())
    drive.get(MINE)
    list(drive.list("devices"))
    feed = drive.feed("devices")
    cursor = feed.latest()
    feed.wait(cursor)
    feed.since(cursor)
    drive.delete(MINE, AsRead(None))
    drive.revoke()
    feed.close()

    hosts = {(each.host, each.path) for each in box.asked}
    assert hosts == {
        (API, TOKEN_PATH),
        (CONTENT, "/2/files/upload"),
        (CONTENT, "/2/files/download"),
        (API, "/2/files/list_folder"),
        (API, "/2/files/list_folder/get_latest_cursor"),
        (NOTIFY, LONGPOLL),
        (API, "/2/files/list_folder/continue"),
        (API, "/2/files/delete_v2"),
        (API, "/2/auth/token/revoke"),
    }


# Signing out


def test_revoking_signs_out_for_good(box: DropboxState, drive: Dropbox, told: Told) -> None:
    list(drive.list(""))

    drive.revoke()

    assert box.revoked == 1
    with pytest.raises((SignInNeededError, TokenExpiredError)):
        list(drive.list(""))
    again = drive_of(box, told)
    with pytest.raises(SignInNeededError):
        list(again.list(""))


def test_revoking_signed_out_already_sends_nothing(box: DropboxState, drive: Dropbox) -> None:
    box.refusing_refresh = True

    drive.revoke()

    assert box.requests(API, "/2/auth/token/revoke") == []


# The feed


@pytest.fixture
def watched(box: DropboxState) -> str:
    """devices/, with the other device's file in it."""
    box.put_by_hand(THEIRS, b"b")
    return "devices"


def test_a_change_another_device_made_is_told(
    box: DropboxState, drive: Dropbox, watched: str
) -> None:
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.put_by_hand(THEIRS, b"bb")

    assert feed.wait(cursor).changes is True
    others, after = feed.since(cursor)

    assert others is True and after != cursor
    box.hold = 0.1
    assert feed.wait(after).changes is False
    feed.close()


def test_what_this_device_wrote_is_its_own(box: DropboxState, drive: Dropbox, watched: str) -> None:
    feed = drive.feed(watched)
    cursor = feed.latest()
    rev = drive.put(MINE, b"a", Absent())
    drive.put(MINE, b"aa", AsRead(rev))

    assert feed.wait(cursor).changes is True
    assert feed.since(cursor)[0] is False
    feed.close()


def test_a_file_deleted_is_not_its_own(box: DropboxState, drive: Dropbox, watched: str) -> None:
    drive.put(MINE, b"a", Absent())
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.delete_by_hand(MINE)

    assert feed.since(cursor)[0] is True
    feed.close()


def test_the_folder_deleted_is_told(box: DropboxState, drive: Dropbox, watched: str) -> None:
    drive.put(MINE, b"a", Absent())
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.delete_by_hand("devices")

    assert feed.wait(cursor).changes is True
    assert feed.since(cursor)[0] is True
    feed.close()


def test_only_its_folder_is_watched(box: DropboxState, drive: Dropbox, watched: str) -> None:
    box.hold = 0.3
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.put_by_hand(NOTE, b"a note from elsewhere")

    began = time.monotonic()
    assert feed.wait(cursor).changes is False
    assert time.monotonic() - began >= 0.25  # it waited
    feed.close()


def test_the_wait_is_sent_only_the_cursor(box: DropboxState, drive: Dropbox, watched: str) -> None:
    box.hold = 0.1
    feed = drive.feed(watched)
    cursor = feed.latest()

    feed.wait(cursor)

    [waited] = box.requests(NOTIFY)
    assert waited.authorization is None
    assert waited.args == {"cursor": cursor, "timeout": WAIT}
    feed.close()


def test_many_changes_come_page_by_page(box: DropboxState, drive: Dropbox, watched: str) -> None:
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.page = 2
    for number in range(5):
        box.put_by_hand(f"devices/{number:08}.json", b"d")

    others, _after = feed.since(cursor)

    assert others is True
    assert len(box.requests(API, "/2/files/list_folder/continue")) == 3
    feed.close()


def test_a_cursor_too_old_is_told(box: DropboxState, drive: Dropbox, watched: str) -> None:
    feed = drive.feed(watched)
    cursor = feed.latest()
    box.expire_cursors()

    with pytest.raises(CursorExpiredError):
        feed.wait(cursor)
    with pytest.raises(CursorExpiredError):
        feed.since(cursor)
    feed.close()


def test_the_pause_asked_for_is_passed_on(box: DropboxState, drive: Dropbox, watched: str) -> None:
    box.hold, box.backoff = 0.1, 60
    feed = drive.feed(watched)

    assert feed.wait(feed.latest()).backoff == 60.0
    feed.close()


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (Answer(429, {"Retry-After": "30"}, b""), TooBusyError),
        (Answer(400, body=b"Error in call to API function"), UnavailableError),
        (Answer(200, body={"changes": "yes"}), UnavailableError),
        (Answer(500, body=b""), UnavailableError),
    ],
)
def test_a_wait_that_fails(
    box: DropboxState, drive: Dropbox, watched: str, answer: Answer, error: type[Exception]
) -> None:
    feed = drive.feed(watched)
    box.notify_answers.append(answer)

    with pytest.raises(error):
        feed.wait(feed.latest())
    feed.close()


def test_a_folder_not_there_has_no_cursor(drive: Dropbox) -> None:
    feed = drive.feed("devices")

    with pytest.raises(NotThereError):
        feed.latest()
    feed.close()


def test_a_wait_cut_ends_at_once(box: DropboxState, drive: Dropbox, watched: str) -> None:
    box.hold = 30.0
    feed = drive.feed(watched)
    cursor = feed.latest()
    ended: list[BaseException] = []

    def waiting() -> None:
        try:
            feed.wait(cursor)
        except UnavailableError as error:
            ended.append(error)

    thread = threading.Thread(target=waiting)
    thread.start()
    for _ in range(100):
        if box.requests(NOTIFY):
            break
        time.sleep(0.02)
    time.sleep(0.1)
    began = time.monotonic()
    feed.cut()
    thread.join(5)

    assert not thread.is_alive() and ended
    assert time.monotonic() - began < 2
    feed.close()


def test_a_cut_before_the_wait_ends_that_wait_alone(
    box: DropboxState, drive: Dropbox, watched: str
) -> None:
    box.hold = 0.1
    feed = drive.feed(watched)
    cursor = feed.latest()
    feed.cut()

    with pytest.raises(UnavailableError):
        feed.wait(cursor)
    assert box.requests(NOTIFY) == []
    assert feed.wait(cursor).changes is False
    feed.close()
