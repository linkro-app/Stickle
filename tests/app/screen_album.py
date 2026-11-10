"""Pictures of windows kept in a folder, and what is different from the last time.

<folder>/now/      this time's pictures
<folder>/last/     the time before
<folder>/changed/  those that differ: the old one on the left, the new on the right
<folder>/report.txt
"""

import shutil
from pathlib import Path

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

GAP = 12
_GAP_COLOUR = "#ff00ff"


def _same(one: QImage, other: QImage) -> bool:
    format_ = QImage.Format.Format_ARGB32
    return one.convertToFormat(format_) == other.convertToFormat(format_)


def _side_by_side(before: QImage, after: QImage) -> QImage:
    joined = QImage(
        before.width() + GAP + after.width(),
        max(before.height(), after.height()),
        QImage.Format.Format_ARGB32,
    )
    joined.fill(QColor(_GAP_COLOUR))
    painter = QPainter(joined)
    painter.drawImage(0, 0, before)
    painter.drawImage(before.width() + GAP, 0, after)
    painter.end()
    return joined


class Album:
    def __init__(self, folder: Path) -> None:
        self.now = folder / "now"
        self.last = folder / "last"
        self.changed = folder / "changed"
        self.report = folder / "report.txt"

    def begin(self) -> None:
        """This time's pictures start empty, the ones there were kept to compare."""
        if self.now.exists():
            shutil.rmtree(self.last, ignore_errors=True)
            self.now.rename(self.last)
        shutil.rmtree(self.changed, ignore_errors=True)
        self.now.mkdir(parents=True)

    def take(self, name: str, window: QWidget) -> None:
        saved = window.grab().save(str(self.now / f"{name}.png"))
        assert saved, name

    def end(self) -> str:
        """Compares this time's pictures with the last; what differs, in words."""
        now = {path.stem for path in self.now.glob("*.png")}
        if not self.last.exists():
            said = f"First album: {len(now)} pictures, nothing to compare them with."
            self.report.write_text(said + "\n", "utf-8")
            return said
        last = {path.stem for path in self.last.glob("*.png")}
        changed: list[str] = []
        for name in sorted(now & last):
            before, after = (QImage(str(side / f"{name}.png")) for side in (self.last, self.now))
            if not _same(before, after):
                changed.append(name)
                self.changed.mkdir(exist_ok=True)
                saved = _side_by_side(before, after).save(str(self.changed / f"{name}.png"))
                assert saved, name
        lines = [
            f"{len(now & last) - len(changed)} the same, {len(changed)} changed,"
            f" {len(now - last)} new, {len(last - now)} gone.",
            *(f"changed: {name}" for name in changed),
            *(f"new: {name}" for name in sorted(now - last)),
            *(f"gone: {name}" for name in sorted(last - now)),
        ]
        said = "\n".join(lines)
        self.report.write_text(said + "\n", "utf-8")
        return said
