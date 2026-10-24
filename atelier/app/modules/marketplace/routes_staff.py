"""The staff member's side: my applications (the staff module links here), withdraw, invitations."""

from __future__ import annotations

from flask import abort, render_template, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.modules.marketplace import bp, cards, common, queries, services, web
from app.services import security, staffing
from app.services.constants import COMMITTED_ASSIGNMENT_STATUSES, Role
from app.services.http import paginate
from app.services.permissions import assignment_or_404, get_user, job_application_or_404, role_required

STAFF = Role.STAFF
PER_PAGE = 10


@bp.get("/jobs/applications")
@role_required(STAFF)
def my_applications():
    """My applications with their status; the atelier's contact appears once one is accepted."""
    user = get_user()
    tab = request.args.get("tab", "")
    if tab not in queries.MY_TABS:
        tab = "all"
    page = paginate(queries.my_applications_select(user.id, tab), per_page=PER_PAGE)
    by_post = {card.post.id: card for card in cards.build_cards([a.post for a in page.items], user)}
    return render_template(
        "marketplace/mine.html",
        section="applications",
        page=page,
        rows=[(application, by_post[application.post_id]) for application in page.items],
        tab=tab,
        counts=queries.my_application_counts(user.id),
        invitation_count=queries.pending_invitation_count(user.id, security.today()),
        today=security.today(),
    )


@bp.post("/jobs/applications/<int:application_id>/withdraw")
@role_required(STAFF)
def application_withdraw(application_id: int):
    """Step back from an application (an accepted one also cancels the afish row)."""
    application = job_application_or_404(application_id, as_=("applicant",))
    try:
        services.withdraw_application(application, get_user())
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.my_applications")
    return web.done("درخواست شما پس گرفته شد.", "marketplace.my_applications")


@bp.get("/jobs/invitations")
@role_required(STAFF)
def my_invitations():
    """Direct invitations waiting for my answer."""
    user = get_user()
    today = security.today()
    page = paginate(queries.invitations_select(user.id, today), per_page=PER_PAGE)
    rows = []
    for assignment in page.items:
        terms = assignment.terms_dict()
        rows.append(
            {
                "assignment": assignment,
                "event": assignment.event,
                "atelier": assignment.event.atelier,
                "terms": terms,
                "lines": common.terms_lines(terms),
                "example": common.pay_example(terms),
                "busy": bool(
                    staffing.conflicts_for(
                        assignment.event.date,
                        user_id=user.id,
                        exclude_assignment_id=assignment.id,
                        statuses=COMMITTED_ASSIGNMENT_STATUSES,
                    )
                ),
            }
        )
    return render_template(
        "marketplace/mine.html",
        section="invitations",
        page=page,
        rows=rows,
        tab="",
        counts=queries.my_application_counts(user.id),
        invitation_count=page.total or 0,
        today=today,
    )


def _invitation_or_404(assignment_id: int):
    assignment = assignment_or_404(assignment_id, as_=("assignee",))
    user = get_user()
    if assignment.user_id != user.id:  # an invitation is always addressed to an account, never a roster entry
        abort(404)
    return assignment


@bp.post("/jobs/invitations/<int:assignment_id>/accept")
@role_required(STAFF)
def invitation_accept(assignment_id: int):
    assignment = _invitation_or_404(assignment_id)
    try:
        services.respond_to_invitation(assignment, get_user(), True)
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.my_invitations")
    except IntegrityError:
        return web.fail("این دعوت هم‌زمان تغییر کرد؛ صفحه را تازه کنید.", "marketplace.my_invitations")
    return web.done("دعوت را پذیرفتید؛ اتلیه باخبر شد.", "marketplace.my_invitations")


@bp.post("/jobs/invitations/<int:assignment_id>/decline")
@role_required(STAFF)
def invitation_decline(assignment_id: int):
    assignment = _invitation_or_404(assignment_id)
    try:
        services.respond_to_invitation(assignment, get_user(), False)
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.my_invitations")
    return web.done("دعوت را رد کردید.", "marketplace.my_invitations")
