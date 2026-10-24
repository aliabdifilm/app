"""Jalali (Persian / Solar Hijri) calendar helpers.

Pure Python, no web dependencies.  Dates are stored as Gregorian in the
database and shown as Jalali in the UI; this module converts between the two,
formats and parses Jalali dates, builds month grids for the calendar view and
provides "today" in Iran time.

The year start (1 Farvardin / Nowruz) comes from the Borkowski-style break
table used by the widely deployed *jalaali-js* library, which agrees with the
official calendar for every year the app can meet (the test-suite pins the
Nowruz anchors 1403, 1404 and 1405 and cross-checks the whole 1350-1450
range).  Supported Jalali years: 1 to 3000.

Conventions
-----------
* Weeks are Saturday-first: ``weekday_index`` is 0 for Saturday ... 6 for
  Friday.
* Iran has used a fixed UTC+03:30 offset since DST was abolished in 2022.
  :data:`IRAN_TZ` is that fixed offset; it is only used for "what day is it in
  Iran right now" and for converting UTC timestamps for display.
* Naive ``datetime`` values passed to the formatting helpers are used as-is
  (their wall-clock date) unless ``assume_utc=True``, in which case they are
  treated as UTC timestamps and converted to Iran time first.  Timezone-aware
  datetimes are always converted to Iran time.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Optional, Union

from .money import normalize_digits, to_persian_digits

__all__ = [
    "IRAN_TZ",
    "MIN_JALALI_YEAR",
    "MAX_JALALI_YEAR",
    "JALALI_MONTHS",
    "JALALI_WEEKDAYS",
    "DATE_FORMATS",
    "gregorian_to_jalali",
    "jalali_to_gregorian",
    "is_jalali_leap",
    "jalali_month_length",
    "weekday_index",
    "weekday_name",
    "to_date",
    "to_jalali",
    "to_iran_time",
    "to_jalali_str",
    "to_jalali_datetime_str",
    "parse_jalali",
    "month_grid",
    "jalali_month_range",
    "today",
]

#: Fixed Iran offset (UTC+03:30, no DST since 2022).
IRAN_TZ = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")

MIN_JALALI_YEAR = 1
MAX_JALALI_YEAR = 3000

JALALI_MONTHS = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

#: Saturday first, matching :func:`weekday_index`.
JALALI_WEEKDAYS = (
    "شنبه",
    "یکشنبه",
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنجشنبه",
    "جمعه",
)

#: Formats understood by :func:`to_jalali_str` (and the datetime variant).
#: numeric       1403/05/12
#: short         12 مرداد
#: long          12 مرداد 1403
#: weekday_long  جمعه 12 مرداد 1403
#: month_year    مرداد 1403
#: weekday       جمعه
DATE_FORMATS = ("numeric", "short", "long", "weekday_long", "month_year", "weekday")

DateLike = Union[date, datetime, str]

# jalaali-js break years (first year of each regular 33-year-cycle segment).
_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
           1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)

# Days before the first day of each Jalali month in a normal (non-leap) year.
_DAYS_BEFORE_MONTH = tuple(
    (m * 31) if m < 6 else (186 + (m - 6) * 30) for m in range(12)
)


# --------------------------------------------------------------------------
# Core conversion
# --------------------------------------------------------------------------

def _check_year(jy) -> int:
    if isinstance(jy, bool) or not isinstance(jy, int):
        raise ValueError(f"Jalali year must be an integer, got {jy!r}")
    if not MIN_JALALI_YEAR <= jy <= MAX_JALALI_YEAR:
        raise ValueError(
            f"Jalali year {jy} is outside the supported range "
            f"{MIN_JALALI_YEAR}..{MAX_JALALI_YEAR}"
        )
    return jy


@lru_cache(maxsize=512)
def _nowruz_ordinal(jy: int) -> int:
    """Proleptic-Gregorian ordinal (``date.toordinal``) of 1 Farvardin of *jy*."""
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    jump = 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += (jump // 33) * 8 + (jump % 33) // 4
        jp = jm
    n = jy - jp
    leap_j += (n // 33) * 8 + ((n % 33) + 3) // 4
    if jump % 33 == 4 and jump - n == 4:
        leap_j += 1
    leap_g = gy // 4 - ((gy // 100 + 1) * 3) // 4 - 150
    march_day = 20 + leap_j - leap_g
    return date(gy, 3, march_day).toordinal()


def is_jalali_leap(jy: int) -> bool:
    """True when Esfand of Jalali year *jy* has 30 days (1399, 1403, 1408, ...)."""
    _check_year(jy)
    return _nowruz_ordinal(jy + 1) - _nowruz_ordinal(jy) == 366


def jalali_month_length(jy: int, jm: int) -> int:
    """Number of days in Jalali month *jm* (1..12) of year *jy*."""
    _check_year(jy)
    if isinstance(jm, bool) or not isinstance(jm, int) or not 1 <= jm <= 12:
        raise ValueError(f"Jalali month must be 1..12, got {jm!r}")
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_jalali_leap(jy) else 29


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Convert a Jalali date to Gregorian ``(gy, gm, gd)``; ``ValueError`` if invalid."""
    length = jalali_month_length(jy, jm)  # validates jy and jm
    if isinstance(jd, bool) or not isinstance(jd, int) or not 1 <= jd <= length:
        raise ValueError(
            f"Invalid Jalali date {jy}/{jm}/{jd}: day must be 1..{length} for this month"
        )
    ordinal = _nowruz_ordinal(jy) + _DAYS_BEFORE_MONTH[jm - 1] + (jd - 1)
    d = date.fromordinal(ordinal)
    return d.year, d.month, d.day


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to Jalali ``(jy, jm, jd)``; ``ValueError`` if invalid."""
    d = date(gy, gm, gd)  # raises ValueError for impossible dates
    jy = gy - 621
    if not MIN_JALALI_YEAR <= jy <= MAX_JALALI_YEAR + 1:
        raise ValueError(f"Gregorian date {d.isoformat()} is outside the supported range")
    ordinal = d.toordinal()
    if ordinal < _nowruz_ordinal(jy):
        jy -= 1
    if not MIN_JALALI_YEAR <= jy <= MAX_JALALI_YEAR:
        raise ValueError(f"Gregorian date {d.isoformat()} is outside the supported range")
    day_of_year = ordinal - _nowruz_ordinal(jy)  # 0-based
    if day_of_year < 186:
        return jy, 1 + day_of_year // 31, day_of_year % 31 + 1
    rest = day_of_year - 186
    return jy, 7 + rest // 30, rest % 30 + 1


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------

def to_iran_time(value: datetime, assume_utc: bool = True) -> datetime:
    """Return *value* as a timezone-aware datetime in Iran time.

    Aware datetimes are converted; naive ones are interpreted as UTC when
    *assume_utc* is true (the project stores timestamps in UTC), otherwise as
    already being Iran local time.
    """
    if value.tzinfo is None:
        base = timezone.utc if assume_utc else IRAN_TZ
        value = value.replace(tzinfo=base)
    return value.astimezone(IRAN_TZ)


def _parse_iso(text: str) -> Union[date, datetime]:
    text = normalize_digits(text).strip()
    try:
        if len(text) <= 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Not an ISO date/datetime string: {text!r}") from None


def to_date(value: DateLike, assume_utc: bool = False) -> date:
    """Coerce a ``date`` / ``datetime`` / ISO string into a Gregorian ``date``.

    Aware datetimes are converted to Iran time first; naive datetimes are
    converted from UTC only when *assume_utc* is true.
    """
    if isinstance(value, str):
        value = _parse_iso(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None or assume_utc:
            value = to_iran_time(value, assume_utc=True)
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"Expected date, datetime or ISO string, got {type(value).__name__}")


def to_jalali(value: DateLike, assume_utc: bool = False) -> tuple[int, int, int]:
    """Return ``(jy, jm, jd)`` for a date-like value."""
    d = to_date(value, assume_utc=assume_utc)
    return gregorian_to_jalali(d.year, d.month, d.day)


def weekday_index(value: DateLike) -> int:
    """Saturday = 0, Sunday = 1, ... Friday = 6."""
    return (to_date(value).weekday() + 2) % 7


def weekday_name(value: DateLike) -> str:
    """Persian weekday name of a date-like value (``جمعه``)."""
    return JALALI_WEEKDAYS[weekday_index(value)]


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _format_parts(d: date, jy: int, jm: int, jd: int, fmt: str) -> str:
    month = JALALI_MONTHS[jm - 1]
    if fmt == "numeric":
        return f"{jy:04d}/{jm:02d}/{jd:02d}"
    if fmt == "short":
        return f"{jd} {month}"
    if fmt == "long":
        return f"{jd} {month} {jy}"
    if fmt == "weekday_long":
        return f"{JALALI_WEEKDAYS[weekday_index(d)]} {jd} {month} {jy}"
    if fmt == "month_year":
        return f"{month} {jy}"
    if fmt == "weekday":
        return JALALI_WEEKDAYS[weekday_index(d)]
    raise ValueError(f"Unknown date format {fmt!r}; expected one of {DATE_FORMATS}")


def to_jalali_str(
    value: DateLike,
    fmt: str = "numeric",
    persian_digits: bool = False,
    assume_utc: bool = False,
) -> str:
    """Format a date as Jalali text.

    ``fmt``: ``'numeric'`` -> ``1403/05/12``; ``'short'`` -> ``12 مرداد``;
    ``'long'`` -> ``12 مرداد 1403``; ``'weekday_long'`` -> ``جمعه 12 مرداد 1403``;
    plus ``'month_year'`` (``مرداد 1403``) and ``'weekday'`` (``جمعه``).

    *value* may be a ``date``, ``datetime`` or ISO string (``None`` / blank
    gives ``''`` so templates can pass optional columns).  See the module
    docstring for the timezone rules and *assume_utc*.
    """
    if fmt not in DATE_FORMATS:
        raise ValueError(f"Unknown date format {fmt!r}; expected one of {DATE_FORMATS}")
    if _is_blank(value):
        return ""
    d = to_date(value, assume_utc=assume_utc)
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    text = _format_parts(d, jy, jm, jd, fmt)
    return to_persian_digits(text) if persian_digits else text


def to_jalali_datetime_str(
    value: DateLike,
    fmt: str = "numeric",
    persian_digits: bool = False,
    seconds: bool = False,
    assume_utc: bool = True,
) -> str:
    """Format a timestamp as ``<jalali date> HH:MM`` in Iran time.

    Naive datetimes are treated as UTC timestamps (the project stores UTC) and
    converted to Iran time unless ``assume_utc=False``.  A plain ``date`` has no
    time part and is formatted like :func:`to_jalali_str`.
    """
    if fmt not in DATE_FORMATS:
        raise ValueError(f"Unknown date format {fmt!r}; expected one of {DATE_FORMATS}")
    if _is_blank(value):
        return ""
    if isinstance(value, str):
        value = _parse_iso(value)
    if isinstance(value, datetime):
        local = to_iran_time(value, assume_utc=assume_utc)
        jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
        text = _format_parts(local.date(), jy, jm, jd, fmt)
        clock = local.strftime("%H:%M:%S" if seconds else "%H:%M")
        text = f"{text} {clock}"
    elif isinstance(value, date):
        jy, jm, jd = gregorian_to_jalali(value.year, value.month, value.day)
        text = _format_parts(value, jy, jm, jd, fmt)
    else:
        raise TypeError(f"Expected date, datetime or ISO string, got {type(value).__name__}")
    return to_persian_digits(text) if persian_digits else text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_INVISIBLE_RE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_DASHES_RE = re.compile("[‐-―−]")
_SEPARATED_RE = re.compile(r"^(\d{4})\s*([/\-.])\s*(\d{1,2})\s*\2\s*(\d{1,2})$")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_jalali(text) -> date:
    """Parse Jalali text such as ``1403/05/12`` into a Gregorian ``date``.

    Separators ``/``, ``-`` and ``.`` are accepted (the same one twice), as are
    Persian, Arabic-Indic and Latin digits, 1- or 2-digit month/day and the
    compact ``14030512`` form.  Raises ``ValueError`` for anything else and for
    impossible dates (month 13, 31 Mehr, 1404/12/30 because 1404 is not leap).
    """
    if not isinstance(text, str):
        raise ValueError(f"Jalali date must be text, got {type(text).__name__}")
    cleaned = _INVISIBLE_RE.sub("", normalize_digits(text)).strip()
    cleaned = _DASHES_RE.sub("-", cleaned).replace("\\", "/")
    if cleaned == "":
        raise ValueError("Jalali date is empty")
    match = _SEPARATED_RE.match(cleaned)
    if match:
        jy, jm, jd = int(match.group(1)), int(match.group(3)), int(match.group(4))
    else:
        match = _COMPACT_RE.match(cleaned)
        if not match:
            raise ValueError(f"Not a Jalali date (expected YYYY/MM/DD): {text!r}")
        jy, jm, jd = (int(g) for g in match.groups())
    gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
    return date(gy, gm, gd)


# --------------------------------------------------------------------------
# Calendar helpers
# --------------------------------------------------------------------------

def jalali_month_range(jy: int, jm: int) -> tuple[date, date]:
    """First and last Gregorian dates of Jalali month *jm* of year *jy*."""
    length = jalali_month_length(jy, jm)
    first = date(*jalali_to_gregorian(jy, jm, 1))
    last = date(*jalali_to_gregorian(jy, jm, length))
    return first, last


def month_grid(jy: int, jm: int) -> list[list[Optional[date]]]:
    """Calendar grid of a Jalali month: a list of weeks of 7 cells, Saturday first.

    Each cell is the Gregorian ``date`` of that day, or ``None`` for padding
    before the 1st and after the last day of the month.
    """
    first, last = jalali_month_range(jy, jm)
    cells: list[Optional[date]] = [None] * weekday_index(first)
    day = first
    while day <= last:
        cells.append(day)
        day += timedelta(days=1)
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


ClockLike = Union[Callable[[], datetime], datetime, None]


def today(clock: ClockLike = None) -> date:
    """Today's date in Iran time.

    *clock* is for tests: a zero-argument callable returning a ``datetime`` (or
    a ``datetime`` directly).  Naive values are interpreted as UTC; aware ones
    are converted to Iran time.  Without a clock the real UTC time is used.
    """
    if clock is None:
        now = datetime.now(timezone.utc)
    else:
        now = clock() if callable(clock) else clock
        if not isinstance(now, datetime):
            raise TypeError("clock must return a datetime")
    return to_iran_time(now, assume_utc=True).date()
