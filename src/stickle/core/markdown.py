"""Reading note text: CommonMark plus task list checkboxes, ~~strikethrough~~,
==highlight== and addresses typed without brackets.

Notes are stored as Markdown only. This module parses them for display and
maps what is shown back to the text, without depending on Qt. Raw HTML is
not interpreted: it is shown as the characters that were typed.
"""

import re
from functools import cache

from linkify_it import LinkifyIt
from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline
from markdown_it.rules_inline.state_inline import Delimiter
from markdown_it.tree import SyntaxTreeNode

HIGHLIGHT_MARKER = "="
LINE_SEPARATOR = chr(0x2028)  # how a line break inside a paragraph is shown
# "[ ]", "[x]" or "[X]" at the start of a list item, then a space or nothing.
_TASK_START = re.compile(r"\[([ xX])\](?:[ \t]+|$)")
# The same box on the line a list item starts on, after any quote and list markers.
_TASK_LINE = re.compile(r"(?:[ \t]*>)*[ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+\[([ xX])\]")


@cache
def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    md.enable(["strikethrough", "linkify"])
    # Addresses typed as they are ("https://...", "mailto:...") show as links, but
    # not words that only look like domains: "main.py" is not a website.
    md.linkify = LinkifyIt(options={"fuzzy_link": False, "fuzzy_email": False})
    md.inline.ruler.before("emphasis", "highlight", _highlight_tokenize)
    md.inline.ruler2.before("emphasis", "highlight", _highlight_post_process)
    md.core.ruler.after("inline", "tasks", _tasks)
    return md


def parse(text: str) -> SyntaxTreeNode:
    """The syntax tree of a note. Every block node carries the lines it came from."""
    return SyntaxTreeNode(_parser().parse(text))


def _plain(node: SyntaxTreeNode) -> str:
    if node.type in ("text", "code_inline"):
        return node.content
    return "".join(_plain(child) for child in node.children)


def note_title(text: str) -> str:
    """A note's title: its first line with text, as shown, without Markdown marks.

    "# **Shopping**" gives "Shopping", "- [ ] milk" gives "milk". A reminder
    written in it is left out: "- [ ] milk ⏰2026-11-03" gives "milk". Lines that
    show nothing (a rule, an empty task, a reminder alone) are passed over.
    Empty if none.
    """
    # Here, not at the top: that module reads checkboxes with this one.
    from stickle.core.text_reminders import without_codes

    for line in text.splitlines():
        if line.strip() and (title := " ".join(without_codes(_plain(parse(line))).split())):
            return title
    return ""


# ==highlight==, built like markdown-it's ~~strikethrough~~.


def _highlight_tokenize(state: StateInline, silent: bool) -> bool:
    start = state.pos
    if silent or state.src[start] != HIGHLIGHT_MARKER:
        return False
    scanned = state.scanDelims(start, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        token = state.push("text", "", 0)
        token.content = HIGHLIGHT_MARKER
        length -= 1
    for _ in range(length // 2):
        token = state.push("text", "", 0)
        token.content = HIGHLIGHT_MARKER * 2
        state.delimiters.append(
            Delimiter(
                marker=ord(HIGHLIGHT_MARKER),
                length=0,
                token=len(state.tokens) - 1,
                end=-1,
                open=scanned.can_open,
                close=scanned.can_close,
            )
        )
    state.pos += scanned.length
    return True


def _highlight_pairs(state: StateInline, delimiters: list[Delimiter]) -> None:
    lone_markers: list[int] = []
    for start in delimiters:
        if start.marker != ord(HIGHLIGHT_MARKER) or start.end == -1:
            continue
        end = delimiters[start.end]
        opening, closing = state.tokens[start.token], state.tokens[end.token]
        opening.type, opening.nesting = "mark_open", 1
        closing.type, closing.nesting = "mark_close", -1
        for token in (opening, closing):
            token.tag = "mark"
            token.markup = HIGHLIGHT_MARKER * 2
            token.content = ""
        before = state.tokens[end.token - 1]
        if before.type == "text" and before.content == HIGHLIGHT_MARKER:
            lone_markers.append(end.token - 1)
    # An odd run such as "=====" was split "=" + "==" + "==": the single one
    # belongs after the closing tags.
    while lone_markers:
        i = lone_markers.pop()
        j = i + 1
        while j < len(state.tokens) and state.tokens[j].type == "mark_close":
            j += 1
        j -= 1
        if i != j:
            state.tokens[i], state.tokens[j] = state.tokens[j], state.tokens[i]


def _highlight_post_process(state: StateInline) -> None:
    _highlight_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _highlight_pairs(state, meta["delimiters"])


# Task list items: "- [ ] to do", "- [x] done".


def _tasks(state: StateCore) -> None:
    tokens = state.tokens
    for i in range(2, len(tokens)):
        inline, paragraph, item = tokens[i], tokens[i - 1], tokens[i - 2]
        if not (
            inline.type == "inline"
            and paragraph.type == "paragraph_open"
            and item.type == "list_item_open"
            and inline.children
            and inline.children[0].type == "text"
        ):
            continue
        first = inline.children[0]
        match = _TASK_START.match(first.content)
        if match is None:
            continue
        item.meta["checked"] = match.group(1) != " "
        first.content = first.content[match.end() :]


def task_box(line: str) -> tuple[int, str] | None:
    """Where a list item line's checkbox mark is, and what toggling it writes there.

    Only that one character changes: " " becomes "x", "x" or "X" becomes " ".
    """
    match = _TASK_LINE.match(line)
    if match is None:
        return None
    return match.start(1), "x" if match.group(1) == " " else " "


# Mapping a click on the formatted note back to the text.

_SNIPPET = 8


def source_position(source: str, first_line: int, last_line: int, before: str, after: str) -> int:
    """The place in the text that matches a point in a formatted block.

    The block was drawn from lines first_line to last_line (inclusive).
    before and after are the block's shown text on either side of the point.
    Markup makes the two differ, so the nearest match of the few characters
    around the point is used; failing that, the block's end or start.
    """
    lines = source.split("\n")
    last_line = min(last_line, len(lines) - 1)
    first_line = min(first_line, last_line)
    start = sum(len(line) + 1 for line in lines[:first_line])
    segment = "\n".join(lines[first_line : last_line + 1])
    before = before.replace(LINE_SEPARATOR, "\n")
    after = after.replace(LINE_SEPARATOR, "\n")
    shown = len(before) + len(after)
    estimate = round(len(segment) * len(before) / shown) if shown else 0

    def nearest(snippet: str, offset: int) -> int | None:
        """The point offset characters into the occurrence nearest the estimate."""
        found: list[int] = []
        index = segment.find(snippet)
        while index != -1:
            found.append(index + offset)
            index = segment.find(snippet, index + 1)
        return min(found, key=lambda point: abs(point - estimate)) if found else None

    for size in range(min(_SNIPPET, len(before)), 0, -1):
        point = nearest(before[-size:], size)
        if point is not None:
            return start + point
    for size in range(min(_SNIPPET, len(after)), 0, -1):
        point = nearest(after[:size], 0)
        if point is not None:
            return start + point
    return start + (len(segment) if before else 0)
