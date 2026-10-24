"""``staff`` - the personal panel of a staff member (نیرو): URL prefix ``/me``.

=======================================  ==================================================================
``/me``                                   dashboard: next afish, invitations, this month's pay, profile meter
``/me/invitations``                       the inbox: accept / decline with a double-booking warning, terms
``/me/assignments/<id>``                  one afish: terms + worked example, venue once committed, share, .ics
``/me/schedule``                          Jalali month calendar and agenda; ``/me/schedule.ics`` for all
``/me/availability``                      mark days I can / cannot work (read by staffing and the talent search)
``/me/earnings``                          pay per event from ``PayrollLine`` rows, totals, chart, CSV export
``/me/profile``, ``/me/portfolio``        skills, bio, equipment, listing switches, avatar, sample work
``/me/reviews``                           reviews I received
``/me/settings``                          notification preferences, password link, mobile number change (OTP)
``/me/applications``                      read-only list of my marketplace applications
=======================================  ==================================================================

Design notes (details in ``README.md`` next to this file):

* **Own data only.**  Every afish row is loaded with ``assignment_or_404(as_=("assignee",))`` and every list
  starts from ``Assignment.involving_user`` (directly or through a roster entry linked to the account); payroll lines
  are joined to the person's own afish rows.  Somebody else's id is a 404, never a 403.
* **No pay maths here.**  Terms are worded and illustrated with ``payroll.compute_pay``; earnings are the numbers the
  accounting module stored on the ``PayrollLine`` (a frozen calculation).  Accept / decline / withdraw go through
  ``staffing.transition``, so the audit trail, notifications and the event bus are the shared ones.
* **Privacy.**  Hall address, phone, map and the atelier contact appear only once the afish is committed
  (accepted / confirmed / completed); a calendar file never contains pay.
"""

from flask import Blueprint

from app.services import nav, security
from app.services.constants import Role

bp = Blueprint("staff", __name__, url_prefix="/me")


def _invitations_badge(user):
    """Invitations waiting for an answer (one COUNT per page render)."""
    from app.modules.staff import queries

    return queries.pending_invitation_count(user.id, security.today())


def _onboarded(user) -> bool:
    from app.modules.staff import profile

    return profile.onboarding_complete(user)


# order: 10 home, 20-60 daily work, 70-80 marketplace (its own module), 90+ settings (docs/ARCHITECTURE.md)
nav.register(Role.STAFF, "داشبورد", "staff.dashboard", icon="home", order=10)
nav.register(
    Role.STAFF,
    "دعوت‌ها",
    "staff.invitations",
    icon="mail",
    order=20,
    badge=_invitations_badge,
)
nav.register(
    Role.STAFF,
    "برنامه من",
    "staff.schedule",
    icon="calendar",
    order=30,
    active=("staff.assignment*", "staff.availability*"),
)
nav.register(Role.STAFF, "درآمد", "staff.earnings_page", icon="coins", order=40, active=("staff.earning*",))
nav.register(
    Role.STAFF,
    "پروفایل من",
    "staff.profile",
    icon="user",
    order=90,
    active=("staff.avatar*", "staff.portfolio*", "staff.reviews", "staff.settings", "staff.mobile*", "staff.notification_prefs", "staff.applications"),
)
nav.register_home(Role.STAFF, "staff.dashboard")
nav.register_onboarding(Role.STAFF, "staff.profile", is_complete=_onboarded)

# Attach the routes (keep these imports at the bottom: they import ``bp`` from this module).
from app.modules.staff import (  # noqa: E402,F401
    routes_account,
    routes_assignments,
    routes_availability,
    routes_earnings,
    routes_home,
    routes_profile,
)
