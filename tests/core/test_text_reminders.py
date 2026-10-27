"""Reminders written in a note's text: read strictly, one a line, and written."""

from datetime import date, datetime, time

from hypothesis import given
from hypothesis import strategies as st

from stickle.core.markdown import note_title
from stickle.core.text_reminders import MARK, code, codes, due, with_code, written

NINE = time(9, 0)


def test_a_day_and_a_time_or_a_day_only() -> None:
    [timed] = codes(f"- [ ] 보고서 제출 {MARK}2026-10-20 15:00")
    assert (timed.day, timed.at, timed.note) == (date(2026, 10, 20), time(15, 0), None)
    [day_only] = codes(f"장보기 {MARK}2026-10-21")
    assert day_only.at is None
    assert day_only.when(NINE) == datetime(2026, 10, 21, 9, 0)


def test_only_what_is_written_exactly_is_a_reminder() -> None:
    for text in (
        f"{MARK}2026-02-30 10:00",  # no such day
        f"{MARK}2026-10-20 25:00",  # no such time
        f"{MARK}2026-10-20 9:00",  # the hour in two digits
        f"{MARK}26-10-20",
        f"{MARK} 2026-10-20",
        "2026-10-20 15:00",  # no mark
        f"{MARK}2026-10-20 15:001",
    ):
        assert codes(text) == [], text
    # After a reminder, other words and numbers are just text.
    [timed] = codes(f"{MARK}2026-10-20 15:00 2층 회의실")
    assert timed.at == time(15, 0)


def test_a_day_picked_in_another_calendar_comes_first_in_brackets() -> None:
    [lunar] = codes(f"제사 {MARK}[음력 8월 15일] 2026-09-25 15:00")
    assert (lunar.note, lunar.day, lunar.at) == ("음력 8월 15일", date(2026, 9, 25), time(15, 0))


def test_one_a_line_and_none_in_a_code_block() -> None:
    text = "\n".join(
        [
            f"a {MARK}2026-10-20 15:00 b {MARK}2026-10-21 10:00",
            "```",
            f"{MARK}2026-10-22 10:00",
            "```",
            f"c {MARK}2026-10-23 08:30",
        ]
    )
    assert [(w.line, w.code.day) for w in written(text)] == [
        (0, date(2026, 10, 20)),
        (4, date(2026, 10, 23)),
    ]


def test_a_checked_line_does_not_ring_and_the_same_time_rings_once() -> None:
    text = "\n".join(
        [
            f"- [ ] 보고서 {MARK}2026-10-20 15:00",
            f"- [x] 끝낸 일 {MARK}2026-10-20 16:00",
            f"- [ ] 같은 때 {MARK}2026-10-20 15:00",
            f"점심 {MARK}2026-10-20",
        ]
    )
    assert due(text, NINE) == {
        datetime(2026, 10, 20, 15, 0): [0, 2],
        datetime(2026, 10, 20, 9, 0): [3],
    }


def test_written_at_the_end_of_a_line_or_in_place_of_its_reminder() -> None:
    when = datetime(2026, 11, 3, 9, 30)
    assert with_code("- [ ] 보고서", when) == f"- [ ] 보고서 {MARK}2026-11-03 09:30"
    assert with_code("", when) == f"{MARK}2026-11-03 09:30"
    old = f"- [ ] 보고서 {MARK}2026-10-20 15:00 오후"
    assert with_code(old, when) == f"- [ ] 보고서 {MARK}2026-11-03 09:30 오후"


@given(
    # Any line but a code block's fence, where nothing is a reminder.
    st.text(st.characters(exclude_characters="\n`~" + MARK), max_size=30),
    st.datetimes(datetime(1000, 1, 1), datetime(9999, 12, 31)).map(
        lambda moment: moment.replace(second=0, microsecond=0)
    ),
)
def test_what_is_written_is_read_back(line: str, when: datetime) -> None:
    written_line = with_code(line, when)
    assert code(when) in written_line
    found = [w for w in written(written_line)]
    assert len(found) == 1
    assert found[0].code.when(NINE) == when


def test_a_title_leaves_its_reminder_out() -> None:
    assert note_title(f"- [ ] 보고서 {MARK}2026-11-03 15:00") == "보고서"
    assert note_title(f"{MARK}2026-11-03\n장보기 목록") == "장보기 목록"
    assert note_title(f"{MARK}2026-11-03 25:00 틀린 코드") == f"{MARK}2026-11-03 25:00 틀린 코드"
