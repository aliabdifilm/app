"""Small helpers shared by the staff routes and templates (no database access here)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping, Optional

from app.services import jalali
from app.services.constants import (
    COMMITTED_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    EventStatus,
)
from app.services.money import normalize_digits, to_persian_digits

MIN_YEAR, MAX_YEAR = 1300, 1500

#: Badge tones of the assignment statuses (``components/ui.html`` badge()).
STATUS_TONES: dict[AssignmentStatus, str] = {
    AssignmentStatus.INVITED: "warning",
    AssignmentStatus.ACCEPTED: "success",
    AssignmentStatus.CONFIRMED: "success",
    AssignmentStatus.COMPLETED: "info",
    AssignmentStatus.DECLINED: "neutral",
    AssignmentStatus.CANCELLED: "danger",
}
#: Calendar chip tones (``calendar_month`` events).
CALENDAR_TONES: dict[AssignmentStatus, str] = {
    AssignmentStatus.INVITED: "warning",
    AssignmentStatus.ACCEPTED: "success",
    AssignmentStatus.CONFIRMED: "success",
    AssignmentStatus.COMPLETED: "info",
}


def status_tone(status: Any) -> str:
    return STATUS_TONES.get(AssignmentStatus.coerce(status), "neutral")


def venue_visible(assignment: Any) -> bool:
    """Hall address, phone and map are shown once the afish is committed (and the event still stands)."""
    return (
        assignment.status in COMMITTED_ASSIGNMENT_STATUSES
        and assignment.event.status != EventStatus.CANCELLED
    )


def call_time_of(assignment: Any) -> Optional[dt.time]:
    """When the person must be there: the agreed call time, else the start of the ceremony."""
    return assignment.call_time or assignment.event.start_time


def clock_text(value: Optional[dt.time]) -> str:
    """``18:30`` -> ``۱۸:۳۰`` (empty string for None)."""
    return to_persian_digits(value.strftime("%H:%M")) if value else ""


def person_label(user: Any) -> str:
    """A name that is never the mobile number (``User.display_name`` falls back to it)."""
    name = (getattr(user, "full_name", "") or "").strip()
    if not name or re.search(r"\d{8,}", normalize_digits(name).replace(" ", "")):
        return "کاربر بی‌نام"
    return name


# -- Jalali months -------------------------------------------------------------


def _int_arg(args: Mapping[str, Any], name: str) -> Optional[int]:
    text = normalize_digits(args.get(name, "")).strip()
    return int(text) if re.fullmatch(r"\d{1,4}", text) else None


def parse_month(args: Mapping[str, Any], today: dt.date) -> tuple[int, int]:
    """``(jalali year, month)`` from ``?y=1404&m=5`` (any digits); junk falls back to the current month."""
    cy, cm, _ = jalali.gregorian_to_jalali(today.year, today.month, today.day)
    year, month = _int_arg(args, "y"), _int_arg(args, "m")
    if year is None or not (MIN_YEAR <= year <= MAX_YEAR):
        return cy, cm
    if month is None or not (1 <= month <= 12):
        return year, cm if year == cy else 1
    return year, month


def parse_year(args: Mapping[str, Any], today: dt.date) -> int:
    year = _int_arg(args, "y")
    if year is None or not (MIN_YEAR <= year <= MAX_YEAR):
        return jalali.gregorian_to_jalali(today.year, today.month, today.day)[0]
    return year


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_title(year: int, month: int) -> str:
    return to_persian_digits(f"{jalali.JALALI_MONTHS[month - 1]} {year}")


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    return jalali.jalali_month_range(year, month)


def jalali_today(today: dt.date) -> tuple[int, int, int]:
    return jalali.gregorian_to_jalali(today.year, today.month, today.day)


# -- text ----------------------------------------------------------------------


def clip(text: Optional[str], length: int = 140) -> str:
    """A one-line excerpt for lists (whitespace collapsed, ellipsis added when cut)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= length else flat[: length - 1].rstrip() + "…"


def share_text(assignment: Any, *, include_venue: bool) -> str:
    """The afish as a short message for a messenger (no pay information)."""
    event = assignment.event
    hall = event.hall
    when = jalali.to_jalali_str(event.date, "weekday_long", persian_digits=True)
    lines = [f"افیش {assignment.role.label} - {hall.name if hall else ''}", when]
    start = call_time_of(assignment)
    if start:
        lines.append(("ساعت حضور: " if assignment.call_time else "شروع مراسم: ") + clock_text(start))
    if include_venue and hall is not None:
        if hall.address:
            lines.append(f"آدرس: {hall.address}")
        if hall.map_url and hall.map_url.startswith(("http://", "https://")):
            lines.append(f"نقشه: {hall.map_url}")
    return "\n".join(lines)
