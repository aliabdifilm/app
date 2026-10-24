"""Reusable SELECTs of the atelier panel (always scoped by atelier ids the caller already owns)."""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager, joinedload

from app.models import Atelier, Event, EventOffer, Hall
from app.services.constants import EventStatus, OfferMode, OfferStatus

WORKING_STATUSES = (EventStatus.DRAFT, EventStatus.OPEN, EventStatus.OFFERED, EventStatus.CONFIRMED)
FINISHED_STATUSES = (EventStatus.DONE, EventStatus.INVOICED, EventStatus.CANCELLED)


def _incoming_conditions(atelier_ids: Iterable[int], today: dt.date) -> tuple:
    return (
        EventOffer.atelier_id.in_(sorted(set(atelier_ids))),
        EventOffer.status == OfferStatus.PENDING,
        Event.atelier_id.is_(None),
        Event.status.in_([EventStatus.OPEN, EventStatus.OFFERED]),
        Event.date >= today,
    )


def incoming_offer_count(atelier_ids: Iterable[int], today: dt.date) -> sa.Select:
    """``SELECT count(...)`` of :func:`incoming_offers` (for badges and tab counters)."""
    return (
        sa.select(sa.func.count(EventOffer.id))
        .join(Event, Event.id == EventOffer.event_id)
        .where(*_incoming_conditions(atelier_ids, today))
    )


def incoming_offer_count_for_manager(manager_id: int, today: dt.date) -> sa.Select:
    """Pending offers across all ateliers of one manager in a single statement (the menu badge)."""
    return (
        sa.select(sa.func.count(EventOffer.id))
        .join(Event, Event.id == EventOffer.event_id)
        .join(Atelier, Atelier.id == EventOffer.atelier_id)
        .where(
            Atelier.manager_id == manager_id,
            EventOffer.status == OfferStatus.PENDING,
            Event.atelier_id.is_(None),
            Event.status.in_([EventStatus.OPEN, EventStatus.OFFERED]),
            Event.date >= today,
        )
    )


def incoming_offers(atelier_ids: Iterable[int], today: dt.date) -> sa.Select:
    """Pending direct offers addressed to these ateliers for events that are still free."""
    return (
        sa.select(EventOffer)
        .join(Event, Event.id == EventOffer.event_id)
        .where(*_incoming_conditions(atelier_ids, today))
        .options(
            contains_eager(EventOffer.event).joinedload(Event.hall),
            joinedload(EventOffer.atelier),
            joinedload(EventOffer.offered_by),
        )
        .order_by(Event.date, Event.start_time, EventOffer.id)
    )


def _open_conditions(atelier_ids: Iterable[int], today: dt.date, city: Optional[str]) -> list:
    answered = sa.select(EventOffer.event_id).where(
        EventOffer.atelier_id.in_(sorted(set(atelier_ids))),
        EventOffer.status.in_([OfferStatus.PENDING, OfferStatus.DECLINED]),
    )
    conditions = [
        Event.offer_mode == OfferMode.OPEN,
        Event.status == EventStatus.OPEN,
        Event.atelier_id.is_(None),
        Event.date >= today,
        Event.id.not_in(answered),
    ]
    if city:
        conditions.append(Hall.city == city)
    return conditions


def open_events(atelier_ids: Iterable[int], today: dt.date, city: Optional[str] = None) -> sa.Select:
    """Events the hall opened to every atelier that this atelier has not answered yet."""
    return (
        sa.select(Event)
        .join(Hall, Hall.id == Event.hall_id)
        .where(*_open_conditions(atelier_ids, today, city))
        .options(contains_eager(Event.hall))
        .order_by(Event.date, Event.start_time, Event.id)
    )


def open_event_count(atelier_ids: Iterable[int], today: dt.date, city: Optional[str] = None) -> sa.Select:
    """``SELECT count(...)`` of :func:`open_events`."""
    return (
        sa.select(sa.func.count(Event.id))
        .join(Hall, Hall.id == Event.hall_id)
        .where(*_open_conditions(atelier_ids, today, city))
    )


def my_events(atelier_id: int) -> sa.Select:
    return sa.select(Event).where(Event.atelier_id == atelier_id).options(joinedload(Event.hall))


def upcoming(atelier_id: int, today: dt.date) -> sa.Select:
    return (
        my_events(atelier_id)
        .where(Event.date >= today, Event.status.in_(WORKING_STATUSES))
        .order_by(Event.date, Event.start_time, Event.id)
    )


def past(atelier_id: int, today: dt.date) -> sa.Select:
    return (
        my_events(atelier_id)
        .where(sa.or_(Event.date < today, Event.status.in_(FINISHED_STATUSES)))
        .order_by(Event.date.desc(), Event.id.desc())
    )
