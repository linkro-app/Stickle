"""Formatted text pasted from browsers and word processors becomes Markdown."""

import pytest
from markdown_it import MarkdownIt

from stickle.core.paste import html_to_markdown, pasted_text


def md(html: str) -> str | None:
    return html_to_markdown(html)


def test_bold_italic_and_highlight() -> None:
    assert md("<p>a <b>bold</b>, <i>slanted</i> and <mark>marked</mark> word</p>") == (
        "a **bold**, *slanted* and ==marked== word"
    )


def test_headings_and_paragraphs() -> None:
    assert md("<h1>제목</h1><p>첫 문단</p><h3>작은 제목</h3><p>둘째</p>") == (
        "# 제목\n\n첫 문단\n\n### 작은 제목\n\n둘째"
    )


def test_line_breaks_stay() -> None:
    assert md("<p><b>주소</b><br>서울시<br>종로구</p>") == "**주소**\n서울시\n종로구"


def test_lists_nest_with_their_markers_widths() -> None:
    html = (
        "<ul><li>우유<ul><li>저지방</li></ul></li><li>빵</li></ul>"
        "<ol><li>하나<ol><li>가</li></ol></li><li>둘</li></ol>"
    )
    assert md(html) == "- 우유\n  - 저지방\n- 빵\n\n1. 하나\n   1. 가\n2. 둘"


def test_a_numbered_list_keeps_its_start() -> None:
    assert md('<ol start="4"><li><b>넷</b></li><li>다섯</li></ol>') == "4. **넷**\n5. 다섯"


def test_checklists_from_github() -> None:
    html = (
        '<ul class="contains-task-list">'
        '<li class="task-list-item"><input type="checkbox" checked disabled> 끝낸 일</li>'
        '<li class="task-list-item"><input type="checkbox" disabled> 남은 일</li></ul>'
    )
    assert md(html) == "- [x] 끝낸 일\n- [ ] 남은 일"


def test_links_only_to_the_web_and_mail() -> None:
    assert md('<p><a href="https://x.org/a b">문서</a></p>') == "[문서](https://x.org/a%20b)"
    assert md('<p><a href="https://x.org">https://x.org</a></p>') == "https://x.org"
    assert md('<p><a href="javascript:alert(1)">눌러</a> 보기</p>') == "눌러 보기"
    assert md('<p><b>x</b> <a href="file:///etc/passwd">파일</a></p>') == "**x** 파일"


def test_images_keep_only_their_description() -> None:
    assert md('<p><b>그림:</b> <img src="https://x.org/a.png" alt="고양이"></p>') == (
        "**그림:** 고양이"
    )


def test_code_blocks_keep_their_text_exactly() -> None:
    html = '<pre><code class="language-python">if a * b:\n    print("[x]")\n</code></pre>'
    assert md(html) == '```python\nif a * b:\n    print("[x]")\n```'
    assert md("<p>run <code>a*b</code> now</p>") == "run `a*b` now"


def test_quotes() -> None:
    assert md("<blockquote><p><i>인용</i></p><p>둘째 줄</p></blockquote>") == (
        "> *인용*\n>\n> 둘째 줄"
    )


def test_markdown_signs_in_the_text_stay_text() -> None:
    assert md("<p><b>x</b> 2*3 = 6, a_b, [1], a==b</p>") == (r"**x** 2\*3 = 6, a\_b, \[1\], a=\=b")
    assert md("<p><b>x</b></p><p># 1위</p><p>- 이건 목록이 아님</p>") == (
        "**x**\n\n\\# 1위\n\n\\- 이건 목록이 아님"
    )


def test_google_docs_wrapper_is_not_bold() -> None:
    html = (
        '<b style="font-weight:normal;" id="docs-internal-guid-1">'
        '<span style="font-weight:700">굵게</span><span style="font-style:italic">기울임</span>'
        "<span> 보통</span></b>"
    )
    assert md(html) == "**굵게***기울임* 보통"


def test_word_paragraphs() -> None:
    html = (
        "<html><head><style>p {margin:0}</style></head><body>"
        '<p class="MsoNormal"><b><span lang="KO">회의록</span></b></p>'
        '<p class="MsoNormal"><span lang="KO">안건&nbsp;하나</span></p></body></html>'
    )
    assert md(html) == "**회의록**\n\n안건 하나"


def test_tables_become_one_line_per_row() -> None:
    html = "<table><tr><th><b>이름</b></th><th>값</th></tr><tr><td>a</td><td>1</td></tr></table>"
    assert md(html) == "**이름** | 값\na | 1"


def test_coloured_plain_text_is_left_to_the_plain_copy() -> None:
    vscode = (
        '<div style="color:#d4d4d4"><div><span style="color:#569cd6">def</span> f(a*b):</div></div>'
    )
    assert md(vscode) is None
    assert pasted_text(vscode, "def f(a*b):") == "def f(a*b):"


def test_nul_never_reaches_a_note() -> None:
    assert pasted_text(None, "a\x00b") == "ab"
    assert pasted_text("<p><b>a\x00b</b></p>", "") == "**ab**"


def test_nothing_formatted_falls_back_to_plain() -> None:
    assert pasted_text("<p>그냥 글</p>", "그냥 글") == "그냥 글"
    assert pasted_text(None, "**이미 마크다운**") == "**이미 마크다운**"


ROUND_TRIP = [
    "# 장보기\n\n- [ ] 우유\n- [x] **빵**\n  - 통밀\n\n끝",
    "1. 하나\n2. *둘*\n   - 가\n\n> 인용 **굵게**\n\n```js\nlet a = b * c;\n```",
    "문단 [링크](https://x.org/a) 와 `코드`\n\n### 작은 제목\n\n---\n\n마지막",
]


@pytest.mark.parametrize("markdown", ROUND_TRIP)
def test_rendered_markdown_comes_back_the_same(markdown: str) -> None:
    """What a Markdown page renders reads back as the Markdown it came from."""
    parser = MarkdownIt("commonmark")
    html = parser.render(markdown)
    back = md(html)

    assert back is not None
    assert parser.render(back) == html
