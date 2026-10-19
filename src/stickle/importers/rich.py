"""Building Markdown from styled text, for apps that keep formatted notes.

Readers hand over text a piece at a time with the style it has (bold,
italic) and say where lines and list items start. Markdown's marks cannot
span lines, so each line closes what it opened and the next opens it again.
Characters that would mean something in Markdown are escaped, so a note
shows what it showed in its own app.
"""

import re
from dataclasses import dataclass, field

_ESCAPED = re.compile(r"([\\*_`\[\]])")
_LINE_START = re.compile(r"^(#|>|[-+] |\d+[.)] )")


def escape(text: str) -> str:
    return _ESCAPED.sub(r"\\\1", text).replace("==", "=\\=")


@dataclass
class _Run:
    text: str
    bold: bool
    italic: bool


@dataclass
class RichText:
    lines: list[list[_Run]] = field(default_factory=lambda: [[]])
    markers: list[str] = field(default_factory=lambda: [""])  # "- " or "1. " per line

    def add(self, text: str, bold: bool = False, italic: bool = False) -> None:
        for number, part in enumerate(text.split("\n")):
            if number:
                self.new_line()
            if part:
                self.lines[-1].append(_Run(part, bold, italic))

    def new_line(self) -> None:
        self.lines.append([])
        self.markers.append("")

    def list_item(self, marker: str = "- ") -> None:
        """The current line is a list item."""
        self.markers[-1] = marker

    def _line(self, runs: list[_Run], marker: str) -> str:
        parts: list[str] = []
        for run in _merged(runs):
            text = escape(run.text)
            mark = ("**" if run.bold else "") + ("*" if run.italic else "")
            core = text.strip()
            if mark and core:
                lead = text[: len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()) :]
                text = f"{lead}{mark}{core}{mark[::-1]}{trail}"
            parts.append(text)
        line = "".join(parts).rstrip()
        if not marker:
            return _LINE_START.sub(r"\\\1", line)
        return marker + line.lstrip()

    def markdown(self) -> str:
        lines = [
            self._line(runs, marker) for runs, marker in zip(self.lines, self.markers, strict=True)
        ]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines)


def _merged(runs: list[_Run]) -> list[_Run]:
    """Neighbouring runs of the same style as one, so marks are not split."""
    merged: list[_Run] = []
    for run in runs:
        if merged and (merged[-1].bold, merged[-1].italic) == (run.bold, run.italic):
            merged[-1] = _Run(merged[-1].text + run.text, run.bold, run.italic)
        else:
            merged.append(_Run(run.text, run.bold, run.italic))
    return merged
