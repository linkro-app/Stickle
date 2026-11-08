"""Notes the app makes by itself, known by their texts: the welcome note, and
each version's what's-new note (one id for it on every device), as every
version released so far wrote them, in each language and with each link they
came with. Untouched, such a note can be made again: where sync finds another
device's standing for it, it lets this one go.

Only digests of the texts are kept. A test checks that this version's texts
are among them: a text changed, or a translation of one, is added here, and
none is ever taken out (a device may have kept a note from any version).
"""

import hashlib
import uuid

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "stickle.linkro.co")

# SHA-256 of the text, hex.
WELCOME = frozenset(
    {
        "2752f8ddd6d17090e78a66652eb5e06f2d6593b756ff497f4c3f95381327ca19",
        "3d0342b82603b9e25597ef515be98afe2f4cf3d956e3370544a793399ac8d46c",
        "4c0841cd928e660ae01dec69a4c5e2e22a3542ac8bf4b6073e2e009d8104b46b",
        "54f6ba8b80bb7d2080318dbe5cb3d1d9700a34cffbb32a6d14b09c4ea6309604",
        "7fce778cf525b2bd7eae7dd561023f781180d99586a6a1d0ba51097da28cc4bd",
        "8eac3efacd736e026988532c5745d1ba43876dba868bd0ce56af5054b8c6b6b1",
        "94922a203b52d8027b5230a1dddf186b989affe2f03995a4da0fa99308334341",
        "b3dd8d57f084beb7b3b3cecdf031eea12d6d884b3463e85e94ce7bd63f2e18ea",
        "d11fd2942055647fd14bc2c5da9dcfe451f2ee2b491dcf786b8399898548848f",
        "d99b7bb8ee82eda9414af96a31c91581a0d3b36509e14c498580b79d6265155e",
    }
)
NEWS = {  # by release line
    "0.5": frozenset(
        {
            "126ef72aff02d0a8033723a6b313a1f16a02b63d4640a3b86f981b3e46311654",
            "396ff39cc3e1a9b66780cb003d85f974bbe568970cff2e9bd08eae6ef69b73e7",
            "54fd829c9806c614d98e9555dbed1f2f999da5a3791153e4cc3dfd955163c463",
            "5f06d8c881bb0af90cfa8fd7366f6bcfa2968d47cf6d1e03d74efe923c003807",
            "8d3aee897440f622d7ff5f31d6f3a03d1f83d2996db5c2d3e54f76589a89d4ee",
            "a62a89993aafb2dca6b063ae4aea7625663187f6c88407cfcba726e8e54a034e",
            "b942d9b6fb93e62f87d96e8c5cd1a0dab2a96d01a09256c6a283ecbfe5724d52",
            "da8098a12c69fa6dcc07c8fdb7d4ecfdf8a101502cb058e0d83bb95241b7f552",
        }
    ),
    "0.6": frozenset(
        {
            "0b9c076bca04f4a47eae7aca4f6cdb4a017111344ba3e3d7c9f46be869ee70a5",
            "72d2cd93472ec5536609f64f6c0887e435968c77a33a7526652b4dcd62d79bb2",
            "80610c5aa54da00b83dce193c714c77ca49a380def96d29c4f26aef104de4939",
            "ddde00e607f70cc92b388527ebd01566ab470496b63c6cfd87f07c52d35200b6",
        }
    ),
}


def news_id(line: str) -> str:
    """The what's-new note's id for a release line ("0.6"): the same everywhere."""
    return str(uuid.uuid5(_NAMESPACE, f"whats-new/{line}"))


_NEWS_LINES = {news_id(line): line for line in NEWS}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def welcome(text: str) -> bool:
    """Whether this is the welcome note's text as a version wrote it."""
    return digest(text) in WELCOME


def is_news(note_id: str) -> bool:
    """Whether this is a what's-new note's id."""
    return note_id in _NEWS_LINES


def news(note_id: str, text: str) -> bool:
    """Whether this is the what's-new note's text, as a version wrote it for
    the release line the id is of."""
    line = _NEWS_LINES.get(note_id)
    return line is not None and digest(text) in NEWS[line]
