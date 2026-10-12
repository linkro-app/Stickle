"""Reading and changing the categories and marks notes can have.

Which category and marks a note has is changed through NoteRepository, as
any other property of the note.
"""

import uuid
from collections.abc import Callable

import apsw

from stickle.core.clock import Clock, utc_now
from stickle.core.colors import PALETTE
from stickle.core.labels import (
    BUILT_IN_MARKS,
    DEFAULT_ICON,
    LABEL_ICONS,
    Category,
    Mark,
    clean_name,
)


class CategoryNameError(ValueError):
    """Empty, or the name of another category."""


class MarkNameError(ValueError):
    """Empty (for a new mark), or the name of another mark."""


def _own_name(mark: Mark) -> str:
    return mark.name or mark.id


def _category(row: apsw.SQLiteValues) -> Category:
    category_id, name, color, position, icon, deleted = row
    return Category(
        id=str(category_id),
        name=str(name),
        color=str(color),
        position=int(position),  # pyright: ignore[reportArgumentType]
        icon=str(icon),
        deleted_at=None if deleted is None else str(deleted),
    )


def _mark(row: apsw.SQLiteValues) -> Mark:
    mark_id, name, icon, position, deleted = row
    return Mark(
        id=str(mark_id),
        name=None if name is None else str(name),
        icon=str(icon),
        position=int(position),  # pyright: ignore[reportArgumentType]
        deleted_at=None if deleted is None else str(deleted),
    )


class LabelRepository:
    def __init__(self, connection: apsw.Connection, clock: Clock = utc_now) -> None:
        self._db = connection
        self._clock = clock

    def categories(self) -> list[Category]:
        """Categories not deleted, in their order."""
        rows = self._db.execute(
            "SELECT id, name, color, position, icon, deleted_at FROM categories"
            " WHERE deleted_at IS NULL ORDER BY position, created_at"
        )
        return [_category(row) for row in rows]

    def category(self, category_id: str) -> Category | None:
        """The category, deleted or not."""
        rows = self._db.execute(
            "SELECT id, name, color, position, icon, deleted_at FROM categories WHERE id = ?",
            (category_id,),
        ).fetchall()
        return _category(rows[0]) if rows else None

    def _free_name(self, name: str, renaming: str | None = None) -> str:
        """The name as kept, if no other category has it (case aside)."""
        name = clean_name(name)
        if not name:
            raise CategoryNameError("empty")
        folded = name.casefold()
        if any(
            category.name.casefold() == folded and category.id != renaming
            for category in self.categories()
        ):
            raise CategoryNameError("taken")
        return name

    def _require(self, category_id: str) -> Category:
        category = self.category(category_id)
        if category is None or category.deleted:
            raise KeyError(category_id)
        return category

    def create_category(self, name: str, color: str, icon: str = DEFAULT_ICON) -> Category:
        """A new category, last in the order."""
        name = self._free_name(name)
        if color not in PALETTE:
            raise ValueError(f"colour {color}")
        if icon not in LABEL_ICONS:
            raise ValueError(f"icon {icon}")
        category_id = str(uuid.uuid4())
        now = self._clock()
        with self._db:
            self._db.execute(
                "INSERT INTO categories (id, name, color, icon, position, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, (SELECT coalesce(max(position), 0) + 1 FROM categories),"
                " ?, ?)",
                (category_id, name, color, icon, now, now),
            )
        category = self.category(category_id)
        assert category is not None
        return category

    def rename_category(self, category_id: str, name: str) -> Category:
        self._require(category_id)
        name = self._free_name(name, renaming=category_id)
        with self._db:
            self._db.execute(
                "UPDATE categories SET name = ?, updated_at = ? WHERE id = ?",
                (name, self._clock(), category_id),
            )
        return self._require(category_id)

    def set_category_color(self, category_id: str, color: str) -> Category:
        self._require(category_id)
        if color not in PALETTE:
            raise ValueError(f"colour {color}")
        with self._db:
            self._db.execute(
                "UPDATE categories SET color = ?, updated_at = ? WHERE id = ?",
                (color, self._clock(), category_id),
            )
        return self._require(category_id)

    def set_category_icon(self, category_id: str, icon: str) -> Category:
        self._require(category_id)
        if icon not in LABEL_ICONS:
            raise ValueError(f"icon {icon}")
        with self._db:
            self._db.execute(
                "UPDATE categories SET icon = ?, updated_at = ? WHERE id = ?",
                (icon, self._clock(), category_id),
            )
        return self._require(category_id)

    def move_category(self, category_id: str, step: int) -> list[Category]:
        """Earlier (step -1) or later (1) in the order; the categories as they then are.
        At either end it stays where it is."""
        order = self.categories()
        index = next(i for i, c in enumerate(order) if c.id == self._require(category_id).id)
        target = index + step
        if not 0 <= target < len(order):
            return order
        order.insert(target, order.pop(index))
        now = self._clock()
        with self._db:
            # Numbered afresh: deleted categories keep theirs, which only orders them
            # among themselves if they come back.
            for position, category in enumerate(order, start=1):
                if category.position != position:
                    self._db.execute(
                        "UPDATE categories SET position = ?, updated_at = ? WHERE id = ?",
                        (position, now, category.id),
                    )
        return self.categories()

    def marks(self) -> list[Mark]:
        """Marks not deleted, in their order."""
        rows = self._db.execute(
            "SELECT id, name, icon, position, deleted_at FROM marks"
            " WHERE deleted_at IS NULL ORDER BY position, id"
        )
        return [_mark(row) for row in rows]

    def mark(self, mark_id: str) -> Mark | None:
        """The mark, deleted or not."""
        rows = self._db.execute(
            "SELECT id, name, icon, position, deleted_at FROM marks WHERE id = ?", (mark_id,)
        ).fetchall()
        return _mark(rows[0]) if rows else None

    def _require_mark(self, mark_id: str) -> Mark:
        mark = self.mark(mark_id)
        if mark is None or mark.deleted:
            raise KeyError(mark_id)
        return mark

    def _check_mark_name(
        self, name: str, shown: Callable[[Mark], str], renaming: str | None
    ) -> None:
        """No other mark has the name, as stored or as shown (a built-in mark's
        translated name), case aside."""
        folded = name.casefold()
        for other in self.marks():
            if other.id == renaming:
                continue
            if folded in (shown(other).casefold(), _own_name(other).casefold()):
                raise MarkNameError("taken")

    def create_mark(self, name: str, icon: str, shown: Callable[[Mark], str] = _own_name) -> Mark:
        """A new mark, last in the order. shown gives the name each mark shows."""
        name = clean_name(name)
        if not name:
            raise MarkNameError("empty")
        if icon not in LABEL_ICONS:
            raise ValueError(f"icon {icon}")
        self._check_mark_name(name, shown, None)
        mark_id = str(uuid.uuid4())
        with self._db:
            self._db.execute(
                "INSERT INTO marks (id, name, icon, position, updated_at)"
                " VALUES (?, ?, ?, (SELECT coalesce(max(position), 0) + 1 FROM marks), ?)",
                (mark_id, name, icon, self._clock()),
            )
        return self._require_mark(mark_id)

    def rename_mark(
        self, mark_id: str, name: str, shown: Callable[[Mark], str] = _own_name
    ) -> Mark:
        """A new name. An empty one gives a built-in mark back its own, translated
        name; a mark of the user's own needs a name."""
        mark = self._require_mark(mark_id)
        name = clean_name(name)
        if not name and mark.id not in BUILT_IN_MARKS:
            raise MarkNameError("empty")
        if name:
            self._check_mark_name(name, shown, mark_id)
        with self._db:
            self._db.execute(
                "UPDATE marks SET name = ?, updated_at = ? WHERE id = ?",
                (name or None, self._clock(), mark_id),
            )
        return self._require_mark(mark_id)

    def set_mark_icon(self, mark_id: str, icon: str) -> Mark:
        self._require_mark(mark_id)
        if icon not in LABEL_ICONS:
            raise ValueError(f"icon {icon}")
        with self._db:
            self._db.execute(
                "UPDATE marks SET icon = ?, updated_at = ? WHERE id = ?",
                (icon, self._clock(), mark_id),
            )
        return self._require_mark(mark_id)

    def move_mark(self, mark_id: str, step: int) -> list[Mark]:
        """Earlier (step -1) or later (1) in the order, as move_category."""
        order = self.marks()
        index = next(i for i, m in enumerate(order) if m.id == self._require_mark(mark_id).id)
        target = index + step
        if not 0 <= target < len(order):
            return order
        order.insert(target, order.pop(index))
        now = self._clock()
        with self._db:
            for position, mark in enumerate(order, start=1):
                if mark.position != position:
                    self._db.execute(
                        "UPDATE marks SET position = ?, updated_at = ? WHERE id = ?",
                        (position, now, mark.id),
                    )
        return self.marks()
