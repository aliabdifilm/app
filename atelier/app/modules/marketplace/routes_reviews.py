"""Two-way reviews after a completed assignment: the atelier reviews the person, the person the atelier.

One review per author per assignment (database unique constraint), 1-5 stars and an optional comment.
Reviews hidden by an admin (``is_hidden``) are excluded from every list and average.
"""

from __future__ import annotations

import sqlalchemy as sa
from flask import abort, render_template, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Review
from app.modules.marketplace import bp, forms, queries, services, web
from app.services.constants import AssignmentStatus, Role
from app.services.forms import Form
from app.services.http import json_body, json_error, paginate, wants_json
from app.services.permissions import assignment_or_404, get_user, owned_or_404, role_required

PARTIES = (Role.ATELIER_MANAGER, Role.STAFF)


@bp.get("/jobs/reviews")
@role_required(*PARTIES)
def reviews():
    """Reviews I still owe, the ones I wrote and the ones I received."""
    user = get_user()
    tab = request.args.get("tab", "")
    if tab not in queries.REVIEW_TABS:
        tab = "pending"
    pending = queries.pending_reviews_select(user)
    stmt = {
        "pending": pending,
        "written": queries.reviews_by_user(user.id),
        "received": queries.reviews_about_user(user.id),
    }[tab]
    page = paginate(stmt, per_page=10)
    return render_template(
        "marketplace/reviews.html",
        page=page,
        tab=tab,
        pending_count=page.total if tab == "pending" else queries.count_of(pending),
        user_role=user.role,
        report_reasons=services.REPORT_REASONS,
    )


def _render_review_form(assignment, context, *, existing, values, errors, status=200, alert=None):
    return (
        render_template(
            "marketplace/review_form.html",
            assignment=assignment,
            event=assignment.event,
            context=context,
            existing=existing,
            completed=assignment.status == AssignmentStatus.COMPLETED,
            problem=alert,
            form=values,
            errors=errors,
        ),
        status,
    )


@bp.route("/jobs/reviews/new/<int:assignment_id>", methods=["GET", "POST"])
@role_required(*PARTIES)
def review_new(assignment_id: int):
    assignment = assignment_or_404(assignment_id, as_=("atelier", "assignee"))
    user = get_user()
    context = services.review_context(assignment, user)
    if context is None:
        abort(404)
    existing = db.session.scalar(
        sa.select(Review).where(Review.assignment_id == assignment.id, Review.author_id == user.id)
    )
    if request.method == "GET" or existing is not None or assignment.status != AssignmentStatus.COMPLETED:
        status = 200 if request.method == "GET" else 409
        return _render_review_form(assignment, context, existing=existing, values={}, errors={}, status=status)

    form = Form(request.form)
    data = forms.read_review(form)
    if data is None:
        if wants_json():
            return json_error(next(iter(form.errors.values())), 422, "validation_error", fields=form.errors)
        return _render_review_form(assignment, context, existing=None, values=form.values, errors=form.errors, status=422)
    try:
        services.create_review(assignment, user, data.rating, data.comment)
        db.session.commit()
    except services.MarketplaceError as exc:
        db.session.rollback()
        if wants_json():
            return json_error(str(exc), exc.status, exc.code)
        return _render_review_form(assignment, context, existing=None, values=form.values, errors={}, status=exc.status, alert=str(exc))
    except IntegrityError:  # two submits at once: the unique (assignment, author) pair held
        return web.fail("برای این همکاری قبلاً نظر داده‌اید.", "marketplace.reviews", tab="written")
    return web.done("نظر شما ثبت شد؛ سپاس.", "marketplace.reviews", tab="written")


@bp.post("/jobs/reviews/<int:review_id>/report")
@role_required(*PARTIES)
def review_report(review_id: int):
    """The person a review is about may report it (unfair, abusive ...); an admin decides whether to hide it."""
    user = get_user()
    review = owned_or_404(Review, review_id, lambda row, me: row.subject_id == me.id and not row.is_hidden)
    form = Form(json_body() if request.is_json else request.form)
    reason = form.choice("reason", "دلیل گزارش", [key for key, _ in services.REPORT_REASONS], required=True)
    details = form.text("details", "توضیح", max_len=300, multiline=True)
    if not form.ok:
        return web.fail(next(iter(form.errors.values())), "marketplace.reviews", 422, "validation_error", tab="received")
    try:
        services.report_entity(user, "review.reported", review, reason, details, None)
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.reviews", tab="received")
    return web.done("گزارش شما ثبت شد؛ مدیر سایت آن را بررسی می‌کند. سپاس.", "marketplace.reviews", tab="received")
