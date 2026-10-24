"""Date logic for adding many events at once (a whole season): parsing, rules, Esfand 30.

Pure Python on top of :mod:`app.services.jalali` (no Flask, no database), so every rule can be
tested without a request.  Dates are Gregorian :class:`datetime.date` objects in and out; the
Jalali calendar only appears in what a person types and in the messages we show.

Rules worth knowing
-------------------
* A typed date may be Jalali (``1404/04/26``, ``۱۴۰۴-۴-۲۶``, ``14040426``) or ISO Gregorian
  (``2025-07-17``).  Persian, Arabic and Latin digits are all accepted.
* **Esfand** has 29 days, and 30 only in a Jalali leap year (1403, 1408, ...).  ``1404/12/30``
  is therefore *invalid* and is reported with that explanation instead of being silently moved.
* "The same day of every month" (:func:`monthly`) meets short months: a 31st does not exist in
  Mehr..Esfand and a 30th not in a common Esfand.  ``short_month="clamp"`` uses the month's last
  day, ``"skip"`` leaves that month out.
* "Every Thursday and Friday" (:func:`weekly`) walks real days, so it is correct across
  Esfand -> Farvardin (Nowruz) and across leap years.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Iterable

from app.services import jalali
from app.services.forms import clean_text
from app.services.money import normalize_digits, to_persian_digits

__all__ = [
    "MAX_BULK_DATES",
    "MAX_SPAN_DAYS",
    "MAX_AHEAD_DAYS",
    "WEEKDAY_CHOICES",
    "DEFAULT_WEEKDAYS",
    "BadDate",
    "parse_token",
    "parse_many",
    "weekly",
    "monthly",
    "merge",
    "explain_invalid",
]

#: Most dates one bulk request may create (a whole year of every day).
MAX_BULK_DATES = 366
#: Longest range a rule (weekly / monthly) may span.
MAX_SPAN_DAYS = 800
#: How far ahead an event may be planned (typos such as year 1414 are caught).
MAX_AHEAD_DAYS = 1100

#: ``(index, label)`` with Saturday = 0 ... Friday = 6, the order of an Iranian week.
WEEKDAY_CHOICES: tuple[tuple[int, str], ...] = tuple(enumerate(jalali.JALALI_WEEKDAYS))
#: Thursday and Friday: when most weddings are held.
DEFAULT_WEEKDAYS: tuple[int, ...] = (5, 6)

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_PARTS_RE = re.compile(r"^(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})$")
_SPLIT_RE = re.compile(r"[\s,;،؛]+")
_AROUND_SEPARATOR_RE = re.compile(r"\s*([/.\-])\s*")


@dataclass(frozen=True)
class BadDate:
    """One token that is not a date, with the reason (Persian, ready to show)."""

    token: str
    message: str


def _fa(value: object) -> str:
    return to_persian_digits(str(value))


def _clean(token: object) -> str:
    return normalize_digits(clean_text(token))


def explain_invalid(token: str) -> str:
    """Why *token* is not a date: wrong month, a day the month does not have (Esfand 30), or a bad shape."""
    text = _clean(token)
    match = _PARTS_RE.match(text)
    if not match:
        return "قالب تاریخ درست نیست؛ مثلاً ۱۴۰۴/۰۴/۲۶."
    jy, jm, jd = (int(part) for part in match.groups())
    if not 1 <= jm <= 12:
        return "ماه باید عددی از ۱ تا ۱۲ باشد."
    try:
        length = jalali.jalali_month_length(jy, jm)
    except ValueError:
        return f"سال {_fa(jy)} پشتیبانی نمی‌شود."
    if jd < 1 or jd > length:
        month = jalali.JALALI_MONTHS[jm - 1]
        if jm == 12 and jd == 30 and not jalali.is_jalali_leap(jy):
            return f"اسفند {_fa(jy)} ۲۹ روز دارد؛ سال {_fa(jy)} کبیسه نیست و ۳۰ اسفند ندارد."
        return f"{month} {_fa(jy)} فقط {_fa(length)} روز دارد."
    return "تاریخ نامعتبر است."


def parse_token(token: str) -> dt.date:
    """One typed date -> :class:`datetime.date`; raises ``ValueError`` with a Persian message."""
    text = _clean(token)
    if not text:
        raise ValueError("تاریخ خالی است.")
    iso = _ISO_RE.match(text)
    try:
        if iso:
            year, month, day = (int(part) for part in iso.groups())
            if year > 1700:  # Gregorian
                return dt.date(year, month, day)
            return dt.date(*jalali.jalali_to_gregorian(year, month, day))  # Jalali with dashes
        return jalali.parse_jalali(text)
    except (ValueError, OverflowError):
        raise ValueError(explain_invalid(text)) from None


def parse_many(text: str) -> tuple[list[dt.date], list[BadDate]]:
    """Dates typed one per line (or separated by spaces, commas or semicolons).

    Returns ``(dates, errors)``; dates are de-duplicated and sorted, every bad token is reported
    once with its reason.  Empty input gives two empty lists.
    """
    dates: list[dt.date] = []
    errors: list[BadDate] = []
    seen_bad: set[str] = set()
    glued = _AROUND_SEPARATOR_RE.sub(r"\1", clean_text(text))  # "1404 / 04 / 26" is one date
    for raw in _SPLIT_RE.split(glued):
        token = clean_text(raw)
        if not token:
            continue
        try:
            dates.append(parse_token(token))
        except ValueError as exc:
            if token not in seen_bad:
                seen_bad.add(token)
                errors.append(BadDate(token, str(exc)))
    return sorted(set(dates)), errors


def _check_span(start: dt.date, end: dt.date) -> None:
    if end < start:
        raise ValueError("تاریخ پایان نباید پیش از تاریخ شروع باشد.")
    if (end - start).days > MAX_SPAN_DAYS:
        raise ValueError("بازهٔ انتخابی بیش از حد طولانی است؛ حداکثر حدود دو سال.")


def weekly(start: dt.date, end: dt.date, weekdays: Iterable[int]) -> list[dt.date]:
    """Every day from *start* to *end* (inclusive) whose weekday is in *weekdays* (Saturday = 0 ... Friday = 6)."""
    wanted = {int(day) for day in weekdays}
    if not wanted or not wanted <= set(range(7)):
        raise ValueError("دست‌کم یک روز هفته را انتخاب کنید.")
    _check_span(start, end)
    out: list[dt.date] = []
    day = start
    while day <= end:
        if jalali.weekday_index(day) in wanted:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def monthly(
    start: dt.date,
    end: dt.date,
    days_of_month: Iterable[int],
    *,
    short_month: str = "clamp",
) -> list[dt.date]:
    """The given Jalali days of every Jalali month between *start* and *end* (inclusive).

    ``short_month`` decides what a day the month does not have means (the 31st of Mehr, the
    30th of a common Esfand): ``"clamp"`` takes the month's last day, ``"skip"`` leaves the
    month out.
    """
    if short_month not in ("clamp", "skip"):
        raise ValueError("short_month must be 'clamp' or 'skip'")
    wanted = sorted({int(day) for day in days_of_month})
    if not wanted or wanted[0] < 1 or wanted[-1] > 31:
        raise ValueError("روز ماه باید عددی از ۱ تا ۳۱ باشد.")
    _check_span(start, end)
    jy, jm, _ = jalali.to_jalali(start)
    end_y, end_m, _ = jalali.to_jalali(end)
    out: set[dt.date] = set()
    while (jy, jm) <= (end_y, end_m):
        length = jalali.jalali_month_length(jy, jm)
        for number in wanted:
            if number > length:
                if short_month == "skip":
                    continue
                number = length
            day = dt.date(*jalali.jalali_to_gregorian(jy, jm, number))
            if start <= day <= end:
                out.add(day)
        jy, jm = (jy, jm + 1) if jm < 12 else (jy + 1, 1)
    return sorted(out)


def merge(*groups: Iterable[dt.date]) -> list[dt.date]:
    """Sorted union of several date lists."""
    merged: set[dt.date] = set()
    for group in groups:
        merged.update(group)
    return sorted(merged)
