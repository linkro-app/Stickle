"""Which master passwords are refused as easy to guess, and why."""

import pytest

from stickle.core.passwords import (
    LISTS,
    Weakness,
    common_passwords,
    typed_in_english,
    weakness,
)


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",
        "Tr0ub4dor&3xyz",
        "나는 오늘 아침에 사과를 먹었다",
        "우리 집 강아지 이름은 콩이",
        "x9K!mQ2#vL4$",
    ],
)
def test_strong_ones_are_taken(password: str) -> None:
    assert weakness(password) is None


@pytest.mark.parametrize(
    "password",
    ["short", "elevenchars", "a b c d e f", "two words", "사랑해 우리"],
)
def test_under_12_characters_and_4_words_is_too_short(password: str) -> None:
    assert weakness(password) == Weakness.SHORT


@pytest.mark.parametrize(
    "password",
    [
        "password1234",  # common, with digits at the end
        "Password1234!",
        "qwertyuiop12",
        "iloveyou1234",
        "dkssudgktpdy",  # 안녕하세요, typed with the Korean input off
        "tkfkdgo123456",  # 사랑해
        "DKSSUDGKTPDY1",  # with Caps Lock
        "qlalfqjsghdlqslek",  # 비밀번호입니다
    ],
)
def test_common_ones_are_refused(password: str) -> None:
    assert weakness(password) == Weakness.COMMON


@pytest.mark.parametrize(
    "password",
    [
        "aaaaaaaaaaaa",
        "abcabcabcabc",
        "123123123123",
        "abcdefghijklm",
        "mlkjihgfedcba",
        "1qaz2wsx3edc",
    ],
)
def test_repeats_and_runs_are_refused(password: str) -> None:
    assert weakness(password) == Weakness.PATTERN


def test_korean_is_turned_into_the_keys_that_type_it() -> None:
    assert typed_in_english("안녕") == "dkssud"
    assert typed_in_english("사랑해") == "tkfkdgo"
    assert typed_in_english("닭") == "ekfr"  # a final of two consonants
    assert typed_in_english("뭐 OK") == "anj OK"  # anything else as it is


def test_the_lists_are_in_the_package_with_their_license() -> None:
    assert (LISTS / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    common = common_passwords()
    assert len(common) > 10_000
    assert {"password", "123456", "사랑해", "tkfkdgo"} <= common
