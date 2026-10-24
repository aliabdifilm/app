"""The hall manager's home: what is coming, what is waiting for an answer, how booked the month is."""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from flask import render_template

from app.extensions import db
from app.models import Event, EventOffer
from app.modules.halls import bp, queries
from app.modules.halls.common import MANAGER, hall_filter, hall_options, month_title, my_halls
from app.services import jalali, security
from app.services.constants import OfferStatus
from app.services.permissions import get_user, role_required

SOON_DAYS = 14
RECENT_DAYS = 14


@bp.get("/")
@role_required(MANAGER)
def dashboard():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    user = get_user()
    today = security.today()
    now = security.now()
    jy, jm, _ = jalali.to_jalali(today)
    first, last = jalali.jalali_month_range(jy, jm)

    upcoming = list(
        db.session.execute(
            queries.events_of(ids)
            .where(queries.tab_condition("upcoming", today))
            .order_by(*queries.order_for("upcoming"))
            .limit(6)
        ).scalars().unique()
    )
    pending_counts = queries.pending_offer_counts([event.id for event in upcoming])
    tab_counts = queries.tab_counts(ids, today)

    soon_limit = today + dt.timedelta(days=SOON_DAYS)
    soon_free = db.session.scalar(
        sa.select(sa.func.count(Event.id)).where(
            Event.hall_id.in_(ids),
            Event.atelier_id.is_(None),
            Event.status.in_(queries.FREE_STATUSES),
            Event.date >= today,
            Event.date <= soon_limit,
        )
    ) or 0

    pending_offers = list(
        db.session.execute(
            queries.offers_of(ids)
            .where(EventOffer.status == OfferStatus.PENDING, Event.date >= today)
            .order_by(Event.date, EventOffer.id)
            .limit(5)
        ).scalars().unique()
    )
    pending_total = queries.pending_offer_total(ids, today)
    answers = list(
        db.session.execute(
            queries.offers_of(ids)
            .where(
                EventOffer.status.in_([OfferStatus.ACCEPTED, OfferStatus.DECLINED]),
                EventOffer.responded_at >= now - dt.timedelta(days=RECENT_DAYS),
            )
            .order_by(EventOffer.responded_at.desc(), EventOffer.id.desc())
            .limit(5)
        ).scalars().unique()
    )
    to_rate_total, to_rate = queries.events_to_rate(ids, user.id, today)
    scope = [hall for hall in halls if hall.id in ids]
    occupancy = queries.occupancy(scope, first, last)
    month_events = sum(row.events for row in occupancy)
    days = sum(row.days_in_month for row in occupancy)
    busy_days = sum(row.days_with_events for row in occupancy)
    booked_days = sum(row.days_booked for row in occupancy)
    return render_template(
        "halls/dashboard.html",
        halls=halls,
        selected=selected,
        hall_choices=hall_options(halls),
        upcoming=upcoming,
        pending=pending_counts,
        counts=tab_counts,
        soon_free=soon_free,
        soon_days=SOON_DAYS,
        pending_offers=pending_offers,
        pending_total=pending_total,
        answers=answers,
        recent_days=RECENT_DAYS,
        to_rate=to_rate,
        to_rate_total=to_rate_total,
        occupancy=occupancy,
        month_title=month_title(jy, jm),
        month_events=month_events,
        busy_percent=int(round(100 * busy_days / days)) if days else 0,
        booked_percent=int(round(100 * booked_days / days)) if days else 0,
        busy_days=busy_days,
        booked_days=booked_days,
        month_days=days,
        today=today,
    )
