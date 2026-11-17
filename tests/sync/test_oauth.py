"""Access tokens made from a refresh token (stickle.sync.targets.oauth), against
a sign-in address in the test that answers as Microsoft's does."""

import ssl
from collections.abc import Iterator
from urllib.parse import parse_qs

import pytest
from graph_server import CLIENT_ID, FIRST_REFRESH, LASTS, TOKEN_PATH, Answer, Graph, graph_server

from stickle.sync.targets.base import (
    NoAccessError,
    SignInNeededError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.targets.oauth import EARLY, TokenAddress, Tokens
from stickle.sync.targets.onedrive import SCOPE


@pytest.fixture
def graph() -> Iterator[Graph]:
    with graph_server() as running:
        yield running


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def tokens_of(graph: Graph, renewed: list[str], clock: Clock | None = None) -> Tokens:
    return Tokens(
        graph.place.tokens,
        CLIENT_ID,
        FIRST_REFRESH,
        renewed.append,
        scope=SCOPE,
        public=graph.context,
        clock=clock or Clock(),
    )


def refreshes(graph: Graph) -> list[dict[str, str]]:
    return [
        {name: values[0] for name, values in parse_qs(each.body.decode("ascii")).items()}
        for each in graph.asked
        if each.path == TOKEN_PATH
    ]


def test_an_access_token_is_made_once_and_kept_while_it_lasts(graph: Graph) -> None:
    renewed: list[str] = []
    tokens = tokens_of(graph, renewed)

    first = tokens.access()

    assert tokens.access() == first
    assert first in graph.access_tokens
    [asked] = refreshes(graph)
    assert asked == {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": FIRST_REFRESH,
        "scope": SCOPE,
    }
    assert len(renewed) == 1 and renewed[0] in graph.refresh_tokens


def test_a_new_refresh_token_is_told_at_once_and_used_next(graph: Graph) -> None:
    renewed: list[str] = []
    tokens = tokens_of(graph, renewed)

    tokens.renew()
    tokens.renew()

    assert len(set(renewed)) == 2 and FIRST_REFRESH not in renewed
    assert [each["refresh_token"] for each in refreshes(graph)] == [FIRST_REFRESH, renewed[0]]


def test_one_about_to_run_out_is_made_again(graph: Graph) -> None:
    clock = Clock()
    tokens = tokens_of(graph, [], clock)
    first = tokens.access()

    clock.now += LASTS - EARLY - 1
    assert tokens.access() == first
    clock.now += 1
    assert tokens.access() != first
    assert len(refreshes(graph)) == 2


def test_a_refresh_turned_down_asks_to_sign_in_and_is_not_tried_again(graph: Graph) -> None:
    graph.refusing_refresh = True
    renewed: list[str] = []
    tokens = tokens_of(graph, renewed)

    with pytest.raises(SignInNeededError):
        tokens.access()

    assert len(refreshes(graph)) == 1
    assert renewed == []


def test_the_app_turned_away_is_no_access(graph: Graph) -> None:
    graph.app_turned_away = True

    with pytest.raises(NoAccessError):
        tokens_of(graph, []).access()


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        (Answer(429, {"Retry-After": "7"}), 7.0),
        (Answer(429), None),
        (Answer(503, {"Retry-After": "3"}), 3.0),
    ],
)
def test_too_busy_waits_as_asked(graph: Graph, answer: Answer, wait: float | None) -> None:
    graph.token_answers.append(answer)

    with pytest.raises(TooBusyError) as raised:
        tokens_of(graph, []).access()

    assert raised.value.retry_after == wait


@pytest.mark.parametrize(
    "answer",
    [
        Answer(503),
        Answer(500, body={"error": "server_error"}),
        Answer(400, body={"error": "invalid_request"}),
        Answer(200, body={"token_type": "Bearer"}),  # no token in it
        Answer(200, body={"access_token": "a", "expires_in": "soon"}),
    ],
)
def test_a_failing_answer_is_for_now(graph: Graph, answer: Answer) -> None:
    graph.token_answers.append(answer)

    with pytest.raises(UnavailableError) as raised:
        tokens_of(graph, []).access()

    assert raised.value.answered


def test_a_redirect_is_not_followed(graph: Graph) -> None:
    elsewhere = f"https://127.0.0.1:{graph.port}/elsewhere"
    graph.token_answers.append(Answer(302, {"Location": elsewhere}))

    with pytest.raises(UnavailableError):
        tokens_of(graph, []).access()

    assert [each.path for each in graph.asked] == [TOKEN_PATH]


def test_no_answer_is_for_now(graph: Graph) -> None:
    nowhere = TokenAddress("127.0.0.1", TOKEN_PATH, 9)  # the discard port: nothing there
    tokens = Tokens(nowhere, CLIENT_ID, FIRST_REFRESH, lambda _token: None, timeout=0.5)

    with pytest.raises(UnavailableError) as raised:
        tokens.access()

    assert not raised.value.answered


def test_a_certificate_no_public_authority_issued_is_sent_nothing(graph: Graph) -> None:
    tokens = Tokens(
        graph.place.tokens,
        CLIENT_ID,
        FIRST_REFRESH,
        lambda _token: None,
        public=ssl.create_default_context,  # the test's authority not among them
    )

    with pytest.raises(UnavailableError):
        tokens.access()

    assert graph.asked == []


def test_nothing_says_what_a_token_is(graph: Graph) -> None:
    renewed: list[str] = []
    tokens = tokens_of(graph, renewed)
    access = tokens.access()
    graph.refusing_refresh = True
    with pytest.raises(SignInNeededError) as raised:
        tokens.renew()

    said = f"{tokens!r} {tokens} {raised.value!r} {raised.value}"
    for token in (access, FIRST_REFRESH, *renewed):
        assert token not in said
