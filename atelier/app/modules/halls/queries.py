"""Reusable SELECTs and small aggregations of the hall panel.

Everything takes the hall ids the caller already owns (``my_hall_ids()``), so a query can never
reach another manager's rows; list pages load their related rows with joins / one extra grouped
query, never one query per row.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager, joinedload

from app.extensions import db
from app.models import Assignment, Atelier, AuditLog, Event, EventOffer, Hall, Review
from app.services import staffing
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    ROLE_DISPLAY_ORDER,
    EventStatus,
    OfferStatus,
)

WORKING_STATUSES = (EventStatus.DRAFT, EventStatus.OPEN, EventStatus.OFFERED, EventStatus.CONFIRMED)
FREE_STATUSES = (EventStatus.DRAFT, EventStatus.OPEN, EventStatus.OFFERED)
FINISHED_STATUSES = (EventStatus.DONE, EventStatus.INVOICED)
BOOKED_STATUSES = (EventStatus.CONFIRMED, EventStatus.DONE, EventStatus.INVOICED)

EVENT_TABS = ("upcoming", "free", "offered", "confirmed", "past", "cancelled")


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def events_of(hall_ids: Iterable[int]) -> sa.Select:
    """Events of these halls with hall and atelier joined (``contains_eager``) so cards need no extra query."""
    return (
        sa.select(Event)
        .join(Hall, Hall.id == Event.hall_id)
        .outerjoin(Atelier, Atelier.id == Event.atelier_id)
        .where(Event.hall_id.in_(sorted(set(hall_ids))))
        .options(contains_eager(Event.hall), contains_eager(Event.atelier))
    )


def tab_condition(tab: str, today: dt.date) -> Any:
    """``WHERE`` clause of one tab of the events list."""
    if tab == "free":
        return sa.and_(Event.atelier_id.is_(None), Event.status.in_(FREE_STATUSES), Event.date >= today)
    if tab == "offered":
        return sa.and_(Event.status == EventStatus.OFFERED, Event.date >= today)
    if tab == "confirmed":
        return sa.and_(Event.status == EventStatus.CONFIRMED, Event.date >= today)
    if tab == "past":
        return sa.and_(
            Event.status != EventStatus.CANCELLED,
            sa.or_(Event.date < today, Event.status.in_(FINISHED_STATUSES)),
        )
    if tab == "cancelled":
        return Event.status == EventStatus.CANCELLED
    return sa.and_(Event.status.in_(WORKING_STATUSES), Event.date >= today)  # upcoming


def search_condition(text: str) -> Any:
    """Free-text filter on title, hall name and atelier name (LIKE wildcards of the input are escaped)."""
    pattern = like_pattern(text)
    return sa.or_(
        Event.title.ilike(pattern, escape="\\"),
        Hall.name.ilike(pattern, escape="\\"),
        Atelier.name.ilike(pattern, escape="\\"),
    )


def like_pattern(text: str) -> str:
    """``%text%`` with the wildcards of *text* escaped (use ``escape="\\"``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def order_for(tab: str) -> tuple[Any, ...]:
    if tab in ("past", "cancelled"):
        return (Event.date.desc(), Event.id.desc())
    return (Event.date, Event.start_time, Event.id)


def tab_counts(hall_ids: Iterable[int], today: dt.date) -> dict[str, int]:
    """Number of events in every tab, from one grouped query."""
    ids = sorted(set(hall_ids))
    counts = {tab: 0 for tab in EVENT_TABS}
    if not ids:
        return counts
    future_flag = sa.case((Event.date >= today, 1), else_=0).label("is_future")
    free_flag = sa.case((Event.atelier_id.is_(None), 1), else_=0).label("is_free")
    rows = db.session.execute(
        sa.select(Event.status, future_flag, free_flag, sa.func.count(Event.id))
        .where(Event.hall_id.in_(ids))
        .group_by(Event.status, future_flag, free_flag)
    )
    for status, future, free, number in rows:
        status = EventStatus.coerce(status)
        if status == EventStatus.CANCELLED:
            counts["cancelled"] += number
            continue
        if future and status in WORKING_STATUSES:
            counts["upcoming"] += number
        if future and free and status in FREE_STATUSES:
            counts["free"] += number
        if future and status == EventStatus.OFFERED:
            counts["offered"] += number
        if future and status == EventStatus.CONFIRMED:
            counts["confirmed"] += number
        if (not future) or status in FINISHED_STATUSES:
            counts["past"] += number
    return counts


def pending_offer_counts(event_ids: Sequence[int]) -> dict[int, int]:
    """Pending direct offers per event (one grouped query for a whole page)."""
    if not event_ids:
        return {}
    rows = db.session.execute(
        sa.select(EventOffer.event_id, sa.func.count(EventOffer.id))
        .where(EventOffer.event_id.in_(list(event_ids)), EventOffer.status == OfferStatus.PENDING)
        .group_by(EventOffer.event_id)
    )
    return {event_id: number for event_id, number in rows}


def month_events(hall_ids: Iterable[int], first: dt.date, last: dt.date) -> list[Event]:
    """Live (not cancelled) events of the halls between two dates, in calendar order."""
    return list(
        db.session.execute(
            events_of(hall_ids)
            .where(Event.date >= first, Event.date <= last, Event.status != EventStatus.CANCELLED)
            .order_by(Event.date, Event.start_time, Event.id)
        ).scalars().unique()
    )


# --------------------------------------------------------------------------
# offers
# --------------------------------------------------------------------------


def offers_of(hall_ids: Iterable[int]) -> sa.Select:
    """Offers for events of these halls with event, hall and atelier joined."""
    return (
        sa.select(EventOffer)
        .join(Event, Event.id == EventOffer.event_id)
        .join(Hall, Hall.id == Event.hall_id)
        .join(Atelier, Atelier.id == EventOffer.atelier_id)
        .where(Event.hall_id.in_(sorted(set(hall_ids))))
        .options(
            contains_eager(EventOffer.event).contains_eager(Event.hall),
            contains_eager(EventOffer.atelier),
        )
    )


def offer_status_counts(hall_ids: Iterable[int]) -> dict[str, int]:
    ids = sorted(set(hall_ids))
    counts = {status.value: 0 for status in OfferStatus}
    if not ids:
        return counts
    rows = db.session.execute(
        sa.select(EventOffer.status, sa.func.count(EventOffer.id))
        .join(Event, Event.id == EventOffer.event_id)
        .where(Event.hall_id.in_(ids))
        .group_by(EventOffer.status)
    )
    for status, number in rows:
        counts[OfferStatus.coerce(status).value] = number
    return counts


def pending_offer_total(hall_ids: Iterable[int], today: dt.date) -> int:
    """Pending offers waiting for an answer (for the menu badge and the dashboard)."""
    ids = sorted(set(hall_ids))
    if not ids:
        return 0
    return int(
        db.session.scalar(
            sa.select(sa.func.count(EventOffer.id))
            .join(Event, Event.id == EventOffer.event_id)
            .where(Event.hall_id.in_(ids), EventOffer.status == OfferStatus.PENDING, Event.date >= today)
        )
        or 0
    )


def pending_badge_for_manager(manager_id: int, today: dt.date) -> int:
    """Same count straight from the manager id (the nav badge does not know the hall ids)."""
    return int(
        db.session.scalar(
            sa.select(sa.func.count(EventOffer.id))
            .join(Event, Event.id == EventOffer.event_id)
            .join(Hall, Hall.id == Event.hall_id)
            .where(Hall.manager_id == manager_id, EventOffer.status == OfferStatus.PENDING, Event.date >= today)
        )
        or 0
    )


# --------------------------------------------------------------------------
# ratings of ateliers
# --------------------------------------------------------------------------


def ratings_for(atelier_ids: Iterable[int]) -> dict[int, tuple[float, int]]:
    """``{atelier_id: (average stars, number of ratings)}`` of hall ratings (hidden ones excluded)."""
    ids = sorted({int(i) for i in atelier_ids})
    if not ids:
        return {}
    rows = db.session.execute(
        sa.select(Review.atelier_id, sa.func.avg(Review.rating), sa.func.count(Review.id))
        .where(Review.atelier_id.in_(ids), Review.is_hidden.is_(False))
        .group_by(Review.atelier_id)
    )
    return {atelier_id: (float(avg), int(number)) for atelier_id, avg, number in rows}


def events_to_rate(hall_ids: Iterable[int], author_id: int, today: dt.date, limit: int = 5) -> tuple[int, list[Event]]:
    """Past events with an atelier that this manager has not rated yet: (total, newest ``limit``)."""
    ids = sorted(set(hall_ids))
    if not ids:
        return 0, []
    rated = sa.select(Review.event_id).where(Review.author_id == author_id, Review.event_id.is_not(None))
    condition = sa.and_(
        Event.hall_id.in_(ids),
        Event.atelier_id.is_not(None),
        Event.status.in_(BOOKED_STATUSES),
        Event.date <= today,
        Event.id.not_in(rated),
    )
    total = int(db.session.scalar(sa.select(sa.func.count(Event.id)).where(condition)) or 0)
    rows = list(
        db.session.execute(
            events_of(ids).where(condition).order_by(Event.date.desc(), Event.id.desc()).limit(limit)
        ).scalars().unique()
    )
    return total, rows


# --------------------------------------------------------------------------
# one event
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    """What the hall may know about the staffing of an event: head-counts only, never pay or invoices."""

    needed: int
    filled: int
    invited: int
    roles: list[dict[str, Any]]

    @property
    def has_staff(self) -> bool:
        return self.filled + self.invited > 0


def outcome_of(event: Event) -> Outcome:
    """Staff head-count per role (committed / waiting / needed) of an event with an atelier."""
    summary = staffing.staffing_summary(event)
    roles = [
        {
            "role": role,
            "label": role.label,
            "needed": summary[role].needed,
            "filled": summary[role].filled,
            "invited": summary[role].invited,
        }
        for role in ROLE_DISPLAY_ORDER
    ]
    return Outcome(summary.needed_total, summary.filled_total, summary.invited_total, roles)


def staff_counts(event_ids: Sequence[int]) -> dict[int, int]:
    """Committed staff (accepted / confirmed / completed) per event, one grouped query."""
    if not event_ids:
        return {}
    committed = [status for status in ACTIVE_ASSIGNMENT_STATUSES if status.value != "invited"]
    rows = db.session.execute(
        sa.select(Assignment.event_id, sa.func.count(Assignment.id))
        .where(Assignment.event_id.in_(list(event_ids)), Assignment.status.in_(committed))
        .group_by(Assignment.event_id)
    )
    return {event_id: number for event_id, number in rows}


def event_offers_list(event: Event) -> list[EventOffer]:
    """Every offer of one event (newest first) with the atelier and its manager loaded."""
    return list(
        db.session.execute(
            sa.select(EventOffer)
            .where(EventOffer.event_id == event.id)
            .options(joinedload(EventOffer.atelier).joinedload(Atelier.manager))
            .order_by(EventOffer.offered_at.desc(), EventOffer.id.desc())
        ).scalars().unique()
    )


def timeline(event: Event, offers: Sequence[EventOffer], limit: int = 40) -> list[AuditLog]:
    """Audit entries about the event and its offers, newest first."""
    offer_ids = [offer.id for offer in offers]
    condition = sa.and_(AuditLog.entity_type == Event.__tablename__, AuditLog.entity_id == event.id)
    if offer_ids:
        condition = sa.or_(
            condition,
            sa.and_(AuditLog.entity_type == EventOffer.__tablename__, AuditLog.entity_id.in_(offer_ids)),
        )
    return list(
        db.session.execute(
            sa.select(AuditLog)
            .where(condition)
            .options(joinedload(AuditLog.actor))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        ).scalars()
    )


# --------------------------------------------------------------------------
# occupancy
# --------------------------------------------------------------------------


@dataclass
class Occupancy:
    """How booked a hall is in one Jalali month."""

    hall: Hall
    days_in_month: int
    days_with_events: int
    days_booked: int  # days with an event that has an atelier (confirmed / done / invoiced)
    events: int

    @property
    def percent(self) -> int:
        return int(round(100 * self.days_with_events / self.days_in_month)) if self.days_in_month else 0

    @property
    def booked_percent(self) -> int:
        return int(round(100 * self.days_booked / self.days_in_month)) if self.days_in_month else 0


def occupancy(halls: Sequence[Hall], first: dt.date, last: dt.date) -> list[Occupancy]:
    """Per hall: days that have an event, days that are booked by an atelier, events (one grouped query)."""
    days = (last - first).days + 1
    stats = {hall.id: [set(), set(), 0] for hall in halls}
    booked = sa.case((sa.and_(Event.atelier_id.is_not(None), Event.status.in_(BOOKED_STATUSES)), 1), else_=0)
    rows = db.session.execute(
        sa.select(Event.hall_id, Event.date, sa.func.count(Event.id), sa.func.sum(booked))
        .where(
            Event.hall_id.in_(list(stats)),
            Event.date >= first,
            Event.date <= last,
            Event.status != EventStatus.CANCELLED,
        )
        .group_by(Event.hall_id, Event.date)
    )
    for hall_id, day, number, booked_number in rows:
        entry = stats[hall_id]
        entry[0].add(day)
        if booked_number:
            entry[1].add(day)
        entry[2] += number
    return [Occupancy(hall, days, len(stats[hall.id][0]), len(stats[hall.id][1]), stats[hall.id][2]) for hall in halls]
