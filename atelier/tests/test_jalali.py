"""Tests for app.services.jalali (calendar conversion, formatting, parsing, grids)."""
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import jalali
from app.services.jalali import (
    IRAN_TZ,
    JALALI_MONTHS,
    JALALI_WEEKDAYS,
    gregorian_to_jalali,
    is_jalali_leap,
    jalali_month_length,
    jalali_month_range,
    jalali_to_gregorian,
    month_grid,
    parse_jalali,
    to_date,
    to_iran_time,
    to_jalali,
    to_jalali_datetime_str,
    to_jalali_str,
    today,
    weekday_index,
    weekday_name,
)

UTC = timezone.utc

# Leap years of the 33-year arithmetic rule: an independent oracle for 1350..1450.
_RULE = {1, 5, 9, 13, 17, 22, 26, 30}


def rule_is_leap(jy):
    return jy % 33 in _RULE


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

def test_month_and_weekday_names():
    assert len(JALALI_MONTHS) == 12
    assert JALALI_MONTHS[0] == "فروردین"
    assert JALALI_MONTHS[4] == "مرداد"
    assert JALALI_MONTHS[11] == "اسفند"
    assert len(set(JALALI_MONTHS)) == 12
    assert len(JALALI_WEEKDAYS) == 7
    assert JALALI_WEEKDAYS[0] == "شنبه"  # Saturday first
    assert JALALI_WEEKDAYS[6] == "جمعه"
    assert len(set(JALALI_WEEKDAYS)) == 7


# --------------------------------------------------------------------------
# Nowruz anchors and well known dates
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "jy, greg",
    [
        (1350, (1971, 3, 21)),
        (1399, (2020, 3, 20)),
        (1400, (2021, 3, 21)),
        (1402, (2023, 3, 21)),
        (1403, (2024, 3, 20)),  # task anchors
        (1404, (2025, 3, 21)),
        (1405, (2026, 3, 21)),
        (1406, (2027, 3, 21)),
        (1408, (2029, 3, 20)),
    ],
)
def test_nowruz_anchors(jy, greg):
    assert jalali_to_gregorian(jy, 1, 1) == greg
    assert gregorian_to_jalali(*greg) == (jy, 1, 1)


@pytest.mark.parametrize(
    "greg, jal",
    [
        ((2024, 3, 20), (1403, 1, 1)),
        ((2025, 3, 21), (1404, 1, 1)),
        ((2026, 3, 21), (1405, 1, 1)),
        ((2024, 3, 19), (1402, 12, 29)),  # day before Nowruz 1403
        ((2025, 3, 20), (1403, 12, 30)),  # leap-day of 1403
        ((2025, 3, 19), (1403, 12, 29)),
        ((2026, 3, 20), (1404, 12, 29)),  # last day of non-leap 1404
        ((2021, 3, 20), (1399, 12, 30)),
        ((2024, 8, 2), (1403, 5, 12)),
        ((2024, 7, 22), (1403, 5, 1)),
        ((2024, 10, 21), (1403, 7, 30)),
        ((2025, 9, 23), (1404, 7, 1)),
        ((2025, 4, 20), (1404, 1, 31)),
        ((2025, 4, 21), (1404, 2, 1)),
        ((1979, 2, 11), (1357, 11, 22)),
        ((2000, 1, 1), (1378, 10, 11)),
        ((2024, 2, 29), (1402, 12, 10)),
        ((2072, 3, 19), (1450, 12, 29)),
    ],
)
def test_known_dates_both_directions(greg, jal):
    assert gregorian_to_jalali(*greg) == jal
    assert jalali_to_gregorian(*jal) == greg


# --------------------------------------------------------------------------
# Leap years and month lengths
# --------------------------------------------------------------------------

@pytest.mark.parametrize("jy", [1375, 1379, 1383, 1387, 1391, 1395, 1399, 1403, 1408, 1412])
def test_known_leap_years(jy):
    assert is_jalali_leap(jy) is True


@pytest.mark.parametrize("jy", [1400, 1401, 1402, 1404, 1405, 1406, 1407, 1409, 1410, 1411])
def test_known_common_years(jy):
    assert is_jalali_leap(jy) is False


@pytest.mark.parametrize("jy", range(1350, 1451))
def test_leap_years_match_33_year_rule(jy):
    assert is_jalali_leap(jy) == rule_is_leap(jy)


def test_esfand_1403_has_30_days_and_1404_has_29():
    assert jalali_month_length(1403, 12) == 30
    assert jalali_month_length(1404, 12) == 29
    assert jalali_to_gregorian(1403, 12, 30) == (2025, 3, 20)
    with pytest.raises(ValueError):
        jalali_to_gregorian(1404, 12, 30)


@pytest.mark.parametrize("jy", [1403, 1404])
@pytest.mark.parametrize(
    "jm, length",
    [(1, 31), (2, 31), (3, 31), (4, 31), (5, 31), (6, 31),
     (7, 30), (8, 30), (9, 30), (10, 30), (11, 30)],
)
def test_month_lengths(jy, jm, length):
    assert jalali_month_length(jy, jm) == length


@pytest.mark.parametrize("jy", range(1350, 1451))
def test_year_length_is_sum_of_month_lengths(jy):
    total = sum(jalali_month_length(jy, m) for m in range(1, 13))
    assert total == (366 if is_jalali_leap(jy) else 365)
    nowruz = date(*jalali_to_gregorian(jy, 1, 1))
    next_nowruz = date(*jalali_to_gregorian(jy + 1, 1, 1))
    assert (next_nowruz - nowruz).days == total


# --------------------------------------------------------------------------
# Round trips over 1350..1450
# --------------------------------------------------------------------------

_START = date(*jalali_to_gregorian(1350, 1, 1))
_END = date(*jalali_to_gregorian(1450, 12, 29))


def test_round_trip_every_seventh_day_1350_to_1450():
    day = _START
    checked = 0
    while day <= _END:
        jal = gregorian_to_jalali(day.year, day.month, day.day)
        assert 1350 <= jal[0] <= 1450
        assert jalali_to_gregorian(*jal) == (day.year, day.month, day.day)
        day += timedelta(days=7)
        checked += 1
    assert checked > 5000


@pytest.mark.parametrize("jy", range(1350, 1451))
def test_round_trip_all_month_boundaries(jy):
    for jm in range(1, 13):
        length = jalali_month_length(jy, jm)
        for jd in (1, 2, length - 1, length):
            gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
            assert gregorian_to_jalali(gy, gm, gd) == (jy, jm, jd)
    # the day before 1 Farvardin is the last day of the previous year
    gy, gm, gd = jalali_to_gregorian(jy, 1, 1)
    before = date(gy, gm, gd) - timedelta(days=1)
    last_month_len = jalali_month_length(jy - 1, 12)
    assert gregorian_to_jalali(before.year, before.month, before.day) == (jy - 1, 12, last_month_len)


def test_every_day_advances_the_jalali_date_by_exactly_one_day():
    day = _START
    previous = gregorian_to_jalali(day.year, day.month, day.day)
    while day < _END:
        day += timedelta(days=1)
        current = gregorian_to_jalali(day.year, day.month, day.day)
        jy, jm, jd = previous
        if jd < jalali_month_length(jy, jm):
            expected = (jy, jm, jd + 1)
        elif jm < 12:
            expected = (jy, jm + 1, 1)
        else:
            expected = (jy + 1, 1, 1)
        assert current == expected, day
        previous = current


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "args",
    [
        (1403, 0, 1), (1403, 13, 1), (1403, 1, 0), (1403, 1, 32), (1403, 7, 31),
        (1403, 12, 31), (1404, 12, 30), (0, 1, 1), (-5, 1, 1), (3001, 1, 1),
        (1403.0, 1, 1), (1403, 1.0, 1), (1403, 1, 1.0), (True, 1, 1), ("1403", 1, 1),
    ],
)
def test_jalali_to_gregorian_rejects_invalid(args):
    with pytest.raises(ValueError):
        jalali_to_gregorian(*args)


@pytest.mark.parametrize(
    "args",
    [(2024, 2, 30), (2023, 2, 29), (2024, 13, 1), (2024, 0, 1), (2024, 4, 31), (100, 1, 1), (621, 1, 1), (9999, 12, 31)],
)
def test_gregorian_to_jalali_rejects_invalid_or_unsupported(args):
    with pytest.raises(ValueError):
        gregorian_to_jalali(*args)


@pytest.mark.parametrize("args", [(1403, 0), (1403, 13), (1403, 1.5), (0, 1), (1403, True)])
def test_month_length_rejects_invalid(args):
    with pytest.raises(ValueError):
        jalali_month_length(*args)


# --------------------------------------------------------------------------
# Weekdays
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, index, name",
    [
        (date(2026, 3, 21), 0, "شنبه"),
        (date(2026, 3, 22), 1, "یکشنبه"),
        (date(2026, 3, 23), 2, "دوشنبه"),
        (date(2026, 3, 24), 3, "سه‌شنبه"),
        (date(2026, 3, 25), 4, "چهارشنبه"),
        (date(2026, 3, 26), 5, "پنجشنبه"),
        (date(2026, 3, 27), 6, "جمعه"),
        (date(2024, 8, 2), 6, "جمعه"),
        (date(2024, 3, 20), 4, "چهارشنبه"),  # Nowruz 1403 was a Wednesday
        (date(2025, 3, 21), 6, "جمعه"),  # Nowruz 1404 was a Friday
        (datetime(2024, 8, 2, 23, 59), 6, "جمعه"),
        ("2024-08-02", 6, "جمعه"),
        ("2026-03-21T10:00:00", 0, "شنبه"),
    ],
)
def test_weekday_index_and_name(value, index, name):
    assert weekday_index(value) == index
    assert weekday_name(value) == name


def test_weekday_index_cycles_over_a_full_week():
    seen = [weekday_index(date(2025, 1, 1) + timedelta(days=i)) for i in range(14)]
    assert seen[:7] == seen[7:]
    assert sorted(seen[:7]) == list(range(7))


# --------------------------------------------------------------------------
# to_jalali_str
# --------------------------------------------------------------------------

D = date(2024, 8, 2)  # 1403/05/12, a Friday


@pytest.mark.parametrize(
    "fmt, persian, expected",
    [
        ("numeric", False, "1403/05/12"),
        ("short", False, "12 مرداد"),
        ("long", False, "12 مرداد 1403"),
        ("weekday_long", False, "جمعه 12 مرداد 1403"),
        ("month_year", False, "مرداد 1403"),
        ("weekday", False, "جمعه"),
        ("numeric", True, "۱۴۰۳/۰۵/۱۲"),
        ("short", True, "۱۲ مرداد"),
        ("long", True, "۱۲ مرداد ۱۴۰۳"),
        ("weekday_long", True, "جمعه ۱۲ مرداد ۱۴۰۳"),
        ("month_year", True, "مرداد ۱۴۰۳"),
        ("weekday", True, "جمعه"),
    ],
)
def test_to_jalali_str_formats(fmt, persian, expected):
    assert to_jalali_str(D, fmt, persian_digits=persian) == expected


def test_to_jalali_str_default_is_numeric_latin():
    assert to_jalali_str(D) == "1403/05/12"


@pytest.mark.parametrize(
    "value",
    [
        date(2024, 8, 2),
        datetime(2024, 8, 2, 13, 45),
        datetime(2024, 8, 2, 13, 45, tzinfo=IRAN_TZ),
        "2024-08-02",
        "2024-08-02T13:45:00",
        "2024-08-02 13:45:00",
        "2024-08-02T10:15:00Z",
        "2024-08-02T13:45:00+03:30",
        "۲۰۲۴-۰۸-۰۲",
    ],
)
def test_to_jalali_str_accepts_date_datetime_and_iso_strings(value):
    assert to_jalali_str(value) == "1403/05/12"


@pytest.mark.parametrize(
    "value, expected",
    [
        (date(2025, 3, 21), "1404/01/01"),  # zero padded
        (date(2025, 3, 20), "1403/12/30"),
        (date(2024, 3, 20), "1403/01/01"),
        (date(2026, 3, 21), "1405/01/01"),
        (date(2025, 9, 23), "1404/07/01"),
    ],
)
def test_to_jalali_str_numeric_is_zero_padded(value, expected):
    assert to_jalali_str(value) == expected


def test_to_jalali_str_esfand_leap_day_long_form():
    assert to_jalali_str(date(2025, 3, 20), "weekday_long") == "پنجشنبه 30 اسفند 1403"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_to_jalali_str_blank_values_render_empty(blank):
    assert to_jalali_str(blank) == ""
    assert to_jalali_str(blank, "weekday_long", persian_digits=True) == ""
    assert to_jalali_datetime_str(blank) == ""


@pytest.mark.parametrize("bad", ["not a date", "2024-13-01", "2024-02-30", "1403/05/12"])
def test_to_jalali_str_rejects_bad_strings(bad):
    with pytest.raises(ValueError):
        to_jalali_str(bad)


@pytest.mark.parametrize("bad", [12345, 1.5, [], object()])
def test_to_jalali_str_rejects_bad_types(bad):
    with pytest.raises(TypeError):
        to_jalali_str(bad)


def test_to_jalali_str_rejects_unknown_format():
    with pytest.raises(ValueError):
        to_jalali_str(D, "fancy")
    with pytest.raises(ValueError):
        to_jalali_str(None, "fancy")


def test_timezone_rules_for_datetimes():
    late_utc = datetime(2025, 3, 20, 21, 0)  # 00:30 on 21 March in Iran
    # naive datetimes are used as-is unless told they are UTC
    assert to_jalali_str(late_utc) == "1403/12/30"
    assert to_jalali_str(late_utc, assume_utc=True) == "1404/01/01"
    # aware datetimes are always converted to Iran time
    assert to_jalali_str(late_utc.replace(tzinfo=UTC)) == "1404/01/01"
    assert to_jalali_str("2025-03-20T21:00:00Z") == "1404/01/01"
    assert to_jalali_str("2025-03-20T21:00:00") == "1403/12/30"
    # the 20:29 UTC / 20:30 UTC boundary (Iran midnight)
    assert to_jalali_str(datetime(2025, 3, 20, 20, 29, 59, tzinfo=UTC)) == "1403/12/30"
    assert to_jalali_str(datetime(2025, 3, 20, 20, 30, 0, tzinfo=UTC)) == "1404/01/01"


def test_to_date_and_to_jalali_helpers():
    assert to_date("2024-08-02") == date(2024, 8, 2)
    assert to_date(datetime(2024, 8, 2, 5, 0)) == date(2024, 8, 2)
    assert to_date(datetime(2024, 8, 2, 22, 0), assume_utc=True) == date(2024, 8, 3)
    assert to_date(date(2024, 8, 2)) == date(2024, 8, 2)
    assert to_jalali("2024-08-02") == (1403, 5, 12)
    with pytest.raises(TypeError):
        to_date(5)
    with pytest.raises(ValueError):
        to_date("nonsense")


# --------------------------------------------------------------------------
# to_jalali_datetime_str / to_iran_time
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, kwargs, expected",
    [
        (datetime(2025, 3, 20, 20, 30), {}, "1404/01/01 00:00"),  # naive = UTC
        (datetime(2025, 3, 20, 20, 29), {}, "1403/12/30 23:59"),
        (datetime(2025, 3, 20, 20, 30), {"assume_utc": False}, "1403/12/30 20:30"),
        (datetime(2024, 8, 2, 10, 15, tzinfo=UTC), {}, "1403/05/12 13:45"),
        (datetime(2024, 8, 2, 13, 45, tzinfo=IRAN_TZ), {}, "1403/05/12 13:45"),
        (datetime(2024, 8, 2, 10, 15, 42, tzinfo=UTC), {"seconds": True}, "1403/05/12 13:45:42"),
        (datetime(2024, 8, 2, 10, 15, tzinfo=UTC), {"persian_digits": True}, "۱۴۰۳/۰۵/۱۲ ۱۳:۴۵"),
        (datetime(2024, 8, 2, 10, 15, tzinfo=UTC), {"fmt": "long"}, "12 مرداد 1403 13:45"),
        ("2024-08-02T10:15:00Z", {}, "1403/05/12 13:45"),
        (date(2024, 8, 2), {}, "1403/05/12"),  # a plain date has no time part
    ],
)
def test_to_jalali_datetime_str(value, kwargs, expected):
    assert to_jalali_datetime_str(value, **kwargs) == expected


def test_to_jalali_datetime_str_rejects_bad_input():
    with pytest.raises(ValueError):
        to_jalali_datetime_str(datetime(2024, 1, 1), fmt="nope")
    with pytest.raises(TypeError):
        to_jalali_datetime_str(42)


def test_to_iran_time():
    out = to_iran_time(datetime(2024, 8, 2, 10, 15))
    assert (out.hour, out.minute) == (13, 45)
    assert out.utcoffset() == timedelta(hours=3, minutes=30)
    assert to_iran_time(datetime(2024, 8, 2, 10, 15, tzinfo=UTC)).hour == 13
    assert to_iran_time(datetime(2024, 8, 2, 10, 15), assume_utc=False).hour == 10
    assert IRAN_TZ.utcoffset(None) == timedelta(hours=3, minutes=30)


# --------------------------------------------------------------------------
# parse_jalali
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("1403/05/12", date(2024, 8, 2)),
        ("1403-05-12", date(2024, 8, 2)),
        ("1403.05.12", date(2024, 8, 2)),
        ("۱۴۰۳/۰۵/۱۲", date(2024, 8, 2)),  # Persian digits
        ("١٤٠٣/٠٥/١٢", date(2024, 8, 2)),  # Arabic-Indic digits
        ("۱۴۰۳-۵-۱۲", date(2024, 8, 2)),
        ("1403/5/12", date(2024, 8, 2)),
        ("1403/05/2", date(2024, 7, 23)),
        ("1403/5/2", date(2024, 7, 23)),
        ("  1403/05/12  ", date(2024, 8, 2)),
        ("1403 / 05 / 12", date(2024, 8, 2)),
        ("‏1403/05/12‎", date(2024, 8, 2)),  # bidi marks
        ("14030512", date(2024, 8, 2)),  # compact
        ("۱۴۰۳۰۵۱۲", date(2024, 8, 2)),
        ("1403‑05‑12", date(2024, 8, 2)),  # non-breaking hyphen
        ("1403/01/01", date(2024, 3, 20)),
        ("1404/01/01", date(2025, 3, 21)),
        ("1405/01/01", date(2026, 3, 21)),
        ("1403/12/30", date(2025, 3, 20)),  # leap day
        ("1399/12/30", date(2021, 3, 20)),
        ("1404/12/29", date(2026, 3, 20)),
        ("1357/11/22", date(1979, 2, 11)),
    ],
)
def test_parse_jalali_valid(text, expected):
    result = parse_jalali(text)
    assert result == expected
    assert type(result) is date


@pytest.mark.parametrize(
    "text",
    [
        "1404/12/30",  # 1404 is not a leap year
        "1402/12/30",
        "1403/13/01",
        "1403/00/10",
        "1403/01/32",
        "1403/01/00",
        "1403/07/31",  # Mehr has 30 days
        "1403/12/31",
        "0000/01/01",
        "3500/01/01",
        "",
        "   ",
        "abc",
        "1403",
        "1403/05",
        "1403/05/12/1",
        "1403/05-12",  # mixed separators
        "1403//05/12",
        "03/05/12",  # 2-digit year is ambiguous
        "12/05/1403",
        "1403/5/",
        "1403/123/1",
        "1403/05/123",
        "1403 05 12",
        "1403/05/12abc",
        "1403.05/12",
        "140305121",
        "1403/05/1.5",
    ],
)
def test_parse_jalali_invalid(text):
    with pytest.raises(ValueError):
        parse_jalali(text)


@pytest.mark.parametrize("bad", [None, 14030512, date(2024, 8, 2), 1403.0, b"1403/05/12"])
def test_parse_jalali_rejects_non_text(bad):
    with pytest.raises(ValueError):
        parse_jalali(bad)


@pytest.mark.parametrize("greg", [date(1979, 2, 11), date(2000, 1, 1), date(2024, 2, 29),
                                  date(2025, 3, 20), date(2025, 3, 21), date(2026, 3, 21)])
@pytest.mark.parametrize("persian", [False, True])
def test_format_then_parse_round_trips(greg, persian):
    assert parse_jalali(to_jalali_str(greg, "numeric", persian_digits=persian)) == greg


# --------------------------------------------------------------------------
# jalali_month_range / month_grid
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "jy, jm, first, last",
    [
        (1403, 5, date(2024, 7, 22), date(2024, 8, 21)),
        (1403, 12, date(2025, 2, 19), date(2025, 3, 20)),  # 30 days (leap)
        (1404, 12, date(2026, 2, 20), date(2026, 3, 20)),  # 29 days
        (1404, 1, date(2025, 3, 21), date(2025, 4, 20)),
        (1405, 1, date(2026, 3, 21), date(2026, 4, 20)),
    ],
)
def test_jalali_month_range(jy, jm, first, last):
    assert jalali_month_range(jy, jm) == (first, last)
    assert (last - first).days + 1 == jalali_month_length(jy, jm)


def test_month_grid_mordad_1403():
    grid = month_grid(1403, 5)
    assert len(grid) == 5
    assert all(len(week) == 7 for week in grid)
    # 1 Mordad 1403 is Monday 2024-07-22: Saturday and Sunday cells are padding.
    assert grid[0][:3] == [None, None, date(2024, 7, 22)]
    assert grid[0][6] == date(2024, 7, 26)  # Friday
    assert grid[1][0] == date(2024, 7, 27)  # next Saturday starts the second week
    assert grid[-1][-1] is None  # trailing padding


def test_month_grid_starting_on_saturday_has_no_leading_padding():
    grid = month_grid(1405, 1)  # 2026-03-21 is a Saturday
    assert grid[0][0] == date(2026, 3, 21)
    assert None not in grid[0]
    assert len(grid) == 5


def test_month_grid_can_need_six_weeks():
    grid = month_grid(1400, 5)  # starts on a Friday, 31 days
    assert len(grid) == 6
    assert grid[0][:6] == [None] * 6 and grid[0][6] is not None


@pytest.mark.parametrize("jy", [1400, 1403, 1404, 1405])
@pytest.mark.parametrize("jm", range(1, 13))
def test_month_grid_invariants(jy, jm):
    grid = month_grid(jy, jm)
    first, last = jalali_month_range(jy, jm)
    assert len(grid) in (5, 6)
    assert all(len(week) == 7 for week in grid)
    days = [d for week in grid for d in week if d is not None]
    assert days[0] == first and days[-1] == last
    assert len(days) == jalali_month_length(jy, jm)
    assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:]))
    # padding only at the edges, and columns are Saturday-first weekdays
    flat = [d for week in grid for d in week]
    assert flat.index(first) == weekday_index(first)
    for week in grid:
        for column, d in enumerate(week):
            if d is not None:
                assert weekday_index(d) == column
                assert gregorian_to_jalali(d.year, d.month, d.day)[:2] == (jy, jm)
    assert flat[flat.index(last) + 1:] == [None] * (len(flat) - flat.index(last) - 1)


# --------------------------------------------------------------------------
# today()
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "utc_now, expected",
    [
        (datetime(2025, 3, 20, 12, 0), date(2025, 3, 20)),
        (datetime(2025, 3, 20, 20, 29, 59), date(2025, 3, 20)),
        (datetime(2025, 3, 20, 20, 30, 0), date(2025, 3, 21)),  # Iran midnight
        (datetime(2025, 3, 20, 23, 59), date(2025, 3, 21)),
        (datetime(2025, 3, 21, 0, 0), date(2025, 3, 21)),
        (datetime(2025, 12, 31, 20, 30), date(2026, 1, 1)),
        (datetime(2025, 3, 20, 20, 30, tzinfo=UTC), date(2025, 3, 21)),
        (datetime(2025, 3, 21, 0, 30, tzinfo=IRAN_TZ), date(2025, 3, 21)),
        (datetime(2025, 3, 20, 23, 0, tzinfo=timezone(timedelta(hours=-5))), date(2025, 3, 21)),
    ],
)
def test_today_with_injected_clock(utc_now, expected):
    assert today(lambda: utc_now) == expected
    assert today(utc_now) == expected  # a datetime may be passed directly


def test_today_without_clock_is_close_to_real_time():
    real = datetime.now(UTC).date()
    assert abs((today() - real).days) <= 1
    assert type(today()) is date


def test_today_rejects_clock_without_datetime():
    with pytest.raises(TypeError):
        today(lambda: "2025-03-21")
    with pytest.raises(TypeError):
        today("2025-03-21")


def test_jalali_date_for_injected_today():
    assert to_jalali_str(today(lambda: datetime(2025, 3, 20, 20, 30)), "long") == "1 فروردین 1404"


# --------------------------------------------------------------------------
# Purity: the service layer must not depend on the web stack
# --------------------------------------------------------------------------

SERVICES = Path(jalali.__file__).parent
FORBIDDEN = {"flask", "sqlalchemy", "flask_sqlalchemy", "flask_login", "flask_wtf", "werkzeug", "jinja2", "wtforms"}


@pytest.mark.parametrize("module", ["jalali", "money", "payroll", "print_yield"])
def test_service_modules_do_not_import_the_web_stack(module):
    tree = ast.parse((SERVICES / f"{module}.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & FORBIDDEN, imported & FORBIDDEN
