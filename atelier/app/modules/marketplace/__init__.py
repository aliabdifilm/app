"""``marketplace`` - the online match between staff and atelier managers (routes start with ``/jobs`` and ``/talent``).

=========================================  ======================================================================
``/jobs``                                  public job board: filter by city, role, Jalali date range, minimum pay;
                                           sort by date / pay / newest (anonymous visitors see no contact data)
``/jobs/<id>``                             one post: terms in plain Persian with a worked example, apply form
``/jobs/<id>/apply`` (POST)                staff apply with a message (blocked on a date conflict / missing skill)
``/jobs/applications``                     **my applications** (staff; the staff module links here), withdraw
``/jobs/invitations``                      direct invitations waiting for an answer (accept / decline)
``/jobs/manage``                           my posts (atelier manager): create, edit, close / reopen
``/jobs/new``, ``/jobs/<id>/edit``         the post form (prefilled from the afish pages and from role presets)
``/jobs/manage/inbox``, ``/jobs/<id>/applicants``    applicants; review one, accept (-> afish row) or reject
``/jobs/reviews``                          two-way reviews after a completed assignment
``/talent``, ``/talent/<id>``              talent directory (managers and admins; staff only if ``is_listed``)
``/talent/<id>/invite``                    invite a talent to one role of one event
=========================================  ======================================================================

Design notes (details in ``README.md`` next to this file):

* **Accept = afish.**  Accepting an application calls ``staffing.assign_person`` with the post's terms as
  the snapshot, ``source=marketplace`` and the usual double-booking check; the post becomes ``filled`` when
  its slots are taken and opens again when someone withdraws or the atelier cancels the afish row.
* **Privacy by design.**  A mobile number is shown only after an accepted application / committed
  assignment between the two parties; cards never fall back to the number as a name.
* **Pay maths is** ``app.services.payroll``; the module only words it («اگر ۸۰ عکس فروخته شود: ...»).
* The pay terms, role, date and linked event of a post are frozen while applications exist, so the
  snapshot an applicant gets is exactly what they saw.
"""

from flask import Blueprint

from app.services import eventbus, nav, security
from app.services.constants import Role

# No url_prefix: the explicit routes below live under /jobs and /talent.
bp = Blueprint("marketplace", __name__)


def _applicants_badge(user):
    """Pending applications on my posts (one COUNT per page render)."""
    from app.modules.marketplace import queries

    return queries.pending_applicant_count(user.id)


def _invitations_badge(user):
    """Invitations waiting for my answer."""
    from app.modules.marketplace import queries

    return queries.pending_invitation_count(user.id, security.today())


# order: 70-80 marketplace (docs/ARCHITECTURE.md)
nav.register(Role.STAFF, "بازار کار", "marketplace.jobs", icon="briefcase", order=70, active=("marketplace.job_detail",))
nav.register(
    Role.STAFF,
    "درخواست‌ها",
    "marketplace.my_applications",
    icon="inbox",
    order=72,
    badge=_invitations_badge,
    active=("marketplace.my_invitations", "marketplace.application_*", "marketplace.invitation_*", "marketplace.review*"),
)
nav.register(
    Role.ATELIER_MANAGER,
    "بازار کار",
    "marketplace.manage_jobs",
    icon="briefcase",
    order=70,
    badge=_applicants_badge,
    active=("marketplace.job_*", "marketplace.inbox", "marketplace.manage_application", "marketplace.application_*", "marketplace.review*"),
)
nav.register(
    (Role.ATELIER_MANAGER, Role.HALL_MANAGER),
    "جستجوی نیرو",
    "marketplace.talent_index",
    icon="search",
    order=75,
    active=("marketplace.talent_*",),
)

# Attach the routes (keep these imports at the bottom: they import ``bp`` from this module).
from app.modules.marketplace import (  # noqa: E402,F401
    routes_board,
    routes_manage,
    routes_reviews,
    routes_staff,
    routes_talent,
)
from app.modules.marketplace import common  # noqa: E402

bp.add_app_template_filter(common.person_name, "mk_name")
bp.add_app_template_filter(common.terms_text, "mk_terms")
bp.add_app_template_filter(common.terms_lines, "mk_terms_lines")
bp.add_app_template_filter(common.clip, "mk_clip")
bp.add_app_template_filter(common.application_tone, "mk_application_tone")
bp.add_app_template_filter(common.post_tone, "mk_post_tone")


def register(app):
    """Module hook: keep applications and the fill count true when an afish row is cancelled elsewhere."""
    from app.modules.marketplace import services

    eventbus.subscribe("assignment.changed", services.on_assignment_changed)
