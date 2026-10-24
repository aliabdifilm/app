"""Dashboard of the atelier manager: what needs attention today."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from flask import redirect, render_template, request, url_for

from app.extensions import db
from app.models import Event, Invoice
from app.modules.ateliers import bp, links, queries
from app.modules.ateliers import board as boards
from app.modules.ateliers.common import MANAGER, current_atelier
from app.services import jalali, notifications, security
from app.services.constants import EventStatus, InvoiceStatus, ROLE_DISPLAY_ORDER
from app.services.permissions import get_my_ateliers, get_user, role_required, set_current_atelier

URGENT_DAYS = 3
ALERT_HORIZON_DAYS = 14
UPCOMING_SHOWN = 6


def month_kpis(atelier_id: int, first: dt.date, last: dt.date) -> dict[str, Any]:
    """Figures of one Jalali month from the invoice totals snapshot (submitted and settled invoices only)."""
    row = db.session.execute(
        sa.select(
            sa.func.count(Invoice.id),
            sa.func.coalesce(sa.func.sum(Invoice.income_total), 0),
            sa.func.coalesce(sa.func.sum(Invoice.expense_total), 0),
            sa.func.coalesce(sa.func.sum(Invoice.net_profit), 0),
            sa.func.coalesce(sa.func.sum(Invoice.units_total), 0),
        )
        .join(Event, Event.id == Invoice.event_id)
        .where(
            Invoice.atelier_id == atelier_id,
            Invoice.status.in_([InvoiceStatus.SUBMITTED, InvoiceStatus.SETTLED]),
            Event.date >= first,
            Event.date <= last,
        )
    ).one()
    events = db.session.scalar(
        sa.select(sa.func.count(Event.id)).where(
            Event.atelier_id == atelier_id,
            Event.date >= first,
            Event.date <= last,
            Event.status != EventStatus.CANCELLED,
        )
    )
    return {
        "invoices": int(row[0]),
        "income": int(row[1]),
        "expenses": int(row[2]),
        "net": int(row[3]),
        "units": int(row[4]),
        "events": int(events or 0),
    }


def staffing_alerts(boards_by_id: dict[int, boards.EventBoard], today: dt.date) -> list[dict[str, Any]]:
    """One alert per event that still misses people, with the shortfall per role."""
    alerts: list[dict[str, Any]] = []
    for board in sorted(boards_by_id.values(), key=lambda b: (b.event.date, b.event.id)):
        event = board.event
        days = (event.date - today).days
        if days < 0 or days > ALERT_HORIZON_DAYS:
            continue
        roles = []
        for role in ROLE_DISPLAY_ORDER:
            slots = board.summary[role]
            if slots.shortfall > 0:
                roles.append(
                    {"role": role, "shortfall": slots.shortfall, "missing": slots.missing, "waiting": slots.invited}
                )
        if roles:
            alerts.append(
                {
                    "event": event,
                    "days": days,
                    "tone": "danger" if days <= URGENT_DAYS else "warning",
                    "roles": roles,
                }
            )
    return alerts


@bp.get("/")
@role_required(MANAGER)
def dashboard():
    user = get_user()
    atelier = current_atelier()
    today = security.today()
    jy, jm, _ = jalali.to_jalali(today)
    first, last = jalali.jalali_month_range(jy, jm)

    upcoming_events = list(
        db.session.execute(queries.upcoming(atelier.id, today).limit(40)).scalars().unique()
    )
    event_boards = boards.load_boards(upcoming_events)
    alerts = staffing_alerts(event_boards, today)

    offers_page = db.session.execute(queries.incoming_offers([atelier.id], today).limit(4)).scalars().unique().all()
    offers_total = db.session.scalar(queries.incoming_offer_count([atelier.id], today))

    overdue = list(
        db.session.execute(
            queries.my_events(atelier.id)
            .outerjoin(Invoice, Invoice.event_id == Event.id)
            .where(
                Event.status == EventStatus.CONFIRMED,
                Event.date < today,
                # an event whose invoice is already submitted needs no reminder (the accounting module
                # does not have to move the event on to "done")
                sa.or_(Invoice.id.is_(None), Invoice.status.in_([InvoiceStatus.DRAFT, InvoiceStatus.VOID])),
            )
            .order_by(Event.date.desc())
            .limit(5)
        ).scalars().unique()
    )
    awaiting_invoice = list(
        db.session.execute(
            queries.my_events(atelier.id)
            .outerjoin(Invoice, Invoice.event_id == Event.id)
            .where(
                Event.status == EventStatus.DONE,
                sa.or_(Invoice.id.is_(None), Invoice.status == InvoiceStatus.DRAFT),
            )
            .order_by(Event.date.desc())
            .limit(5)
        ).scalars().unique()
    )

    return render_template(
        "ateliers/dashboard.html",
        atelier=atelier,
        ateliers=get_my_ateliers(),
        today=today,
        month_title=jalali.to_jalali_str(today, "month_year", persian_digits=True),
        kpis=month_kpis(atelier.id, first, last),
        upcoming=upcoming_events[:UPCOMING_SHOWN],
        boards=event_boards,
        alerts=alerts,
        offers=offers_page,
        offers_total=int(offers_total or 0),
        overdue=overdue,
        awaiting_invoice=awaiting_invoice,
        unread=notifications.recent(user, 5, unread_only=True),
        links={
            "invoice": links.accounting_invoice_url,
            "inventory": links.inventory_url(),
            "jobs": links.job_posts_url(),
        },
    )


@bp.post("/switch")
@role_required(MANAGER)
def switch():
    """Choose which of my ateliers the panel works on (404 for an atelier that is not mine)."""
    raw = request.form.get("atelier_id", "")
    try:
        atelier_id = int(raw)
    except ValueError:
        atelier_id = 0
    set_current_atelier(atelier_id)
    return redirect(url_for("ateliers.dashboard"))
