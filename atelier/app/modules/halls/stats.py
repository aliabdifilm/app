"""Numbers for the hall reports: events per month, offer acceptance rate, response time.

All of it is scoped to the hall ids the caller owns.  Money never appears here: a hall sees how
its dates were handled, not what an atelier earned.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional

import sqlalchemy as sa

from app.extensions import db
from app.models import Atelier, Event, EventOffer, Hall
from app.modules.halls.queries import BOOKED_STATUSES, FREE_STATUSES, ratings_for
from app.services import jalali
from app.services.constants import EventStatus, OfferStatus


# --------------------------------------------------------------------------
# events per Jalali month
# --------------------------------------------------------------------------


@dataclass
class MonthRow:
    jm: int
    label: str
    total: int = 0  # live events (not cancelled)
    booked: int = 0  # with an atelier: confirmed / done / invoiced
    free: int = 0  # still without an atelier
    cancelled: int = 0

    @property
    def booked_percent(self) -> int:
        return int(round(100 * self.booked / self.total)) if self.total else 0


def year_range(jy: int) -> tuple[dt.date, dt.date]:
    return jalali.jalali_month_range(jy, 1)[0], jalali.jalali_month_range(jy, 12)[1]


def events_per_month(hall_ids: Iterable[int], jy: int) -> list[MonthRow]:
    """Twelve rows (Farvardin..Esfand) counting the events of the Jalali year *jy*."""
    ids = sorted(set(hall_ids))
    rows = [MonthRow(jm, jalali.JALALI_MONTHS[jm - 1]) for jm in range(1, 13)]
    if not ids:
        return rows
    first, last = year_range(jy)
    result = db.session.execute(
        sa.select(Event.date, Event.status, Event.atelier_id.is_(None), sa.func.count(Event.id))
        .where(Event.hall_id.in_(ids), Event.date >= first, Event.date <= last)
        .group_by(Event.date, Event.status, Event.atelier_id.is_(None))
    )
    for day, status, no_atelier, number in result:
        row = rows[jalali.to_jalali(day)[1] - 1]
        status = EventStatus.coerce(status)
        if status == EventStatus.CANCELLED:
            row.cancelled += number
            continue
        row.total += number
        if status in BOOKED_STATUSES and not no_atelier:
            row.booked += number
        elif status in FREE_STATUSES:
            row.free += number
    return rows


def lost_dates(hall_ids: Iterable[int], today: dt.date, first: dt.date, last: dt.date) -> int:
    """Past events in the period that never got an atelier (a date the hall could not hand over)."""
    ids = sorted(set(hall_ids))
    if not ids:
        return 0
    return int(
        db.session.scalar(
            sa.select(sa.func.count(Event.id)).where(
                Event.hall_id.in_(ids),
                Event.date >= first,
                Event.date <= min(last, today - dt.timedelta(days=1)),
                Event.atelier_id.is_(None),
                Event.status != EventStatus.CANCELLED,
            )
        )
        or 0
    )


# --------------------------------------------------------------------------
# offers: acceptance rate and response time
# --------------------------------------------------------------------------


def _hours(delta: dt.timedelta) -> float:
    return max(0.0, delta.total_seconds() / 3600)


@dataclass
class OfferStats:
    """Direct offers of the period (made by the hall) and how ateliers answered."""

    sent: int = 0
    pending: int = 0
    accepted: int = 0
    declined: int = 0
    withdrawn: int = 0
    claims: int = 0  # events ateliers took from the public board themselves
    response_hours: list[float] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return self.accepted + self.declined

    @property
    def acceptance_rate(self) -> Optional[int]:
        """Accepted share of the offers that were answered, in percent (``None`` when nobody answered)."""
        return int(round(100 * self.accepted / self.answered)) if self.answered else None

    @property
    def average_hours(self) -> Optional[float]:
        return sum(self.response_hours) / len(self.response_hours) if self.response_hours else None

    @property
    def median_hours(self) -> Optional[float]:
        return statistics.median(self.response_hours) if self.response_hours else None


@dataclass
class AtelierRow:
    atelier: Atelier
    stats: OfferStats
    rating: Optional[float] = None
    rating_count: int = 0


def offer_stats(hall_ids: Iterable[int], first: dt.date, last: dt.date) -> tuple[OfferStats, list[AtelierRow]]:
    """Overall offer statistics for events dated in ``[first, last]`` plus one row per atelier.

    A *direct* offer is one the hall manager created (``offered_by`` is the hall's manager); an
    accepted offer created by the atelier itself is a *claim* from the public board and is
    counted apart, because nobody "answered" it.  The response time is
    ``responded_at - offered_at`` of answered direct offers.
    """
    ids = sorted(set(hall_ids))
    total = OfferStats()
    if not ids:
        return total, []
    direct = EventOffer.offered_by_id == Hall.manager_id
    base = (
        sa.select(EventOffer.atelier_id, EventOffer.status, direct, sa.func.count(EventOffer.id))
        .join(Event, Event.id == EventOffer.event_id)
        .join(Hall, Hall.id == Event.hall_id)
        .where(Event.hall_id.in_(ids), Event.date >= first, Event.date <= last)
        .group_by(EventOffer.atelier_id, EventOffer.status, direct)
    )
    per_atelier: dict[int, OfferStats] = {}
    for atelier_id, status, is_direct, number in db.session.execute(base):
        status = OfferStatus.coerce(status)
        mine = per_atelier.setdefault(atelier_id, OfferStats())
        for bucket in (total, mine):
            if not is_direct:
                if status == OfferStatus.ACCEPTED:
                    bucket.claims += number
                continue
            bucket.sent += number
            if status == OfferStatus.PENDING:
                bucket.pending += number
            elif status == OfferStatus.ACCEPTED:
                bucket.accepted += number
            elif status == OfferStatus.DECLINED:
                bucket.declined += number
            else:
                bucket.withdrawn += number

    answered = (
        sa.select(EventOffer.atelier_id, EventOffer.offered_at, EventOffer.responded_at)
        .join(Event, Event.id == EventOffer.event_id)
        .join(Hall, Hall.id == Event.hall_id)
        .where(
            Event.hall_id.in_(ids),
            Event.date >= first,
            Event.date <= last,
            direct,
            EventOffer.status.in_([OfferStatus.ACCEPTED, OfferStatus.DECLINED]),
            EventOffer.responded_at.is_not(None),
            EventOffer.offered_at.is_not(None),
        )
    )
    for atelier_id, offered_at, responded_at in db.session.execute(answered):
        hours = _hours(responded_at - offered_at)
        total.response_hours.append(hours)
        per_atelier.setdefault(atelier_id, OfferStats()).response_hours.append(hours)

    ateliers = {
        atelier.id: atelier
        for atelier in db.session.execute(sa.select(Atelier).where(Atelier.id.in_(list(per_atelier)))).scalars()
    } if per_atelier else {}
    ratings = ratings_for(per_atelier)
    rows = [
        AtelierRow(ateliers[atelier_id], stats, *(ratings.get(atelier_id) or (None, 0)))
        for atelier_id, stats in per_atelier.items()
        if atelier_id in ateliers
    ]
    rows.sort(key=lambda row: (-(row.stats.sent + row.stats.claims), row.atelier.name))
    return total, rows


def format_hours(hours: Optional[float]) -> str:
    """``None`` -> ``—``; under an hour in minutes, under two days in hours, else days (Persian digits)."""
    from app.services.money import to_persian_digits

    if hours is None:
        return "—"
    if hours < 1:
        return f"{to_persian_digits(max(1, round(hours * 60)))} دقیقه"
    if hours < 48:
        return f"{to_persian_digits(round(hours))} ساعت"
    return f"{to_persian_digits(round(hours / 24))} روز"
