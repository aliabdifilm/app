"""``halls`` - the hall manager's panel (URL prefix ``/hall``).

======================================  ==============================================================
``/hall/``                              dashboard: upcoming events, pending offers and answers, occupancy
``/hall/setup``                         onboarding: create the first hall
``/hall/halls``                         my halls (a manager may own several): profile, photos, logistics
``/hall/calendar``, ``/hall/events``    Jalali month calendar and the list of events (filters, pagination)
``/hall/events/new``, ``.../bulk``      one event, or a whole season with the multi-date picker
``/hall/events/<id>``                   one event: offers, who accepted, outcome, rating, history
``/hall/offers``, ``.../offers/new``    give dates to ateliers (direct or open to all), withdraw, re-offer
``/hall/ateliers``                      searchable atelier directory (rating, city, verified badge)
``/hall/agreements``                    hall-atelier agreement records (period, exclusivity, rent)
``/hall/reports``                       events per month, acceptance rate, response time
======================================  ==============================================================

Rules live elsewhere and are not repeated here: handing a date to an atelier is
``app.services.event_offers`` (shared with the ``ateliers`` module), staffing and status changes
are ``app.services.staffing``, the module's own rules are ``rules.py``.  A hall never sees an
atelier's money: the outcome of an event is head-counts and a status.
"""

from flask import Blueprint

from app.extensions import db
from app.services import eventbus, nav, uploads
from app.services.constants import Role

bp = Blueprint("halls", __name__, url_prefix="/hall")


def _offers_badge(user):
    """Offers still waiting for an answer (one COUNT per page render)."""
    from app.modules.halls import queries
    from app.services import security

    return queries.pending_badge_for_manager(user.id, security.today())


# order: 10 home, 20-60 daily work, 90+ settings (docs/ARCHITECTURE.md)
nav.register(Role.HALL_MANAGER, "داشبورد", "halls.dashboard", icon="home", order=10)
nav.register(
    Role.HALL_MANAGER,
    "مراسم‌ها",
    "halls.calendar",
    icon="calendar",
    order=20,
    active=("halls.events", "halls.event_*", "halls.bulk*"),
)
nav.register(
    Role.HALL_MANAGER,
    "پیشنهادها",
    "halls.offers",
    icon="send",
    order=30,
    badge=_offers_badge,
    active=("halls.offer_*",),
)
nav.register(
    Role.HALL_MANAGER,
    "اتلیه‌ها",
    "halls.directory",
    icon="camera",
    order=40,
    active=("halls.atelier_*", "halls.agreement*"),
)
nav.register(Role.HALL_MANAGER, "گزارش‌ها", "halls.reports", icon="chart", order=50)
nav.register(
    Role.HALL_MANAGER, "تالارهای من", "halls.index", icon="building", order=90, active=("halls.hall_*", "halls.setup")
)
nav.register_home(Role.HALL_MANAGER, "halls.dashboard")
nav.register_onboarding(Role.HALL_MANAGER, "halls.setup", is_complete=lambda user: bool(user.halls))


def _can_see_hall_photo(relpath, user):
    """Hall pictures are shown to signed-in users, and only while the picture still belongs to a hall."""
    import sqlalchemy as sa

    from app.models import HallPhoto

    if not getattr(user, "is_authenticated", False):
        return False
    return db.session.execute(sa.select(HallPhoto.id).where(HallPhoto.image_path == relpath)).first() is not None


uploads.register_kind("hall", _can_see_hall_photo)

# Attach the routes (keep these imports at the bottom: they import ``bp`` from this module).
from app.modules.halls import (  # noqa: E402,F401
    agreements,
    bulk,
    dashboard,
    directory,
    events,
    offers,
    profiles,
    reports,
)
from app.modules.halls import common  # noqa: E402

bp.add_app_template_filter(common.hhmm, "hl_time")
bp.add_app_template_filter(common.input_time, "hl_input_time")
bp.add_app_template_filter(common.event_tone, "hl_event_tone")
bp.add_app_template_filter(common.offer_tone, "hl_offer_tone")
bp.add_app_template_global(common.state_of, "hl_state")


def on_event_status_changed(event_id, old_status, new_status, **_):
    """An atelier left an event (handed back or cancelled): its ``accepted`` offer becomes history.

    Without this the "one accepted offer per event" index would stop the next atelier from taking
    the event.  Runs inside the publisher's transaction, so it only flushes (never commits).
    """
    if old_status == "confirmed" and new_status in ("open", "draft", "cancelled"):
        from app.models import Event
        from app.services import event_offers

        event = db.session.get(Event, event_id)
        if event is not None:
            with db.session.begin_nested():
                event_offers.release_accepted(event)


def register(app):
    """Module hook: the bus subscription and the ``halls-agreement-reminders`` command."""
    eventbus.subscribe("event.status_changed", on_event_status_changed)
    from app.modules.halls import reminders

    reminders.register_cli(app)
