import io
import json
import secrets
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from stickle.platform import credential_helper
from stickle.platform.credential_helper import HelperBackend, serve
from stickle.platform.credentials import (
    KEY_BYTES,
    SERVICE,
    CredentialStore,
    CredentialStoreUnavailableError,
)


class FakeBackend:
    """The store in memory, as the platform's would be reached in this process."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


# A stand-in for the helper, speaking its language over the pipes; each start
# is written down in the file it is given. How it behaves: "works", "slow" (never
# says it is ready), "dies" (ready, then ends at the first request), "locked"
# (the store refuses every request).
FAKE_HELPER = textwrap.dedent(
    """
    import json, sys, time
    how, starts = sys.argv[1], sys.argv[2]
    with open(starts, "a") as file:
        file.write("start\\n")
    if how == "slow":
        time.sleep(30)
    print("ready", flush=True)
    items = {}
    for line in sys.stdin:
        if how == "dies":
            sys.exit(3)
        request = json.loads(line)
        if how == "locked":
            print(json.dumps({"error": "KeyringLocked"}), flush=True)
            continue
        key = (request["service"], request["name"])
        if request["op"] == "get":
            print(json.dumps({"value": items.get(key)}), flush=True)
        elif request["op"] == "set":
            items[key] = request["value"]
            print(json.dumps({"value": None}), flush=True)
        else:
            items.pop(key, None)
            print(json.dumps({"value": None}), flush=True)
    """
)


def helper(tmp_path: Path, how: str, fallback: FakeBackend | None = None) -> HelperBackend:
    script = tmp_path / "helper.py"
    script.write_text(FAKE_HELPER)
    command = [sys.executable, str(script), how, str(tmp_path / "starts")]
    return HelperBackend(lambda: fallback or FakeBackend(), command, ready_timeout=5)


def starts(tmp_path: Path) -> int:
    file = tmp_path / "starts"
    return len(file.read_text().splitlines()) if file.exists() else 0


def test_a_key_is_made_and_read_back_through_one_helper(tmp_path: Path) -> None:
    backend = helper(tmp_path, "works")
    store = CredentialStore(backend)

    key = store.get_or_create_key("test-key")  # asks three times: read, write, read

    assert len(key) == KEY_BYTES
    assert store.get_or_create_key("test-key") == key
    assert store.through_helper
    assert starts(tmp_path) == 1  # one process for all of them
    store.close()


def test_a_helper_that_does_not_start_leaves_it_to_this_process(tmp_path: Path) -> None:
    fallback = FakeBackend()
    backend = HelperBackend(lambda: fallback, [str(tmp_path / "no-such-program")], 5)
    store = CredentialStore(backend)

    key = store.get_or_create_key("test-key")

    assert fallback.items[(SERVICE, "test-key")]
    assert store.read("test-key") == key
    assert not store.through_helper


@pytest.mark.parametrize("how", ["slow", "dies"])
def test_a_helper_that_fails_leaves_it_to_this_process(tmp_path: Path, how: str) -> None:
    fallback = FakeBackend()
    backend = helper(tmp_path, how, fallback)
    if how == "slow":
        backend = HelperBackend(lambda: fallback, backend._command, ready_timeout=0.5)  # pyright: ignore[reportPrivateUsage]
    store = CredentialStore(backend)

    key = store.get_or_create_key("test-key")

    assert len(key) == KEY_BYTES
    assert not store.through_helper
    assert fallback.writes == 1


def test_the_stores_own_refusal_is_passed_on_not_tried_again(tmp_path: Path) -> None:
    # Locked is an answer: trying in this process would only ask the person twice.
    fallback = FakeBackend()
    store = CredentialStore(helper(tmp_path, "locked", fallback))

    with pytest.raises(CredentialStoreUnavailableError, match="KeyringLocked"):
        store.read("test-key")
    assert store.through_helper
    assert fallback.writes == 0
    store.close()


def test_the_helper_answers_each_request_and_names_failures_by_kind() -> None:
    backend = FakeBackend()
    requests = [
        {"op": "set", "service": SERVICE, "name": "a", "value": "c2VjcmV0"},
        {"op": "get", "service": SERVICE, "name": "a"},
        {"op": "delete", "service": SERVICE, "name": "a"},
        {"op": "delete", "service": SERVICE, "name": "a"},  # gone already: KeyError
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()

    assert serve(stdin, stdout, lambda: backend) == 0

    lines = stdout.getvalue().splitlines()
    assert lines[0] == "ready"
    assert [json.loads(line) for line in lines[1:]] == [
        {"value": None},
        {"value": "c2VjcmV0"},
        {"value": None},
        {"error": "KeyError"},
    ]


def test_a_helper_ended_by_its_input_does_not_end_the_process_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended = threading.Event()
    monkeypatch.setattr(credential_helper, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(credential_helper, "_end_now", ended.set)
    request = {"op": "get", "service": SERVICE, "name": "a"}

    serve(io.StringIO(json.dumps(request) + "\n"), io.StringIO(), FakeBackend)

    assert not ended.wait(0.5)


def test_no_store_there_is_said_once_asked() -> None:
    def missing() -> FakeBackend:
        raise CredentialStoreUnavailableError("RuntimeError: no Secret Service")

    stdout = io.StringIO()
    request = {"op": "get", "service": SERVICE, "name": "a"}

    serve(io.StringIO(json.dumps(request) + "\n"), stdout, missing)

    assert json.loads(stdout.getvalue().splitlines()[1]) == {
        "error": "RuntimeError: no Secret Service"
    }


def test_the_real_helper_keeps_the_stores_libraries_out_of_this_process() -> None:
    # In a fresh process: this one has loaded keyring for other tests.
    code = textwrap.dedent(
        f"""
        import sys
        from stickle.platform.credentials import CredentialStore, CredentialStoreUnavailableError
        store = CredentialStore()
        try:
            print("absent", store.read("test-{secrets.token_hex(8)}") is None)
        except CredentialStoreUnavailableError as error:
            print("unavailable", error)
        print("helper", store.through_helper)
        store.close()
        print("keyring", any(name.startswith("keyring") for name in sys.modules))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    ).stdout.splitlines()

    assert out[0] in ("absent True",) or out[0].startswith("unavailable")
    assert out[1] == "helper True"
    assert out[2] == "keyring False"
