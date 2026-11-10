"""What the end of a round tells whoever runs sync: whether, and when, to
try again, and what stopped it until the user does something.

- No answer, or the cloud failing for now; vault.json half read: tried again,
  waiting longer each time it fails running (15 seconds, then twice as long,
  up to five minutes).
- Too many requests: tried again after as long as the cloud asked
  (Retry-After, however long), or as above.
- The cloud full: the round goes on reading and puts nothing more once a put
  is turned down; what was not put waits here. Rounds go on as usual, each
  trying once to put again.
- Sign-in needed again, or the app's access withdrawn: sync with that cloud
  stops, and is not tried again by itself.
- The vault gone, made again, of a newer format, or locked with a key this
  device does not have: sync with that vault stops.
- A big change it would have brought: sync with that vault stops until the
  user lets it take that in. Looked at again when the app starts.
- A server showing a certificate that is not the one trusted: sync with that
  server stops, and the user looks at the certificate.

A round that went through, full or not, resets the waits.
"""

from dataclasses import dataclass
from enum import Enum

from stickle.sync.engine import BigChangeError
from stickle.sync.format import NewerFormatError, UnknownKeyError, UnreadableError
from stickle.sync.targets.base import (
    CertificateError,
    NoAccessError,
    SignInNeededError,
    TokenExpiredError,
    TooBusyError,
    UnavailableError,
)
from stickle.sync.vault import VaultChangedError, VaultGoneError

FIRST_WAIT = 15.0  # seconds
LONGEST_WAIT = 5 * 60.0


class Stopped(Enum):
    """Why sync stopped until the user does something."""

    SIGN_IN = "sign in"  # the cloud wants the user to sign in again
    NO_ACCESS = "no access"  # the app may not use the cloud any more
    VAULT_GONE = "vault gone"
    VAULT_CHANGED = "vault changed"  # made again, from another device
    NEWER = "newer"  # a newer version's: an update goes on
    BIG_CHANGE = "big change"  # many notes at once going from sight, or conflict copies
    CERTIFICATE = "certificate"  # a server's certificate is not the one trusted


@dataclass(frozen=True)
class Outcome:
    stopped: Stopped | None = None
    wait: float | None = None  # seconds to the next try, when it failed for now; None: as usual
    failures: int = 0  # rounds running that failed for now
    full: bool = False  # nothing put: the cloud is full
    offline: bool = False  # it failed for now, with no answer at all


def after(error: BaseException | None, failures: int = 0, full: bool = False) -> Outcome:
    """The outcome of a round that ended with error (None: it went through;
    full: it put nothing, the cloud full), after failures rounds running that
    failed for now. An error it does not know of is raised again."""
    match error:
        case None:
            return Outcome(full=full)
        case SignInNeededError():
            return Outcome(Stopped.SIGN_IN)
        case NoAccessError():
            return Outcome(Stopped.NO_ACCESS)
        case VaultGoneError():
            return Outcome(Stopped.VAULT_GONE)
        case VaultChangedError():
            return Outcome(Stopped.VAULT_CHANGED)
        case NewerFormatError() | UnknownKeyError():
            return Outcome(Stopped.NEWER)
        case BigChangeError():
            return Outcome(Stopped.BIG_CHANGE)
        case CertificateError():
            return Outcome(Stopped.CERTIFICATE)
        case TooBusyError(retry_after=asked) if asked is not None:
            return Outcome(wait=max(asked, 0.0), failures=failures + 1)
        case UnavailableError(answered=False):
            return Outcome(wait=_wait(failures + 1), failures=failures + 1, offline=True)
        case TooBusyError() | UnavailableError() | TokenExpiredError() | UnreadableError():
            return Outcome(wait=_wait(failures + 1), failures=failures + 1)
        case _:
            raise error


def _wait(failures: int) -> float:
    return min(FIRST_WAIT * 2 ** (failures - 1), LONGEST_WAIT)
