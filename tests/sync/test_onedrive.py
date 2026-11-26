"""The OneDrive target (stickle.sync.targets.onedrive) against Graph in the test,
over real HTTPS: what it asks, what it does with each answer, and where its
tokens go. What every target answers alike is in test_target_contract."""

import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest
from graph_server import (
    APPROOT,
    CLIENT_ID,
    FIRST_REFRESH,
    HOST,
    TOKEN_PATH,
    Answer,
    Graph,
    graph_server,
)

from stickle.sync.targets import onedrive
from stickle.sync.targets.base import (
    Absent,
    AsRead,
    FullError,
    NoAccessError,
    NotThereError,
    RefusedError,
    SignInNeededError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.onedrive import (
    CHUNK,
    CONTENT_HOSTS,
    IDLE_REOPEN,
    PAGE,
    OneDrive,
    Place,
    _hosted,  # pyright: ignore[reportPrivateUsage]
)

NOTE = "notes/0f8e6f3c-1d9b-4a63-9c38-3b2a4f2f7d10.json"


@dataclass
class Told:
    renewed: list[str] = field(default_factory=list[str])
    heard: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])


@pytest.fixture
def graph() -> Iterator[Graph]:
    with graph_server() as running:
        yield running


@pytest.fixture
def told() -> Told:
    return Told()


def drive_of(
    graph: Graph,
    told: Told,
    *,
    folder: str = "",
    public: Callable[[], ssl.SSLContext] | None = None,
    chunk: int = CHUNK,
) -> OneDrive:
    return OneDrive(
        CLIENT_ID,
        FIRST_REFRESH,
        told.renewed.append,
        place=graph.place,
        folder=folder,
        public=graph.context if public is None else public,
        heard=lambda date, took: told.heard.append((date, took)),
        chunk=chunk,
    )


@pytest.fixture
def drive(graph: Graph, told: Told) -> Iterator[OneDrive]:
    made = drive_of(graph, told)
    yield made
    made.close()


def to_graph(graph: Graph) -> int:
    return len(graph.graph_requests())


# Listing


def test_a_folder_not_made_yet_lists_nothing(drive: OneDrive) -> None:
    assert list(drive.list("notes")) == []
    assert list(drive.list("")) == []


@pytest.mark.parametrize("by_id", [False, True], ids=["by-path", "by-id"])
def test_a_long_listing_comes_page_by_page(graph: Graph, drive: OneDrive, by_id: bool) -> None:
    graph.next_link_by_id = by_id
    names = {f"notes/{number:04}.json" for number in range(2 * PAGE + 50)}
    for name in names:
        graph.put_by_hand(name, b"n")

    listed = {entry.path: entry for entry in drive.list("notes")}

    assert set(listed) == names
    assert all(entry.tag == graph.files[path].tag for path, entry in listed.items())
    pages = [each for each in graph.graph_requests() if "/children" in each.path]
    assert len(pages) == 3


def test_folders_and_odd_names_are_left_out(graph: Graph, drive: OneDrive) -> None:
    graph.put_by_hand("notes/a.json", b"aa")
    graph.put_by_hand("notes/inside/b.json", b"b")
    graph.also_listed["notes"] = [
        {"name": "..", "size": 1, "eTag": '"1"', "file": {}},
        {"name": "c\\d.json", "size": 1, "eTag": '"2"', "file": {}},
        {"name": "no-file-facet.json", "size": 1, "eTag": '"3"'},
        {"size": 1, "eTag": '"4"', "file": {}},
    ]

    assert [(e.path, e.size) for e in drive.list("notes")] == [("notes/a.json", 2)]


def test_a_next_page_elsewhere_is_not_followed(graph: Graph, drive: OneDrive) -> None:
    for number in range(PAGE + 1):
        graph.put_by_hand(f"notes/{number}.json", b"n")
    graph.next_link_host = "localhost"  # the same server, under a name not Graph's

    with pytest.raises(UnavailableError):
        list(drive.list("notes"))

    assert all(each.host != f"localhost:{graph.port}" for each in graph.asked)


def test_a_next_page_outside_graphs_api_is_not_followed(graph: Graph, drive: OneDrive) -> None:
    list(drive.list(""))  # signed in
    elsewhere = f"https://{HOST}:{graph.port}/elsewhere/children?$skiptoken=1"
    graph.answers.append(Answer(200, body={"value": [], "@odata.nextLink": elsewhere}))

    with pytest.raises(UnavailableError):
        list(drive.list("notes"))

    assert not any(each.path.startswith("/elsewhere") for each in graph.asked)


# Reading and writing


def test_a_read_gives_the_file_and_its_tag(graph: Graph, drive: OneDrive) -> None:
    tag = graph.put_by_hand(NOTE, b"from elsewhere")

    read = drive.get(NOTE)

    assert (read.data, read.tag) == (b"from elsewhere", tag)
    [download] = [each for each in graph.asked if each.path.startswith("/download/")]
    assert download.authorization is None


def test_a_folder_is_no_file(graph: Graph, drive: OneDrive) -> None:
    graph.put_by_hand(NOTE, b"n")

    with pytest.raises(NotThereError):
        drive.get("notes")


def test_a_file_made_fails_if_one_is_there(graph: Graph, drive: OneDrive) -> None:
    graph.put_by_hand(NOTE, b"from elsewhere")

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"mine", Absent())

    assert graph.files[NOTE].data == b"from elsewhere"
    [made] = [each for each in graph.asked if each.method == "PUT"]
    assert made.path.endswith(":/content?@microsoft.graph.conflictBehavior=fail")


def test_a_file_written_over_only_as_read(graph: Graph, drive: OneDrive) -> None:
    first = drive.put(NOTE, b"1", Absent())
    graph.put_by_hand(NOTE, b"from elsewhere")

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(first))

    assert graph.files[NOTE].data == b"from elsewhere"
    now = drive.get(NOTE).tag
    again = drive.put(NOTE, b"22", AsRead(now))
    assert graph.files[NOTE].data == b"22" and again == graph.files[NOTE].tag != now


def test_a_file_written_over_gone_with_its_folder_is_refused(graph: Graph, drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    graph.files.clear()  # its folder with it

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(tag))

    assert graph.files == {}


@pytest.mark.parametrize("chunk", [CHUNK, 1000], ids=["whole", "in-parts"])
def test_a_file_written_after_its_upload_session_was_made_is_refused(
    graph: Graph, told: Told, chunk: int
) -> None:
    drive = drive_of(graph, told, chunk=chunk)
    tag = drive.put(NOTE, b"1", Absent())
    graph.meanwhile[NOTE] = b"from elsewhere"

    with pytest.raises(RefusedError):
        drive.put(NOTE, bytes(range(256)) * 10, AsRead(tag))

    assert graph.files[NOTE].data == b"from elsewhere"


def test_a_file_deleted_after_its_upload_session_was_made_is_refused(
    graph: Graph, drive: OneDrive
) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    graph.meanwhile[NOTE] = None

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(tag))

    assert graph.files == {}  # not made again


def test_a_file_written_over_without_a_tag_is_refused_unsent(graph: Graph, drive: OneDrive) -> None:
    drive.put(NOTE, b"1", Absent())
    before = to_graph(graph)

    with pytest.raises(RefusedError):
        drive.put(NOTE, b"22", AsRead(None))

    assert to_graph(graph) == before
    assert graph.files[NOTE].data == b"1"


def test_a_delete_without_a_tag_deletes_as_it_is(graph: Graph, drive: OneDrive) -> None:
    # As a device takes its own file off when it leaves: only it writes that file.
    graph.put_by_hand("devices/aaaaaaaa.json", b"d")

    drive.delete("devices/aaaaaaaa.json", AsRead(None))

    assert graph.files == {}


def test_a_delete_with_a_tag_only_as_read(graph: Graph, drive: OneDrive) -> None:
    first = graph.put_by_hand(NOTE, b"1")
    graph.put_by_hand(NOTE, b"22")

    with pytest.raises(RefusedError):
        drive.delete(NOTE, AsRead(first))
    assert NOTE in graph.files


def test_a_large_file_goes_in_parts(graph: Graph, told: Told) -> None:
    drive = drive_of(graph, told, chunk=1000)
    tag = drive.put(NOTE, b"1", Absent())
    data = bytes(range(256)) * 10

    drive.put(NOTE, data, AsRead(tag))

    assert drive.get(NOTE).data == data
    parts = [each for each in graph.asked if each.path.startswith("/upload/")]
    assert [len(each.body) for each in parts] == [1000, 1000, 560]
    drive.close()


def test_nothing_is_no_file_to_write(drive: OneDrive) -> None:
    with pytest.raises(ValueError):
        drive.put(NOTE, b"", Absent())


def test_paths_stay_in_its_folder(graph: Graph, told: Told) -> None:
    drive = drive_of(graph, told, folder="a-test")
    drive.put(NOTE, b"1", Absent())

    assert set(graph.files) == {f"a-test/{NOTE}"}
    assert [entry.path for entry in drive.list("notes")] == [NOTE]
    for elsewhere in ("../vault.json", "notes/../../x", "notes\\x", "/vault.json", "notes//x"):
        with pytest.raises(ValueError):
            drive.get(elsewhere)
    with pytest.raises(ValueError):
        drive_of(graph, told, folder="..")
    with pytest.raises(ValueError):
        drive.delete("", AsRead(None))  # the folder itself
    assert set(graph.files) == {f"a-test/{NOTE}"}
    drive.close()


# Tokens


def test_an_access_token_turned_down_is_made_again_once(graph: Graph, drive: OneDrive) -> None:
    list(drive.list(""))
    graph.expire_access_tokens()

    drive.put(NOTE, b"1", Absent())

    assert graph.files[NOTE].data == b"1"
    assert len([each for each in graph.asked if each.path == TOKEN_PATH]) == 2


def test_the_new_refresh_tokens_are_told_at_once(graph: Graph, drive: OneDrive, told: Told) -> None:
    list(drive.list(""))
    assert len(told.renewed) == 1
    graph.expire_access_tokens()
    list(drive.list(""))

    assert len(set(told.renewed)) == 2
    assert all(token in graph.refresh_tokens for token in told.renewed)


def test_an_access_token_turned_down_again_is_out_of_date(graph: Graph, drive: OneDrive) -> None:
    graph.turning_down_access = True

    with pytest.raises(TokenExpiredError):
        drive.get(NOTE)

    assert to_graph(graph) == 2


def test_a_refresh_turned_down_stops_to_sign_in(graph: Graph, drive: OneDrive) -> None:
    graph.refusing_refresh = True

    with pytest.raises(SignInNeededError):
        list(drive.list("notes"))

    assert to_graph(graph) == 0


def test_the_app_turned_away_stops(graph: Graph, drive: OneDrive) -> None:
    graph.app_turned_away = True

    with pytest.raises(NoAccessError):
        drive.get(NOTE)


def test_the_tokens_go_only_where_they_belong(graph: Graph, drive: OneDrive, told: Told) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    drive.put(NOTE, b"22", AsRead(tag))
    read = drive.get(NOTE)
    list(drive.list("notes"))
    graph.expire_access_tokens()
    drive.delete(NOTE, AsRead(read.tag))

    refresh = {FIRST_REFRESH, *told.renewed}
    for asked in graph.asked:
        carried = f"{asked.authorization} {asked.path} {asked.body!r}"
        if asked.path == TOKEN_PATH:
            assert asked.authorization is None
            assert not any(token in carried for token in graph.issued)
        elif asked.path.startswith("/v1.0/"):
            assert (asked.authorization or "").removeprefix("Bearer ") in graph.issued
            assert not any(token in carried for token in refresh)
        else:  # downloads and uploads
            assert asked.authorization is None
            assert not any(token in carried for token in refresh | set(graph.issued))


def test_nothing_it_says_tells_a_token(graph: Graph, drive: OneDrive, told: Told) -> None:
    drive.put(NOTE, b"1", Absent())
    graph.turning_down_access = True
    with pytest.raises(TokenExpiredError) as raised:
        drive.get(NOTE)

    said = f"{drive!r} {raised.value!r} {raised.value}"
    for token in (FIRST_REFRESH, *told.renewed, *graph.issued):
        assert token not in said
    assert NOTE not in said


# Answers


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        (Answer(429, {"Retry-After": "7"}), 7.0),
        (Answer(503, {"Retry-After": "3"}), 3.0),
        (Answer(509), None),
    ],
)
def test_too_busy_waits_as_asked(
    graph: Graph, drive: OneDrive, answer: Answer, wait: float | None
) -> None:
    list(drive.list(""))  # signed in
    graph.answers.append(answer)

    with pytest.raises(TooBusyError) as raised:
        drive.get(NOTE)

    assert raised.value.retry_after == wait


@pytest.mark.parametrize("status", [500, 502, 503, 504, 400, 302])
def test_a_failing_answer_is_for_now(graph: Graph, drive: OneDrive, status: int) -> None:
    list(drive.list(""))
    graph.answers.append(Answer(status))

    with pytest.raises(UnavailableError) as raised:
        drive.get(NOTE)

    assert raised.value.answered


def test_forbidden_stops(graph: Graph, drive: OneDrive) -> None:
    list(drive.list(""))
    graph.answers.append(Answer(403, body={"error": {"code": "accessDenied"}}))

    with pytest.raises(NoAccessError):
        drive.get(NOTE)


def test_no_room_left_stops_the_writes(graph: Graph, drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    graph.full = True

    with pytest.raises(FullError):
        drive.put("notes/another.json", b"2", Absent())
    with pytest.raises(FullError):
        drive.put(NOTE, b"22", AsRead(tag))
    assert drive.get(NOTE).data == b"1"  # reading goes on


def test_no_answer_is_for_now(told: Told) -> None:
    nowhere = OneDrive(
        CLIENT_ID, FIRST_REFRESH, told.renewed.append, place=Place(HOST, HOST, port=9), timeout=0.5
    )

    with pytest.raises(UnavailableError) as raised:
        nowhere.get(NOTE)

    assert not raised.value.answered


def test_a_certificate_no_public_authority_issued_is_sent_nothing(graph: Graph, told: Told) -> None:
    drive = drive_of(graph, told, public=ssl.create_default_context)

    with pytest.raises(UnavailableError):
        drive.get(NOTE)

    assert graph.asked == []


def test_the_cloud_time_is_told(graph: Graph, drive: OneDrive, told: Told) -> None:
    list(drive.list(""))

    assert told.heard
    date, took = told.heard[-1]
    assert abs(date - time.time() * 1000) < 60_000 and took >= 0


# Download and upload addresses


def test_addresses_not_microsofts_are_sent_nothing(graph: Graph, drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    graph.content_host = "localhost"  # the same server, under a name not Microsoft's

    with pytest.raises(UnavailableError):
        drive.get(NOTE)
    with pytest.raises(UnavailableError):
        drive.put(NOTE, b"22", AsRead(tag))

    assert not any(each.path.startswith(("/download/", "/upload/")) for each in graph.asked)
    assert graph.files[NOTE].data == b"1"


def test_an_address_on_another_port_is_sent_nothing(graph: Graph, drive: OneDrive) -> None:
    tag = drive.put(NOTE, b"1", Absent())
    graph.content_port = graph.port + 1

    for done in (lambda: drive.get(NOTE), lambda: drive.put(NOTE, b"22", AsRead(tag))):
        with pytest.raises(UnavailableError) as raised:
            done()
        assert raised.value.answered  # turned down here, not tried


def test_a_download_address_not_over_https_is_sent_nothing(graph: Graph, drive: OneDrive) -> None:
    list(drive.list(""))
    item: dict[str, object] = {
        "name": "x.json",
        "eTag": '"{A},1"',
        "file": {},
        "@microsoft.graph.downloadUrl": f"http://{HOST}:{graph.port}/download/x",
    }
    graph.answers.append(Answer(200, body=item))

    with pytest.raises(UnavailableError):
        drive.get(NOTE)

    assert not any(each.path.startswith("/download/") for each in graph.asked)


@pytest.mark.parametrize(
    ("host", "theirs"),
    [
        ("public.bn.files.1drv.com", True),
        ("PUBLIC.BN.FILES.1DRV.COM", True),
        ("my.microsoftpersonalcontent.com", True),
        ("api.onedrive.com", True),
        ("1drv.com", False),
        ("evil1drv.com", False),
        ("1drv.com.example.net", False),
        ("x.api.onedrive.com", False),
        ("graph.microsoft.com", False),
    ],
)
def test_microsofts_hosts_for_files(host: str, theirs: bool) -> None:
    assert _hosted(host, CONTENT_HOSTS) is theirs


def test_requests_go_under_the_apps_folder(graph: Graph, drive: OneDrive) -> None:
    drive.put(NOTE, b"1", Absent())
    drive.get(NOTE)
    list(drive.list("notes"))

    for asked in graph.graph_requests():
        assert asked.path.startswith(APPROOT), asked.path
        assert ":?" not in asked.path and not asked.path.endswith(":")  # nothing after it


# A connection kept between requests


def test_a_connection_left_idle_a_while_is_not_used_again(
    graph: Graph, drive: OneDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cut off unnoticed as the network changed, it would wait the whole time out.
    now = [0.0]
    monkeypatch.setattr(onedrive, "_idle_now", lambda: now[0])
    list(drive.list("notes"))
    kept = drive._kept  # pyright: ignore[reportPrivateUsage]
    assert kept is not None
    now[0] += IDLE_REOPEN - 1
    list(drive.list("notes"))
    assert drive._kept is kept  # pyright: ignore[reportPrivateUsage]
    now[0] += IDLE_REOPEN + 1
    list(drive.list("notes"))
    assert drive._kept not in (None, kept)  # pyright: ignore[reportPrivateUsage]
