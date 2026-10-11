"""Categories and marks: what a note is about, and how it is to be dealt with.

Both are independent of the note's colour. A note has at most one category
and any number of marks.
"""

from dataclasses import dataclass

# The marks every database starts with, in their order. Their names are
# translated where they are shown, until the user renames one.
BUILT_IN_MARKS = ("todo", "urgent", "important", "waiting")
# The icons a mark can have (each drawn by the app), the built-in marks' first.
MARK_ICONS = (
    *BUILT_IN_MARKS,
    "flag",
    "heart",
    "bulb",
    "person",
    "calendar",
    "home",
    "bookmark",
    "question",
)

# The icons a category can have (from Lucide, kept with the app), by name. A
# category made before icons, or with an icon this version lacks, shows the first.
CATEGORY_ICONS = (
    "tag",
    "folder",
    "briefcase",
    "building-2",
    "house",
    "users",
    "user",
    "baby",
    "heart",
    "heart-pulse",
    "stethoscope",
    "pill",
    "dumbbell",
    "utensils",
    "coffee",
    "shopping-cart",
    "wallet",
    "piggy-bank",
    "credit-card",
    "receipt",
    "book-open",
    "graduation-cap",
    "pencil",
    "file-text",
    "list-checks",
    "calendar",
    "clock",
    "alarm-clock",
    "mail",
    "message-circle",
    "phone",
    "smartphone",
    "laptop",
    "code",
    "terminal",
    "flask-conical",
    "wrench",
    "hammer",
    "plane",
    "car",
    "bus",
    "map-pin",
    "gift",
    "music",
    "camera",
    "film",
    "gamepad-2",
    "palette",
    "lightbulb",
    "sparkles",
    "star",
    "flag",
    "target",
    "trophy",
    "rocket",
    "bookmark",
    "archive",
    "lock",
    "key",
    "leaf",
    "sun",
    "cloud",
    "dog",
    "cat",
)
DEFAULT_CATEGORY_ICON = CATEGORY_ICONS[0]

NAME_LENGTH = 40


@dataclass(frozen=True)
class Category:
    id: str  # random UUID v4
    name: str
    color: str  # a palette key, for its icon
    position: int
    icon: str = DEFAULT_CATEGORY_ICON  # one of CATEGORY_ICONS, or one a later version added
    deleted_at: str | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True)
class Mark:
    id: str  # one of BUILT_IN_MARKS, or a random UUID v4
    name: str | None  # None: a built-in mark's own name, in the chosen language
    icon: str
    position: int
    deleted_at: str | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


def clean_name(name: str) -> str:
    """A category's or mark's name as kept: one line, no outer spaces, not too long."""
    return " ".join(name.split())[:NAME_LENGTH]
