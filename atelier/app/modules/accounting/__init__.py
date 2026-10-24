"""``accounting`` - event cards, invoices, payroll and reports (AGENTS.md "بخش مالی").

======================================  ===============================================================
``/accounting``                         event cards of the month (filters, month summary, invoice accordion)
``/accounting/events/<id>/invoice``     the invoice popup (GET: form / fragment, POST: save)
``.../invoice/preview``                 live numbers as JSON (the server computes; JavaScript never does)
``/accounting/invoices/<id>/print``     A4 invoice sheet;  ``.../export.csv``: Excel-safe CSV
``.../reopen`` ``.../void``             lifecycle actions with a mandatory reason (POST, audited)
``/accounting/payroll``                 monthly payroll per person, mark paid (single / bulk), CSV
``/accounting/reports``                 profit trend, per-event / per-hall profitability, staff leaderboard
======================================  ===============================================================

Only atelier managers enter, and only for events / invoices of their own ateliers (404 otherwise).  All
arithmetic lives in :mod:`app.services.ledger` and :mod:`app.services.payroll`.
"""

from flask import Blueprint

from app.services import nav
from app.services.constants import Role

bp = Blueprint("accounting", __name__, url_prefix="/accounting")


def _awaiting_badge(user):
    """How many past events still need an invoice (a cheap count for the menu badge)."""
    import sqlalchemy as sa

    from app.extensions import db
    from app.models import Event, Invoice
    from app.services import security
    from app.services.constants import EventStatus
    from app.services.permissions import my_atelier_ids

    ids = my_atelier_ids(user)
    if not ids:
        return 0
    return db.session.scalar(
        sa.select(sa.func.count(Event.id)).where(
            Event.atelier_id.in_(sorted(ids)),
            Event.status.in_([EventStatus.CONFIRMED, EventStatus.DONE]),
            Event.date <= security.today(),
            ~sa.exists().where(Invoice.event_id == Event.id),
        )
    )


nav.register(
    Role.ATELIER_MANAGER,
    "حسابداری",
    "accounting.index",
    icon="coins",
    order=50,
    badge=_awaiting_badge,
    active=("accounting.*",),
)

from app.modules.accounting import routes  # noqa: E402,F401  (attaches the routes to bp)


def register(app):
    """Template filters for printed sheets: ``{{ amount|words }}`` and ``{{ amount|toman_words }}``."""
    from app.modules.accounting.words import to_words, toman_in_words

    app.add_template_filter(to_words, "words")
    app.add_template_filter(toman_in_words, "toman_words")
