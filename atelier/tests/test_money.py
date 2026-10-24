"""Tests for app.services.money (integer Toman parsing / formatting, digit systems)."""
from decimal import Decimal

import pytest

from app.services.money import (
    MAX_TOMAN,
    format_toman,
    normalize_digits,
    parse_toman,
    to_persian_digits,
)

PERSIAN = "۰۱۲۳۴۵۶۷۸۹"
ARABIC = "٠١٢٣٤٥٦٧٨٩"
LATIN = "0123456789"


# --------------------------------------------------------------------------
# normalize_digits / to_persian_digits
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        (PERSIAN, LATIN),
        (ARABIC, LATIN),
        (LATIN, LATIN),
        ("۱٬۸۷۰٬۰۰۰", "1,870,000"),  # Arabic thousands separator
        ("۱،۸۷۰،۰۰۰", "1,870,000"),  # Arabic comma
        ("١٬٨٧٠٬٠٠٠", "1,870,000"),
        ("1,870,000", "1,870,000"),
        ("۳٫۵", "3.5"),  # Arabic decimal separator
        ("سال ۱۴۰۳ ماه ۵", "سال 1403 ماه 5"),  # letters untouched
        ("۱۴۰۳/۰۵/۱۲", "1403/05/12"),
        ("０１２", "012"),  # full-width digits
        ("", ""),
        (None, ""),
        (1870, "1870"),
    ],
)
def test_normalize_digits(text, expected):
    assert normalize_digits(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        (LATIN, PERSIAN),
        (ARABIC, PERSIAN),
        (PERSIAN, PERSIAN),
        ("a1b2", "a۱b۲"),
        ("1403/05/12", "۱۴۰۳/۰۵/۱۲"),
        ("1,870,000", "۱,۸۷۰,۰۰۰"),  # separators are not touched
        (1870, "۱۸۷۰"),
        ("", ""),
        (None, ""),
        ("تومان", "تومان"),
    ],
)
def test_to_persian_digits(text, expected):
    assert to_persian_digits(text) == expected


@pytest.mark.parametrize("n", [0, 7, 10, 999, 1000, 123456789, 1870000])
def test_persian_digit_round_trip(n):
    assert normalize_digits(to_persian_digits(n)) == str(n)


# --------------------------------------------------------------------------
# parse_toman
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("0", 0),
        ("7", 7),
        ("1870000", 1_870_000),
        ("1,870,000", 1_870_000),
        ("  1,870,000  ", 1_870_000),
        ("۱۸۷۰۰۰۰", 1_870_000),  # Persian digits
        ("۱٬۸۷۰٬۰۰۰", 1_870_000),  # Persian digits + Arabic thousands sep
        ("۱،۸۷۰،۰۰۰", 1_870_000),
        ("١٨٧٠٠٠٠", 1_870_000),  # Arabic-Indic digits
        ("١٬٨٧٠٬٠٠٠", 1_870_000),
        ("1 870 000", 1_870_000),  # spaces as separators
        ("1 870 000", 1_870_000),  # non-breaking spaces
        ("1,870,000 تومان", 1_870_000),  # round-trips format_toman(suffix=True)
        ("۱۲۰ تومان", 120),
        ("500 Toman", 500),
        ("007", 7),
        ("‏1,500‎", 1_500),  # bidi marks pasted with the number
        (1_870_000, 1_870_000),
        (0, 0),
        (Decimal("1500"), 1_500),  # PostgreSQL SUM() returns integral Decimal
        ("999,999,999,999,999", 10**15 - 1),
        (str(MAX_TOMAN), MAX_TOMAN),
    ],
)
def test_parse_toman_valid(value, expected):
    result = parse_toman(value)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("value", ["", "   ", None, "‏", "تومان", "‌"])
def test_parse_toman_blank_is_zero_by_default(value):
    assert parse_toman(value) == 0
    assert parse_toman(value, allow_blank=True) == 0


@pytest.mark.parametrize("value", ["", "   ", None, "‏", "تومان"])
def test_parse_toman_blank_rejected_when_required(value):
    with pytest.raises(ValueError):
        parse_toman(value, allow_blank=False)


def test_parse_toman_zero_is_not_blank():
    assert parse_toman("0", allow_blank=False) == 0
    assert parse_toman(0, allow_blank=False) == 0


@pytest.mark.parametrize(
    "value",
    [
        "1.5",
        "1.0",
        "0.5",
        "۳٫۵",
        "۱۲۳.۴۵",
        "1,870,000.00",
        "abc",
        "12abc",
        "abc12",
        "1e6",
        "1E3",
        "Infinity",
        "NaN",
        "0x10",
        "1,87,000",  # malformed grouping
        "1,8700",
        "1,0000",
        ",100",
        "100,",
        "1,,000",
        "12,34",
        "--5",
        "+5",
        "5-",
        "1 2",  # space groups must be 3 digits
        "1.870.000",  # European grouping is ambiguous: refuse
        "$100",
        "۱۲ ریال۳",
        1.5,
        5.0,  # even integral floats are refused: money never touches float
        float("nan"),
        float("inf"),
        True,
        False,
        [],
        {},
        (1,),
        Decimal("1.5"),
        Decimal("NaN"),
        MAX_TOMAN + 1,
        "1000000000000001",
        "9" * 30,
        "9" * 5000,
    ],
)
def test_parse_toman_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_toman(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("-5", -5),
        ("−5", -5),  # U+2212 minus sign
        ("-۱٬۵۰۰", -1_500),
        ("- 1,500", -1_500),
        ("-1,870,000 تومان", -1_870_000),
        ("-0", 0),
        (-5, -5),
        (Decimal("-7"), -7),
    ],
)
def test_parse_toman_negative_allowed(value, expected):
    assert parse_toman(value, allow_negative=True) == expected


@pytest.mark.parametrize("value", ["-5", "−5", "-۱٬۵۰۰", "-0", -5, Decimal("-7")])
def test_parse_toman_negative_rejected_by_default(value):
    with pytest.raises(ValueError):
        parse_toman(value)


@pytest.mark.parametrize("value", ["-", "- ", "−", "-تومان"])
def test_parse_toman_sign_without_digits(value):
    with pytest.raises(ValueError):
        parse_toman(value, allow_negative=True)


def test_parse_toman_negative_limit_is_symmetric():
    assert parse_toman(-MAX_TOMAN, allow_negative=True) == -MAX_TOMAN
    with pytest.raises(ValueError):
        parse_toman(-MAX_TOMAN - 1, allow_negative=True)


# --------------------------------------------------------------------------
# format_toman
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "n, persian, suffix, expected",
    [
        (0, False, False, "0"),
        (7, False, False, "7"),
        (999, False, False, "999"),
        (1000, False, False, "1,000"),
        (12345, False, False, "12,345"),
        (1_870_000, False, False, "1,870,000"),
        (1_000_000_000, False, False, "1,000,000,000"),
        (1_870_000, False, True, "1,870,000 تومان"),
        (1_870_000, True, False, "۱٬۸۷۰٬۰۰۰"),
        (1_870_000, True, True, "۱٬۸۷۰٬۰۰۰ تومان"),
        (0, True, True, "۰ تومان"),
        (-1500, False, False, "-1,500"),
        (-1500, True, True, "-۱٬۵۰۰ تومان"),
        (None, False, False, "0"),
        ("1870000", False, False, "1,870,000"),
        ("۱٬۸۷۰٬۰۰۰", False, False, "1,870,000"),
        (Decimal("2500000"), False, False, "2,500,000"),
    ],
)
def test_format_toman(n, persian, suffix, expected):
    assert format_toman(n, persian_digits=persian, suffix=suffix) == expected


def test_format_toman_defaults_are_latin_without_suffix():
    assert format_toman(1_870_000) == "1,870,000"


@pytest.mark.parametrize("bad", [1.5, 100.0, True, False, "1.5", "abc", [], Decimal("1.5")])
def test_format_toman_refuses_non_integers(bad):
    with pytest.raises(ValueError):
        format_toman(bad)


@pytest.mark.parametrize("n", [0, 1, 9, 10, 99, 100, 999, 1000, 54321, 1_870_000, 10**12, MAX_TOMAN])
@pytest.mark.parametrize("persian", [False, True])
@pytest.mark.parametrize("suffix", [False, True])
def test_format_then_parse_round_trips(n, persian, suffix):
    text = format_toman(n, persian_digits=persian, suffix=suffix)
    assert parse_toman(text) == n


@pytest.mark.parametrize("n", [-1, -999, -1_870_000])
def test_negative_round_trip(n):
    assert parse_toman(format_toman(n, persian_digits=True, suffix=True), allow_negative=True) == n
