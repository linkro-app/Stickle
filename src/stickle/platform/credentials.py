"""Keep secrets in the operating system's credential store.

Windows Credential Manager (protected by DPAPI), the macOS Keychain, or a
Secret Service provider on Linux (GNOME Keyring, KWallet). The backend is
chosen explicitly per platform: keyring's automatic discovery reads package
metadata that a compiled build does not carry.

Nothing here ever falls back to a file. When the store is missing, locked,
or its unlock prompt is dismissed, CredentialStoreUnavailableError is raised and
the caller decides (later: ask for a password instead).

The store is reached from a helper process (stickle.platform.credential_helper),
so that its libraries never stay in Stickle's memory; here only if that fails.
"""

import base64
import binascii
import secrets
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from stickle.platform.credential_helper import HelperBackend, StoreReplyError

if TYPE_CHECKING:
    from keyring.backend import KeyringBackend

SERVICE = "co.linkro.stickle"
DATABASE_KEY = "local-database-key"
KEY_BYTES = 32


class CredentialStoreUnavailableError(Exception):
    """No usable credential store: none installed, locked, or access refused."""


class KeyMissingError(Exception):
    """The store answered, and it holds no key, but creating one was not allowed."""


class Backend(Protocol):
    """The part of a keyring backend this module uses."""

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class _KeyringAdapter:
    """Presents a keyring backend with plain types (its own annotations are looser)."""

    def __init__(self, backend: KeyringBackend) -> None:
        self._backend = backend

    def get_password(self, service: str, username: str) -> str | None:
        value = self._backend.get_password(service, username)
        return None if value is None else str(value)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._backend.set_password(service, username, password)  # pyright: ignore[reportUnknownMemberType]

    def delete_password(self, service: str, username: str) -> None:
        self._backend.delete_password(service, username)


def platform_backend() -> Backend:
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            return _KeyringAdapter(WinVaultKeyring())
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring as MacKeyring

            return _KeyringAdapter(MacKeyring())
        from keyring.backends.SecretService import Keyring as SecretServiceKeyring

        # Raises when there is no session bus or no Secret Service provider.
        _ = SecretServiceKeyring.priority
        return _KeyringAdapter(SecretServiceKeyring())
    except Exception as error:
        # keyring raises RuntimeError both for "no service running" and "library
        # missing"; its message tells them apart (and contains no secrets).
        raise CredentialStoreUnavailableError(f"{type(error).__name__}: {error}") from error


def bundled_support_modules() -> list[str]:
    """Modules the platform's credential store needs; a build must contain them."""
    if sys.platform == "win32":
        return ["keyring.backends.Windows", "win32ctypes"]
    if sys.platform == "darwin":
        return ["keyring.backends.macOS"]
    return ["keyring.backends.SecretService", "secretstorage", "jeepney", "cryptography"]


def default_backend() -> Backend:
    """The platform's store, reached from the helper process (or here, should
    the helper not work)."""
    return HelperBackend(platform_backend)


def _kind(error: Exception) -> str:
    """How the store failed, without its message (it may name the secret)."""
    return error.kind if isinstance(error, StoreReplyError) else type(error).__name__


class CredentialStore:
    def __init__(self, backend: Backend | None = None) -> None:
        self._backend = backend if backend is not None else default_backend()

    def close(self) -> None:
        """Done with the store: its helper process may end."""
        if isinstance(self._backend, HelperBackend):
            self._backend.close()

    @property
    def through_helper(self) -> bool | None:
        """Whether it was reached through the helper process; None: never meant to be."""
        return self._backend.through_helper if isinstance(self._backend, HelperBackend) else None

    def read(self, name: str) -> bytes | None:
        """The stored secret, or None only when the store says it does not exist."""
        try:
            value = self._backend.get_password(SERVICE, name)
        except Exception as error:
            # Locked, prompt dismissed, bus gone, ...: unknown is not "absent".
            raise CredentialStoreUnavailableError(_kind(error)) from error
        if value is None:
            return None
        try:
            return base64.b64decode(value, validate=True)
        except binascii.Error as error:
            raise CredentialStoreUnavailableError("stored value is not ours") from error

    def write(self, name: str, secret: bytes) -> None:
        try:
            self._backend.set_password(SERVICE, name, base64.b64encode(secret).decode("ascii"))
        except Exception as error:
            raise CredentialStoreUnavailableError(_kind(error)) from error

    def delete(self, name: str) -> None:
        try:
            self._backend.delete_password(SERVICE, name)
        except Exception as error:
            raise CredentialStoreUnavailableError(_kind(error)) from error

    def get_or_create_key(self, name: str = DATABASE_KEY, create: bool = True) -> bytes:
        """Return the key, creating it only when the store confirms there is none.

        An existing key is never replaced: that would make the database it
        encrypts unreadable for good. Callers pass create=False once a database
        exists: a new key could never open it, so a missing key is an error.
        """
        existing = self.read(name)
        if existing is not None:
            if len(existing) != KEY_BYTES:
                raise CredentialStoreUnavailableError("stored key has the wrong length")
            return existing
        if not create:
            raise KeyMissingError(name)
        key = secrets.token_bytes(KEY_BYTES)
        self.write(name, key)
        if self.read(name) != key:
            raise CredentialStoreUnavailableError("the store did not keep the key")
        return key


class KeyRequest:
    """Fetch (or create) the database key in the background while the app starts.

    Talking to the credential store can take a third of a second on a cold
    start (loading its libraries, a D-Bus round trip on Linux). Started first
    thing, it overlaps loading Qt instead of adding to it. The rules are those
    of get_or_create_key; only the timing changes.
    """

    def __init__(
        self,
        name: str = DATABASE_KEY,
        store: Callable[[], CredentialStore] = CredentialStore,
        create: bool = True,
    ) -> None:
        self._key: bytes | None = None
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._fetch, args=(name, store, create), name="stickle-key", daemon=True
        )
        self._thread.start()

    def _fetch(self, name: str, store: Callable[[], CredentialStore], create: bool) -> None:
        made: CredentialStore | None = None
        try:
            made = store()
            self._key = made.get_or_create_key(name, create)
        except BaseException as error:  # handed to whoever asks for the key
            self._error = error
        finally:
            self._done.set()
            if made is not None:
                made.close()  # after: the app does not wait for the helper to end

    def key(self) -> bytes:
        """Wait for the key; raises what fetching it raised."""
        self._done.wait()
        if self._error is not None:
            raise self._error
        assert self._key is not None
        return self._key
