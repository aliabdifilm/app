"""``ateliers`` - the atelier manager's panel (URL prefix ``/atelier``).

====================================  ===================================================================
``/atelier/``                          dashboard: upcoming events, missing staff, offers, month KPIs
``/atelier/setup``                     onboarding: create the atelier
``/atelier/settings``                  profile + logo, rent per hall, default pay presets
``/atelier/roster``                    fixed staff (نیروهای ثابت): list, create, edit, activate
``/atelier/events``                    offers / open events / upcoming / past, accept - decline - claim
``/atelier/calendar``                  Jalali month calendar of the atelier's events
``/atelier/events/<id>``               one event: staffing, equipment, invoice status
``/atelier/afish``                     the staffing board (افیش) of all upcoming events
``/atelier/events/<id>/afish/...``     pick a person, assign with a terms snapshot, confirm, copy
====================================  ===================================================================

The module owns no money maths and no staffing rules: pay is ``app.services.payroll``, assigning and
status changes are ``app.services.staffing``.  Invoices belong to ``accounting``, job posts to
``marketplace`` and stock to ``inventory``; this panel only links to them (``links.py``).

``flask --app wsgi ateliers-reminders`` sends the idempotent call-time reminders (``reminders.py``).
"""

from flask import Blueprint

from app.services import nav
from app.services.constants import Role

bp = Blueprint("ateliers", __name__, url_prefix="/atelier")


def _offers_badge(user):
    """Number of direct offers waiting for an answer (one COUNT per page render)."""
    from app.extensions import db
    from app.modules.ateliers import queries
    from app.services import security

    return db.session.scalar(queries.incoming_offer_count_for_manager(user.id, security.today())) or 0


# order: 10 home, 20-60 daily work, 90+ settings (docs/ARCHITECTURE.md)
nav.register(Role.ATELIER_MANAGER, "داشبورد", "ateliers.dashboard", icon="home", order=10)
nav.register(
    Role.ATELIER_MANAGER, "افیش نیرو", "ateliers.afish_board", icon="users", order=20, active=("ateliers.afish_*",)
)
nav.register(
    Role.ATELIER_MANAGER,
    "مراسم‌ها",
    "ateliers.events",
    icon="calendar",
    order=25,
    badge=_offers_badge,
    active=("ateliers.event_*", "ateliers.offer_*", "ateliers.calendar"),
)
nav.register(
    Role.ATELIER_MANAGER, "نیروهای ثابت", "ateliers.roster", icon="user-check", order=30, active=("ateliers.roster_*",)
)
nav.register(Role.ATELIER_MANAGER, "تنظیمات اتلیه", "ateliers.settings", icon="settings", order=90)
nav.register_home(Role.ATELIER_MANAGER, "ateliers.dashboard")
nav.register_onboarding(
    Role.ATELIER_MANAGER,
    "ateliers.setup",
    is_complete=lambda user: bool(user.ateliers),
)

# Attach the routes (keep these imports at the bottom: they import ``bp`` from this module).
from app.modules.ateliers import afish, common, dashboard, events, roster, settings  # noqa: E402,F401

bp.add_app_template_filter(common.terms_text, "terms_text")
bp.add_app_template_filter(common.hhmm, "hhmm")
bp.add_app_template_filter(common.input_time, "input_time")
bp.add_app_template_filter(common.assignment_tone, "assignment_tone")
bp.add_app_template_filter(common.event_tone, "event_tone")


def register(app):
    """Module hook: the ``ateliers-reminders`` CLI command."""
    from app.modules.ateliers import reminders

    reminders.register_cli(app)
