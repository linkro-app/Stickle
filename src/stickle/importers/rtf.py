"""Reading the RTF that Windows' rich edit control writes, as Markdown.

Only what notes use: paragraphs and line breaks, bold and italic, bulleted
and numbered paragraphs, and characters in any code page (\\'hh in the
document's or the font's code page, \\uN for others). A link keeps the text
it shows (usually its address, which a note turns into a link again).
Tables of fonts, colours and styles, document information and pictures are
skipped; underline, strikethrough and pictures are named as left behind.
"""

import re
from dataclasses import dataclass, field, replace

from stickle.importers.base import ImportResult
from stickle.importers.rich import RichText

_TOKEN = re.compile(
    r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+"
)
# Destinations whose text is not part of the note.
SKIPPED = {
    "fonttbl", "colortbl", "stylesheet", "info", "generator", "listtable",
    "listoverridetable", "rsidtbl", "xmlnstbl", "mmathPr", "themedata",
    "colorschememapping", "latentstyles", "datastore", "fldinst", "pnseclvl",
}  # fmt: skip
# Control words that stand for a character (by code: they look like plain ones).
SYMBOLS = {
    "bullet": chr(0x2022),
    "emdash": chr(0x2014),
    "endash": chr(0x2013),
    "lquote": chr(0x2018),
    "rquote": chr(0x2019),
    "ldblquote": chr(0x201C),
    "rdblquote": chr(0x201D),
}
# Windows character sets (\fcharsetN) and the code pages they mean.
CHARSETS = {
    0: "cp1252", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab", 134: "gbk",
    136: "big5", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255",
    178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250",
}  # fmt: skip


@dataclass
class _State:
    bold: bool = False
    italic: bool = False
    skip: bool = False
    font: int = 0
    skip_unicode: int = 1  # characters after \uN that stand in for it
    bullet: bool = False  # inside {\pntext …}: the list marker's text


@dataclass
class _Reader:
    result: ImportResult
    rich: RichText = field(default_factory=RichText)
    codepage: str = "cp1252"
    fonts: dict[int, str] = field(default_factory=dict[int, str])
    pending: bytearray = field(default_factory=bytearray)  # \'hh bytes not yet decoded
    marker: str = ""

    def flush(self, state: _State) -> None:
        if self.pending:
            encoding = self.fonts.get(state.font, self.codepage)
            self.text(bytes(self.pending).decode(encoding, errors="replace"), state)
            self.pending.clear()

    def text(self, text: str, state: _State) -> None:
        if state.skip:
            return
        if state.bullet:
            self.marker += text
        else:
            self.rich.add(text, state.bold, state.italic)


def _font_table(rtf: str) -> dict[int, str]:
    """Each font's code page, from \\fN … \\fcharsetM in the font table."""
    fonts: dict[int, str] = {}
    for match in re.finditer(r"\\f(\d+)[^;{}]*?\\fcharset(\d+)", rtf):
        encoding = CHARSETS.get(int(match.group(2)))
        if encoding:
            fonts[int(match.group(1))] = encoding
    return fonts


def rtf_to_markdown(rtf: str, result: ImportResult) -> str:
    reader = _Reader(result)
    if match := re.search(r"\\ansicpg(\d+)", rtf):
        reader.codepage = f"cp{match.group(1)}"
    reader.fonts = _font_table(rtf)
    stack: list[_State] = []
    state = _State()
    skip_chars = 0  # fallback characters to drop after \uN
    for match in _TOKEN.finditer(rtf):
        word, value, hex_byte, symbol, brace, text = match.groups()
        if hex_byte is not None:
            if skip_chars:
                skip_chars -= 1
            else:
                reader.pending.append(int(hex_byte, 16))
            continue
        reader.flush(state)
        if brace == "{":
            stack.append(state)
            state = replace(state)
        elif brace == "}":
            if state.bullet and not (stack and stack[-1].bullet):
                marker = reader.marker.strip()
                reader.rich.list_item(f"{marker} " if re.fullmatch(r"\d+[.)]", marker) else "- ")
                reader.marker = ""
            state = stack.pop() if stack else _State()
        elif symbol is not None:
            if symbol == "*":
                state.skip = True
            elif symbol in "\\{}":
                reader.text(symbol, state)
            elif symbol == "~":
                reader.text("\u00a0", state)
            elif symbol in "-_":
                reader.text("-" if symbol == "_" else "", state)
        elif word is not None:
            skip_chars = _control(word, value, state, reader, skip_chars)
        elif text is not None:
            if skip_chars:
                dropped, text = text[:skip_chars], text[skip_chars:]
                skip_chars -= len(dropped)
            reader.text(text, state)
    reader.flush(state)
    return reader.rich.markdown()


def _control(word: str, value: str | None, state: _State, reader: _Reader, skip_chars: int) -> int:
    on = value != "0"
    if word in SKIPPED:
        state.skip = True
    elif word == "pict":
        state.skip = True
        reader.result.drop("pictures")
    elif word == "pntext":
        state.bullet = True
    elif word in ("par", "line"):
        if not state.skip and not state.bullet:
            reader.rich.new_line()
    elif word == "tab":
        reader.text("\t", state)
    elif word == "b":
        state.bold = on
    elif word == "i":
        state.italic = on
    elif word in ("ul", "uld", "uldb", "ulw") and on:
        reader.result.drop("underline")
    elif word in ("strike", "striked") and on:
        reader.result.drop("strikethrough")
    elif word == "plain":
        state.bold = state.italic = False
    elif word == "f" and value is not None:
        state.font = int(value)
    elif word == "uc" and value is not None:
        state.skip_unicode = int(value)
    elif word == "u" and value is not None:
        code = int(value) % 65536
        reader.text(chr(code), state)
        return state.skip_unicode
    elif word in SYMBOLS:
        reader.text(SYMBOLS[word], state)
    return skip_chars
