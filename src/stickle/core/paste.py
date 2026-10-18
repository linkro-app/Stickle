"""Pasting formatted text: the HTML a browser or word processor copies, as Markdown.

Only what a note can show is kept: headings, bold, italic, highlight, inline
code and code blocks, lists and checklists, quotes and links (web and mail
addresses only). Anything else keeps its text and loses its formatting;
images keep their description, never their address. When the copied HTML
carries no formatting of these kinds (code editors copy coloured spans, for
example), the plain text copied with it is used as it is.

Notes never hold NUL, so it is removed from whatever is pasted.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

LINK_SCHEMES = ("http", "https", "mailto")
SKIPPED = {"script", "style", "head", "title", "meta", "link", "noscript", "template"}
VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "col", "area", "base", "source"}
BLOCKS = {
    "html", "body", "p", "div", "section", "article", "header", "footer", "main", "aside",
    "nav", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "pre", "blockquote", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "hr",
    "dl", "dt", "dd", "address",
}  # fmt: skip
# Tags that mean the HTML is formatted text, not just coloured plain text.
FORMATTING = {
    "b", "strong", "i", "em", "mark", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
    "a", "pre", "blockquote", "code", "input",
}  # fmt: skip
_SPACES = re.compile(r"[ \t\r\n\f\u00a0]+")  # \u00a0: the no-break space of &nbsp;
_ESCAPED = re.compile(r"([\\*_`\[\]])")
_LINE_START = re.compile(r"^(#|>|[-+] |\d+[.)] )", re.MULTILINE)
_CHECKBOX_SPACES = re.compile(r"^(\[[ x]\]) +")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict[str, str])
    children: list[_Node | str] = field(default_factory=list["_Node | str"])


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self._open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self._open[-1].children.append(node)
        if tag not in VOID:
            self._open.append(node)

    def handle_endtag(self, tag: str) -> None:
        # Close up to the matching tag; a stray end tag is ignored.
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def _style(node: _Node) -> dict[str, str]:
    declarations = (part.split(":", 1) for part in node.attrs.get("style", "").split(";"))
    return {d[0].strip().lower(): d[1].strip().lower() for d in declarations if len(d) == 2}


def _bold(node: _Node) -> bool:
    weight = _style(node).get("font-weight", "")
    if node.tag in ("b", "strong"):
        # Google Docs wraps everything in <b style="font-weight:normal">.
        return weight not in ("normal", "400")
    return weight == "bold" or (weight.isdigit() and int(weight) >= 600)


def _italic(node: _Node) -> bool:
    return node.tag in ("i", "em") or _style(node).get("font-style") == "italic"


def _formatted(node: _Node) -> bool:
    for child in node.children:
        if isinstance(child, str):
            continue
        if child.tag in SKIPPED:
            continue
        if child.tag in FORMATTING or _bold(child) or _italic(child) or _formatted(child):
            return True
    return False


def _escape(text: str) -> str:
    return _ESCAPED.sub(r"\\\1", text).replace("==", "=\\=")


def _link(href: str) -> str | None:
    href = href.strip()
    try:
        scheme = urlsplit(href).scheme.lower()
    except ValueError:
        return None
    return href if scheme in LINK_SCHEMES else None


def _wrap(text: str, mark: str) -> str:
    """Emphasis around text, with its edge spaces kept outside (Markdown needs that)."""
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{mark}{stripped}{mark}{trail}"


def _code_span(text: str) -> str:
    ticks = "``" if "`" in text else "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _raw_text(node: _Node) -> str:
    """The text as written, for code: no escaping, line breaks kept."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag not in SKIPPED:
            parts.append(_raw_text(child))
            if child.tag in ("div", "p", "li", "tr"):
                parts.append("\n")
    return "".join(parts)


def _inline(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(_escape(_SPACES.sub(" ", child)))
            continue
        tag = child.tag
        if tag in SKIPPED:
            continue
        if tag == "br":
            parts.append("\n")
        elif tag == "img":
            alt = child.attrs.get("alt", "").strip()
            parts.append(_escape(alt))
        elif tag == "input":
            if child.attrs.get("type", "").lower() == "checkbox":
                parts.append("[x] " if "checked" in child.attrs else "[ ] ")
        elif tag == "code":
            text = _SPACES.sub(" ", _raw_text(child))
            parts.append(_code_span(text) if text.strip() else text)
        elif tag == "a":
            text = _inline(child)
            href = _link(child.attrs.get("href", ""))
            if href is None or not text.strip():
                parts.append(text)
            elif text.strip() in (href, href.removeprefix("mailto:")) and "(" not in href:
                parts.append(href)
            else:
                lead = text[: len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()) :]
                target = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
                parts.append(f"{lead}[{text.strip()}]({target}){trail}")
        else:
            text = _inline(child)
            if tag == "mark":
                text = _wrap(text, "==")
            if _italic(child):
                text = _wrap(text, "*")
            if _bold(child):
                text = _wrap(text, "**")
            parts.append(text)
    return "".join(parts)


def _paragraph(text: str) -> str:
    lines = [line.strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    joined = "\n".join(line for line in lines)
    # A checkbox is followed by one space, however the page spaced it.
    joined = _CHECKBOX_SPACES.sub(r"\1 ", joined)
    return _LINE_START.sub(r"\\\1", joined)


def _list(node: _Node, ordered: bool) -> str:
    number = int(node.attrs["start"]) if node.attrs.get("start", "").isdigit() else 1
    items: list[str] = []
    for child in node.children:
        if isinstance(child, str) or child.tag != "li":
            continue
        marker = f"{number}. " if ordered else "- "
        number += 1
        body = "\n".join(_blocks(child)) or ""
        lines = body.split("\n")
        indent = " " * len(marker)
        items.append(
            marker + lines[0] + "".join(f"\n{indent}{line}" if line else "\n" for line in lines[1:])
        )
    return "\n".join(items)


def _rows(node: _Node) -> list[list[str]]:
    if node.tag == "tr":
        return [
            [
                " ".join(_paragraph(_inline(cell)).split("\n"))
                for cell in node.children
                if isinstance(cell, _Node) and cell.tag in ("td", "th")
            ]
        ]
    rows: list[list[str]] = []
    for child in node.children:
        if isinstance(child, _Node) and child.tag not in SKIPPED:
            rows.extend(_rows(child))
    return rows


def _blocks(node: _Node) -> list[str]:
    """The node's content as Markdown blocks."""
    blocks: list[str] = []
    run: list[_Node | str] = []

    def flush() -> None:
        if run:
            text = _paragraph(_inline(_Node("span", children=list(run))))
            if text:
                blocks.append(text)
            run.clear()

    for child in node.children:
        if isinstance(child, str) or child.tag not in BLOCKS:
            if not (isinstance(child, _Node) and child.tag in SKIPPED):
                run.append(child)
            continue
        flush()
        tag = child.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(_paragraph(_inline(child)).split("\n"))
            if text:
                blocks.append(f"{'#' * int(tag[1])} {text.lstrip(chr(92))}")
        elif tag in ("ul", "ol"):
            text = _list(child, ordered=tag == "ol")
            if text:
                blocks.append(text)
        elif tag == "pre":
            code = _raw_text(child).strip("\n")
            classes = (
                child.attrs.get("class", "")
                + " "
                + "".join(c.attrs.get("class", "") for c in child.children if isinstance(c, _Node))
            )
            language = next(
                (
                    word.removeprefix("language-")
                    for word in classes.split()
                    if word.startswith("language-")
                ),
                "",
            )
            fence = "````" if "```" in code else "```"
            blocks.append(f"{fence}{language}\n{code}\n{fence}")
        elif tag == "blockquote":
            inner = "\n\n".join(_blocks(child))
            if inner:
                blocks.append("\n".join(f"> {line}" if line else ">" for line in inner.split("\n")))
        elif tag == "hr":
            blocks.append("---")
        elif tag in ("table", "thead", "tbody", "tfoot", "tr"):
            # Tables are not shown as such: one line per row, cells apart.
            rows = [" | ".join(cells) for cells in _rows(child) if any(cells)]
            if rows:
                blocks.append("\n".join(rows))
        else:
            blocks.extend(_blocks(child))
    flush()
    return blocks


def html_to_markdown(html: str) -> str | None:
    """The HTML's text as Markdown, or None when it carries no formatting to keep."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    if not _formatted(builder.root):
        return None
    return "\n\n".join(_blocks(builder.root)).replace("\x00", "")


def pasted_text(html: str | None, plain: str) -> str:
    """What to put in the note for something copied as HTML and as plain text."""
    if html:
        converted = html_to_markdown(html)
        if converted is not None and converted.strip():
            return converted
    return plain.replace("\x00", "")
