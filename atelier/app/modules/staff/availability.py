"""The availability calendar: which days a staff member can or cannot work.

A :class:`~app.models.StaffAvailability` row means "I said something about this day"; no row means
unknown (probably free).  Other modules already read the rows:

* ``staffing.check_assignable(...).unavailable`` / ``staffing.is_unavailable`` warn (or, for ateliers that
  block double booking, refuse) when an atelier manager staffs a person on a day marked "unavailable";
* the marketplace talent search can filter by a free day;
* the afish board shows the mark next to the person.

This module is the write side.  Rules: the past cannot be edited, nor days further than
:data:`HORIZON_DAYS` ahead (keeps the table small); a day on which the person already has a committed
afish row cannot be marked (they are working that day).  Nothing here commits.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, StaffAvailability
from app.services import jalali
from app.services.constants import (
    COMMITTED_ASSIGNMENT_STATUSES,
    AvailabilityStatus,
    EventStatus,
)

HORIZON_DAYS = 548  # about 18 months
MAX_DAYS_PER_REQUEST = 400
MAX_RANGE_DAYS = 366
MAX_NOTE = 300

#: What a save can do to the selected days.
UNAVAILABLE = "unavailable"
AVAILABLE = "available"
CLEAR = "clear"
ACTIONS = (UNAVAILABLE, AVAILABLE, CLEAR)


@dataclass
class ApplyResult:
    """What :func:`apply_marks` did (the route turns it into one Persian sentence)."""

    marked: int = 0
    cleared: int = 0
    skipped_past: int = 0
    skipped_busy: int = 0
    skipped_far: int = 0

    @property
    def changed(self) -> int:
        return self.marked + self.cleared

    @property
    def skipped(self) -> int:
        return self.skipped_past + self.skipped_busy + self.skipped_far


def marks_between(user_id: int, first: dt.date, last: dt.date) -> dict[dt.date, StaffAvailability]:
    """``{date: row}`` of my availability marks in the window."""
    rows = db.session.execute(
        sa.select(StaffAvailability).where(
            StaffAvailability.user_id == user_id, StaffAvailability.date.between(first, last)
        )
    ).scalars()
    return {row.date: row for row in rows}


def busy_days(user_id: int, first: dt.date, last: dt.date) -> dict[dt.date, list[Assignment]]:
    """``{date: [committed afish rows]}`` - the days on which the person is working."""
    rows = db.session.execute(
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Assignment.involving_user(user_id),
            Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
            Event.date.between(first, last),
            Event.status != EventStatus.CANCELLED,
        )
        .options(joinedload(Assignment.event).joinedload(Event.hall))
        .order_by(Event.date, Assignment.id)
    ).scalars()
    days: dict[dt.date, list[Assignment]] = {}
    for row in rows:
        days.setdefault(row.event.date, []).append(row)
    return days


def expand_range(
    start: dt.date, end: dt.date, weekdays: Optional[Iterable[int]] = None
) -> list[dt.date]:
    """Every day from *start* to *end* (inclusive), optionally only on some weekdays.

    *weekdays* are Saturday-first indexes (0 = شنبه ... 6 = جمعه), like the calendar grid.  Raises
    ``ValueError`` for an inverted or too long range.
    """
    if end < start:
        raise ValueError("range is inverted")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError("range too long")
    wanted = None if weekdays is None else {int(w) for w in weekdays}
    days = []
    cursor = start
    while cursor <= end:
        if wanted is None or jalali.weekday_index(cursor) in wanted:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def apply_marks(
    user_id: int,
    days: Iterable[dt.date],
    action: str,
    *,
    today: dt.date,
    note: Optional[str] = None,
) -> ApplyResult:
    """Mark, free or clear *days* for the person (``action`` is one of :data:`ACTIONS`).

    Past days, days beyond the horizon and (for ``unavailable`` / ``available``) days with a committed afish
    row are skipped and counted.  ``note`` (a few words, optional) is stored on the marked rows.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown availability action {action!r}")
    result = ApplyResult()
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    candidates: list[dt.date] = []
    for day in sorted(set(days)):
        if day < today:
            result.skipped_past += 1
        elif day > horizon:
            result.skipped_far += 1
        else:
            candidates.append(day)
    if not candidates:
        return result
    existing = marks_between(user_id, candidates[0], candidates[-1])
    busy = busy_days(user_id, candidates[0], candidates[-1]) if action != CLEAR else {}
    text = " ".join((note or "").split())[:MAX_NOTE] or None
    status = AvailabilityStatus.UNAVAILABLE if action == UNAVAILABLE else AvailabilityStatus.AVAILABLE
    for day in candidates:
        row = existing.get(day)
        if action == CLEAR:
            if row is not None:
                db.session.delete(row)
                result.cleared += 1
            continue
        if day in busy:
            result.skipped_busy += 1
            continue
        if row is None:
            db.session.add(StaffAvailability(user_id=user_id, date=day, status=status, note=text))
        else:
            row.status = status
            row.note = text
        result.marked += 1
    db.session.flush()
    return result


def summary_text(result: ApplyResult, action: str) -> str:
    """One Persian sentence about what happened (used as the flash message)."""
    from app.services.money import to_persian_digits as fa

    parts = []
    if result.marked:
        what = "به‌عنوان «در دسترس نیستم»" if action == UNAVAILABLE else "به‌عنوان «در دسترس هستم»"
        parts.append(f"{fa(result.marked)} روز {what} ثبت شد")
    if result.cleared:
        parts.append(f"علامت {fa(result.cleared)} روز برداشته شد")
    if not parts:
        parts.append("تغییری ثبت نشد")
    extra = []
    if result.skipped_busy:
        extra.append(f"{fa(result.skipped_busy)} روز مراسم دارید")
    if result.skipped_past:
        extra.append(f"{fa(result.skipped_past)} روز گذشته است")
    if result.skipped_far:
        extra.append(f"{fa(result.skipped_far)} روز بیش از حد دور است")
    text = "، ".join(parts) + "."
    if extra:
        text += " نادیده گرفته شد: " + "، ".join(extra) + "."
    return text
