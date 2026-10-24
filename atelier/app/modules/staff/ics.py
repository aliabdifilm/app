"""iCalendar (RFC 5545) export of afish rows: "add to my calendar".

Pure functions, no Flask: the route passes assignments and gets text back.

* lines end with CRLF and are folded at 75 *octets* (never inside a UTF-8 character);
* TEXT values are escaped (``\\`` ``;`` ``,`` and newlines);
* an event with a call time or start time is a timed event written in UTC (Iran has no daylight saving
  time since 2022, the offset is a fixed +03:30), otherwise it is an all-day event;
* pay terms never go into a calendar file; venue address and phone only when the caller says the afish is
  committed (``include_venue=True``), like on the web page.
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

from app.services.jalali import IRAN_TZ
from app.services.money import to_persian_digits

CRLF = "\r\n"
PRODID = "-//Atelier Sayyar//Staff schedule 1.0//FA"
#: Wedding photography has no known end time; calendars need one, so a timed event lasts this long.
DEFAULT_DURATION = dt.timedelta(hours=5)
ALARM_BEFORE = dt.timedelta(hours=2)
_EPOCH = dt.datetime(2020, 1, 1)
_UTC = dt.timezone.utc


def escape_text(value: object) -> str:
    """Escape a TEXT value (RFC 5545 section 3.3.11)."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold *line* so that no physical line exceeds 75 octets (continuations start with one space)."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append("".join(current))
            current = [char]
            size = 1 + width  # the leading space of the continuation line counts too
        else:
            current.append(char)
            size += width
    parts.append("".join(current))
    return CRLF.join([parts[0]] + [" " + part for part in parts[1:]])


def _utc_stamp(value: dt.datetime) -> str:
    return value.astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")


def _local_to_utc(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock.replace(tzinfo=None), tzinfo=IRAN_TZ).astimezone(_UTC)


def _aware_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)


def _valid_http_url(url: Optional[str]) -> bool:
    return bool(url) and str(url).startswith(("https://", "http://")) and "\n" not in str(url) and "\r" not in str(url)


def vevent_lines(
    assignment,
    *,
    now: dt.datetime,
    domain: str = "atelier.local",
    include_venue: bool = False,
    url: Optional[str] = None,
) -> list[str]:
    """The unfolded content lines of one VEVENT for *assignment* (an ``Assignment`` with its event loaded)."""
    event = assignment.event
    hall = event.hall
    clock = assignment.call_time or event.start_time
    title_parts = [assignment.role.label]
    if hall is not None and hall.name:
        title_parts.append(hall.name)
    summary = "مراسم - " + " - ".join(title_parts)
    committed = assignment.status.value != "invited"

    description: list[str] = [f"نقش: {assignment.role.label}"]
    if event.atelier is not None and event.atelier.name:
        description.append(f"اتلیه: {event.atelier.name}")
    if assignment.call_time:
        description.append("ساعت حضور: " + to_persian_digits(assignment.call_time.strftime("%H:%M")))
    if event.start_time:
        description.append("شروع مراسم: " + to_persian_digits(event.start_time.strftime("%H:%M")))
    if not committed:
        description.append("هنوز دعوت را نپذیرفته‌اید.")

    location = ""
    if include_venue and hall is not None:
        location = " - ".join(part for part in (hall.name, hall.city, hall.address) if part)
        if hall.phone:
            description.append(f"تلفن تالار: {hall.phone}")
        if _valid_http_url(hall.map_url):
            description.append(f"نقشه: {hall.map_url}")
    elif hall is not None and hall.name:
        location = hall.name
    if assignment.notes:
        description.append(f"توضیحات اتلیه: {assignment.notes}")

    stamp = _aware_utc(assignment.updated_at or assignment.created_at or now)
    sequence = max(0, int((stamp.replace(tzinfo=None) - _EPOCH).total_seconds() // 60))
    lines = [
        "BEGIN:VEVENT",
        f"UID:sayyar-assignment-{assignment.id}@{domain}",
        f"DTSTAMP:{_utc_stamp(_aware_utc(now))}",
        f"LAST-MODIFIED:{_utc_stamp(stamp)}",
        f"SEQUENCE:{sequence}",
    ]
    if clock is not None:
        start = _local_to_utc(event.date, clock)
        lines += [f"DTSTART:{_utc_stamp(start)}", f"DTEND:{_utc_stamp(start + DEFAULT_DURATION)}"]
        alarm = ALARM_BEFORE
    else:
        lines += [
            f"DTSTART;VALUE=DATE:{event.date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(event.date + dt.timedelta(days=1)).strftime('%Y%m%d')}",
        ]
        alarm = dt.timedelta(hours=12)  # noon of the day before
    lines += [
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(chr(10).join(description))}",
        f"STATUS:{'CONFIRMED' if committed else 'TENTATIVE'}",
        "TRANSP:OPAQUE",
    ]
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    if _valid_http_url(url):
        lines.append(f"URL:{url}")
    total_minutes = int(alarm.total_seconds() // 60)
    lines += [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{escape_text('یادآوری: ' + summary)}",
        f"TRIGGER:-PT{total_minutes // 60}H" if total_minutes % 60 == 0 else f"TRIGGER:-PT{total_minutes}M",
        "END:VALARM",
        "END:VEVENT",
    ]
    return lines


def build_calendar(
    assignments: Iterable,
    *,
    now: dt.datetime,
    domain: str = "atelier.local",
    name: str = "برنامه کاری اتلیه سیار",
    venue_visible=lambda assignment: False,
    url_for_assignment=lambda assignment: None,
) -> str:
    """A complete VCALENDAR document (CRLF line endings) for *assignments*."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(name)}",
        "X-WR-TIMEZONE:Asia/Tehran",
    ]
    for assignment in assignments:
        lines += vevent_lines(
            assignment,
            now=now,
            domain=domain,
            include_venue=bool(venue_visible(assignment)),
            url=url_for_assignment(assignment),
        )
    lines.append("END:VCALENDAR")
    return CRLF.join(fold_line(line) for line in lines) + CRLF
