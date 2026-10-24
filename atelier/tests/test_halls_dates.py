"""Bulk dates: parsing Jalali / ISO text, weekly and monthly rules, Esfand 30 and leap years."""

from __future__ import annotations

import datetime as dt

import pytest

from app.modules.halls import dates
from app.services import jalali


def j(jy: int, jm: int, jd: int) -> dt.date:
    return dt.date(*jalali.jalali_to_gregorian(jy, jm, jd))


# --------------------------------------------------------------------------
# one typed date
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["1404/04/26", "۱۴۰۴/۰۴/۲۶", "١٤٠٤/٠٤/٢٦", "1404-4-26", "۱۴۰۴.۴.۲۶", "14040426", " 1404 / 04 / 26 ", "2025-07-17"],
)
def test_every_spelling_of_the_same_day_parses(text):
    assert dates.parse_token(text) == dt.date(2025, 7, 17)


def test_esfand_30_exists_only_in_a_leap_year():
    assert dates.parse_token("1403/12/30") == dt.date(2025, 3, 20)  # 1403 is a leap year
    with pytest.raises(ValueError) as error:
        dates.parse_token("1404/12/30")
    assert "اسفند" in str(error.value) and "۲۹ روز" in str(error.value)
    assert dates.parse_token("1404/12/29") == dt.date(2026, 3, 20)


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("1404/13/01", "ماه"),
        ("1404/00/10", "ماه"),
        ("1404/07/31", "۳۰ روز"),  # Mehr has 30 days
        ("1404/01/32", "۳۱ روز"),
        ("1404/06/00", "۳۱ روز"),
        ("hello", "قالب"),
        ("1404/04", "قالب"),
        ("", "خالی"),
    ],
)
def test_invalid_dates_say_why(text, fragment):
    with pytest.raises(ValueError) as error:
        dates.parse_token(text)
    assert fragment in str(error.value)


def test_a_gregorian_impossible_day_is_reported_not_raised_as_a_crash():
    with pytest.raises(ValueError):
        dates.parse_token("2025-02-30")


def test_parse_many_splits_on_lines_commas_and_spaces_and_collects_errors():
    parsed, bad = dates.parse_many("1404/04/26، 1404/04/27\n1404/04/28 ; 2025-07-20\n1404/12/30  zzz 1404/04/26")
    assert parsed == [dt.date(2025, 7, 17), dt.date(2025, 7, 18), dt.date(2025, 7, 19), dt.date(2025, 7, 20)]
    assert [item.token for item in bad] == ["1404/12/30", "zzz"]
    assert "کبیسه" in bad[0].message
    assert dates.parse_many("") == ([], [])


# --------------------------------------------------------------------------
# weekly rule
# --------------------------------------------------------------------------


def test_weekly_thursdays_and_fridays_across_a_whole_summer():
    start, end = j(1404, 3, 1), j(1404, 5, 31)  # khordad .. mordad
    found = dates.weekly(start, end, dates.DEFAULT_WEEKDAYS)
    assert found and all(jalali.weekday_index(day) in (5, 6) for day in found)
    assert found == sorted(set(found))
    # every Thursday and Friday of the range is there exactly once
    total = sum(1 for n in range((end - start).days + 1) if jalali.weekday_index(start + dt.timedelta(days=n)) in (5, 6))
    assert len(found) == total


def test_weekly_across_esfand_into_nowruz_is_continuous():
    start, end = j(1403, 12, 20), j(1404, 1, 15)
    found = dates.weekly(start, end, [5])  # Thursdays
    assert found[0] <= j(1403, 12, 26) and found[-1] >= j(1404, 1, 8)
    gaps = {(b - a).days for a, b in zip(found, found[1:])}
    assert gaps == {7}  # no day lost or doubled at the Esfand -> Farvardin boundary
    assert j(1403, 12, 30) in dates.weekly(j(1403, 12, 30), j(1403, 12, 30), range(7))  # the leap-year day exists
    assert j(1404, 1, 1) in dates.weekly(j(1403, 12, 29), j(1404, 1, 2), range(7))


def test_weekly_needs_a_weekday_and_a_sane_range():
    with pytest.raises(ValueError):
        dates.weekly(dt.date(2025, 1, 1), dt.date(2025, 2, 1), [])
    with pytest.raises(ValueError):
        dates.weekly(dt.date(2025, 1, 1), dt.date(2025, 2, 1), [7])
    with pytest.raises(ValueError):
        dates.weekly(dt.date(2025, 2, 1), dt.date(2025, 1, 1), [0])
    with pytest.raises(ValueError):
        dates.weekly(dt.date(2025, 1, 1), dt.date(2030, 1, 1), [0])


# --------------------------------------------------------------------------
# monthly rule
# --------------------------------------------------------------------------


def test_monthly_first_and_fifteenth():
    found = dates.monthly(j(1404, 1, 1), j(1404, 12, 29), [1, 15])
    assert len(found) == 24
    assert [jalali.to_jalali(day)[2] for day in found] == [1, 15] * 12


def test_monthly_30th_clamps_in_a_common_esfand_but_not_in_a_leap_one():
    leap = dates.monthly(j(1403, 12, 1), j(1403, 12, 30), [30])
    assert leap == [j(1403, 12, 30)]
    common = dates.monthly(j(1404, 12, 1), j(1404, 12, 29), [30])
    assert common == [j(1404, 12, 29)]  # clamped to the last day of the month
    assert dates.monthly(j(1404, 12, 1), j(1404, 12, 29), [30], short_month="skip") == []


def test_monthly_31st_exists_only_in_the_first_six_months():
    clamped = dates.monthly(j(1404, 1, 1), j(1404, 12, 29), [31])
    days = [jalali.to_jalali(day)[1:] for day in clamped]
    assert len(clamped) == 12
    assert days[:6] == [(m, 31) for m in range(1, 7)]
    assert days[6:11] == [(m, 30) for m in range(7, 12)]
    assert days[11] == (12, 29)
    skipped = dates.monthly(j(1404, 1, 1), j(1404, 12, 29), [31], short_month="skip")
    assert len(skipped) == 6


def test_monthly_respects_partial_first_and_last_months():
    found = dates.monthly(j(1404, 2, 10), j(1404, 4, 10), [5, 10, 20])
    assert found[0] == j(1404, 2, 10) and found[-1] == j(1404, 4, 10)
    assert j(1404, 2, 5) not in found and j(1404, 4, 20) not in found


@pytest.mark.parametrize("days", [[], [0], [32], [-1]])
def test_monthly_rejects_bad_days(days):
    with pytest.raises(ValueError):
        dates.monthly(dt.date(2025, 1, 1), dt.date(2025, 6, 1), days)
    with pytest.raises(ValueError):
        dates.monthly(dt.date(2025, 1, 1), dt.date(2025, 6, 1), [1], short_month="x")


def test_merge_is_a_sorted_union():
    a = [dt.date(2025, 1, 3), dt.date(2025, 1, 1)]
    b = [dt.date(2025, 1, 2), dt.date(2025, 1, 1)]
    assert dates.merge(a, b) == [dt.date(2025, 1, 1), dt.date(2025, 1, 2), dt.date(2025, 1, 3)]
