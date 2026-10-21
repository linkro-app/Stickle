"""Write packaging/licenses/qt-third-party.txt: the notices of the third-party code
inside the Qt modules Stickle ships, from Qt's own attribution pages.

    uv run python scripts/qt_notices.py

Run again when Qt moves to a new minor version, and commit the result. The
modules are those the builds include (Core, D-Bus, GUI, Image Formats, Network;
Svg and Widgets have no third-party code of their own).
"""

import html
import re
import urllib.request
from pathlib import Path

from PySide6 import __version__ as qt_version

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "licenses" / "qt-third-party.txt"
DOCS = "https://doc.qt.io/qt-6/"
MODULES = ("qtcore", "qtdbus", "qtgui", "qtimageformats", "qtnetwork")
# The page's text: from its title to the end of the article.
ARTICLE = re.compile(r"<h1[^>]*>.*?<!-- @@@", re.S)


def fetch(page: str) -> str:
    with urllib.request.urlopen(DOCS + page, timeout=60) as response:
        return response.read().decode("utf-8")


def pages(index: str) -> list[str]:
    """The attribution pages of the shipped modules, in the index's order."""
    modules = "|".join(MODULES)
    found = re.findall(rf'href="((?:{modules})-attribution-[^"#]+\.html)"', index)
    return list(dict.fromkeys(found))


def as_text(page: str) -> str:
    match = ARTICLE.search(page)
    if match is None:
        raise RuntimeError("no article on the page")
    text = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", match.group(0))
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text.replace("<!-- @@@", "")).strip()


def main() -> int:
    names = pages(fetch("licenses-used-in-qt.html"))
    if not names:
        raise SystemExit("no attribution pages found")
    parts = [
        f"Third-party code in the Qt {qt_version} modules shipped with Stickle\n"
        f"(from {DOCS}licenses-used-in-qt.html)"
    ]
    parts += [as_text(fetch(name)) for name in names]
    OUT.write_text(("\n\n" + "=" * 72 + "\n\n").join(parts) + "\n", encoding="utf-8")
    print(f"{len(names)} notices written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
