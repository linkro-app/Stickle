"""Whether a password is strong enough to be a master password, or the secret
notes' password.

Either can be tried against copies of the files it opens, so a guessable one
is refused, with the reason in one line (no strength meter):

- among the most common passwords (common_passwords/), also with digits or
  symbols added at the end, and Korean ones also as typed on an English
  keyboard with the Korean input off;
- one character, or a few, over and over, or a run of the keyboard or the
  alphabet;
- a master password shorter than 12 characters, unless it is 4 words or more.
  The secret notes' password, typed more often, needs 8 characters
  (stickle.crypto.secret_notes).

Being the same as another password of Stickle's is checked where those are
known (stickle.unlock).
"""

import unicodedata
from enum import Enum
from functools import cache
from pathlib import Path

MIN_LENGTH = 12
MIN_WORDS = 4
WORD_LENGTH = 2  # a word counts from two characters
LISTS = Path(__file__).resolve().parent / "common_passwords"

# The keyboard's rows and the alphabet, read either way: a password made of
# one of them alone is guessed at once.
RUNS = (
    "1234567890qwertyuiopasdfghjklzxcvbnm",
    "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p",
    "abcdefghijklmnopqrstuvwxyz",
    "01234567890",
)

# Korean as typed on an English keyboard (두벌식): initial consonants, vowels,
# and final consonants, in the order Unicode composes syllables from.
INITIALS = "r R s e E f a q Q t T d w W c z x v g".split()  # noqa: SIM905
VOWELS = "k o i O j p u P h hk ho hl y n nj np nl b m ml l".split()  # noqa: SIM905
FINALS = ["", *"r R rt s sw sg e f fr fa fq ft fx fv fg a q qt t T d w c z x v g".split()]  # noqa: SIM905
FIRST_SYLLABLE, LAST_SYLLABLE = 0xAC00, 0xD7A3


class Weakness(Enum):
    SHORT = "short"
    COMMON = "common"
    PATTERN = "pattern"


def typed_in_english(text: str) -> str:
    """Korean syllables as the keys that type them with the Korean input off."""
    keys: list[str] = []
    for char in text:
        code = ord(char)
        if FIRST_SYLLABLE <= code <= LAST_SYLLABLE:
            index = code - FIRST_SYLLABLE
            keys.append(INITIALS[index // 588] + VOWELS[index % 588 // 28] + FINALS[index % 28])
        else:
            keys.append(char)
    return "".join(keys)


def _comparable(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


@cache
def common_passwords() -> frozenset[str]:
    common = (LISTS / "common-passwords.txt").read_text(encoding="utf-8").split()
    korean = (LISTS / "korean.txt").read_text(encoding="utf-8").split()
    return frozenset(
        _comparable(word) for word in [*common, *korean, *(typed_in_english(k) for k in korean)]
    )


def _bare(text: str) -> str:
    """Without the digits and symbols commonly added at the end (password123!)."""
    return text.rstrip("0123456789!@#$%^&*.?-_~ ")


def _repeated(text: str) -> bool:
    """The same part over and over (aaaa, abcabc): its period is shorter than it."""
    return len(text) > 1 and (text + text).find(text, 1) < len(text)


def _run(text: str) -> bool:
    return any(text in run or text in run[::-1] for run in RUNS)


def weakness(password: str) -> Weakness | None:
    """Why the password would be easy to guess as a master password, or None
    if it is not."""
    text = unicodedata.normalize("NFC", password)
    words = [word for word in text.split() if len(word) >= WORD_LENGTH]
    if len(text) < MIN_LENGTH and len(words) < MIN_WORDS:
        return Weakness.SHORT
    return guessable(text)


def guessable(password: str) -> Weakness | None:
    """Why the password would be guessed first whatever its length (common, or a
    pattern), or None if it would not."""
    text = unicodedata.normalize("NFC", password)
    plain = _comparable("".join(text.split()))
    common = common_passwords()
    if plain in common or _bare(plain) in common or _comparable(text) in common:
        return Weakness.COMMON
    if _repeated(plain) or _run(plain):
        return Weakness.PATTERN
    return None
