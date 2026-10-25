"""Open the notes database at start-up, asking the user only when needed.

The key comes from the credential store without any question, or from a
password. When the notes cannot be opened, the recovery dialog explains why;
nothing is created, replaced or deleted on the way. When the key is out of
reach (lost from the credential store, a damaged key file, a forgotten
password), the recovery key opens the notes, and only once it has, the key
is put back: into the credential store, or under a new password.
"""

import logging
from collections.abc import Callable
from pathlib import Path

import apsw

from stickle.app.password_dialog import PasswordDialog
from stickle.app.recovery_dialog import Choice, Problem, RecoveryDialog
from stickle.app.recovery_key_dialog import EnterRecoveryKeyDialog
from stickle.crypto.keyfile import KeyFileError, WrongPasswordError
from stickle.crypto.recovery import RecoveryKeyTypoError, WrongRecoveryKeyError
from stickle.data.database import WrongKeyError
from stickle.data.export import export_markdown
from stickle.data.schema import MigrationError, NewerSchemaError, open_store, schema_version
from stickle.platform.credentials import CredentialStoreUnavailableError
from stickle.unlock import Blocked, NeedPassword, Unlock, Unlocked

log = logging.getLogger(__name__)

# Replaceable in tests.
type AskPassword = Callable[[PasswordDialog], bool]
type AskRecovery = Callable[[RecoveryDialog], Choice]
type AskRecoveryKey = Callable[[EnterRecoveryKeyDialog], bool]


def _exec(dialog: PasswordDialog) -> bool:
    return dialog.exec() == PasswordDialog.DialogCode.Accepted


def _run(dialog: RecoveryDialog) -> Choice:
    return dialog.run()


def _exec_key(dialog: EnterRecoveryKeyDialog) -> bool:
    return dialog.exec() == EnterRecoveryKeyDialog.DialogCode.Accepted


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _password_key(unlock: Unlock, create: bool, ask: AskPassword) -> tuple[bytes | None, bool]:
    """The key from a password, and whether the user said they forgot it instead."""
    key: bytes | None = None
    dialog: PasswordDialog

    def submit(password: str) -> str | None:
        nonlocal key
        try:
            key = unlock.create_password(password) if create else unlock.enter_password(password)
        except WrongPasswordError:
            return dialog.wrong_password()
        except (KeyFileError, OSError) as error:
            log.error("password key failed: %s", _describe(error))
            return dialog.key_file_failed(type(error).__name__)
        return None

    dialog = PasswordDialog(
        create, submit, can_recover=unlock.has_recovery_key, portable=unlock.portable
    )
    accepted = ask(dialog)
    return (key if accepted else None), dialog.forgot


def _recovery_key(unlock: Unlock, ask: AskRecoveryKey) -> bytes | None:
    """The key from the recovery key, or None if the user went back."""
    key: bytes | None = None
    dialog: EnterRecoveryKeyDialog

    def submit(typed: str) -> str | None:
        nonlocal key
        try:
            key = unlock.open_with_recovery_key(typed)
        except RecoveryKeyTypoError:
            return dialog.typo()
        except WrongRecoveryKeyError:
            return dialog.wrong_key()
        except (KeyFileError, OSError) as error:
            log.error("recovery file failed: %s", _describe(error))
            return dialog.no_recovery_key()
        return None

    dialog = EnterRecoveryKeyDialog(submit)
    return key if ask(dialog) else None


def _keep_recovered_key(unlock: Unlock, key: bytes, ask_password: AskPassword) -> None:
    """Put a key the recovery key found (and that opened the notes) back in its place."""
    if not unlock.recovered_key_needs_password:
        try:
            unlock.keep_recovered_key(key)
            log.info("recovered key kept in the credential store")
            return
        except CredentialStoreUnavailableError as error:
            log.warning("credential store still unavailable (%s): a password instead", error)
    dialog: PasswordDialog

    def submit(password: str) -> str | None:
        try:
            unlock.set_password(key, password)
        except (KeyFileError, OSError) as error:
            log.error("new password failed: %s", _describe(error))
            return dialog.key_file_failed(type(error).__name__)
        return None

    dialog = PasswordDialog(True, submit, after_recovery=True)
    if ask_password(dialog):
        log.info("recovered key kept under a new password")
    else:
        # The notes open this time; the recovery key is needed again next time.
        log.warning("recovered key not kept: no new password")


def _open_problem(error: Exception) -> Problem:
    if isinstance(error, WrongKeyError):
        return Problem("wrong_key", _describe(error))
    if isinstance(error, NewerSchemaError):
        return Problem("newer_version", _describe(error))
    if isinstance(error, MigrationError):
        return Problem("upgrade_failed", _describe(error), error.diff)
    return Problem("open_failed", _describe(error))


def open_notes(
    unlock: Unlock,
    ask_password: AskPassword = _exec,
    ask_recovery: AskRecovery = _run,
    ask_recovery_key: AskRecoveryKey = _exec_key,
) -> apsw.Connection | None:
    """The open database, or None when the user chose to quit.

    On success unlock.opened_key holds the key that opened it.
    """
    folder: Path = unlock.folder
    while True:
        outcome = unlock.outcome()
        recovered = False
        if isinstance(outcome, Blocked):
            log.warning("notes cannot be unlocked: %s", outcome.problem)
            dialog = RecoveryDialog(
                Problem(outcome.problem), folder, can_recover=unlock.has_recovery_key
            )
            choice = ask_recovery(dialog)
            if choice == Choice.QUIT:
                return None
            if choice == Choice.RETRY:
                unlock.start()
                continue
            found = _recovery_key(unlock, ask_recovery_key)
            if found is None:
                continue
            key, source, recovered = found, "recovery key", True
        elif isinstance(outcome, NeedPassword):
            found, forgot = _password_key(unlock, outcome.create, ask_password)
            if forgot:
                found = _recovery_key(unlock, ask_recovery_key)
                if found is None:
                    continue
                recovered = True
            elif found is None:
                log.info("quit at the password prompt")
                return None
            key, source = found, "recovery key" if recovered else "password"
        else:
            assert isinstance(outcome, Unlocked)
            key, source = outcome.key, outcome.source
        while True:
            try:
                connection = open_store(unlock.database, key)
            except (WrongKeyError, NewerSchemaError, MigrationError, apsw.Error) as error:
                problem = _open_problem(error)
                log.error("notes could not be opened: %s", problem.error)
                can_export = problem.kind in {"newer_version", "upgrade_failed", "open_failed"}
                dialog = RecoveryDialog(
                    problem,
                    folder,
                    (lambda target, key=key: export_markdown(unlock.database, key, target))
                    if can_export and unlock.database.exists()
                    else None,
                )
                if ask_recovery(dialog) == Choice.QUIT:
                    return None
                continue
            log.info(
                "notes opened (key from the %s, schema v%d)", source, schema_version(connection)
            )
            if recovered:
                _keep_recovered_key(unlock, key, ask_password)
            unlock.opened_key = key
            unlock.keep_copies(connection)
            return connection
