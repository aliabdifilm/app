"""The public job board (``/jobs``), a post's page and the apply action.

Anonymous visitors may read everything here but never see a mobile number; applying needs a staff
account (visitors are sent to the login page and come back).
"""

from __future__ import annotations

from typing import Any, Optional

import sqlalchemy as sa
from flask import abort, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import JobApplication, JobPost
from app.modules.marketplace import bp, cards, common, forms, queries, services, web
from app.services import security
from app.services.constants import ApplicationStatus, JobPostStatus, Role
from app.services.forms import Form
from app.services.http import json_body, json_error, paginate, wants_json
from app.services.permissions import get_user, login_required, role_required

BOARD_PER_PAGE = 12


@bp.get("/jobs")
def jobs():
    """Open calls for staff: filter by city, role, Jalali date range and minimum pay; sort by date or pay."""
    today = security.today()
    filters = forms.read_board_filters(request.args)
    stmt = queries.board_select(
        today,
        city=filters.city or None,
        role=filters.role,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
    if filters.min_pay or filters.sort == "pay":
        # Pay is computed by payroll.compute_pay (bonus tiers live in JSON), so these two options are
        # evaluated on the candidate rows in Python and paginated as a list.
        candidates = list(
            db.session.scalars(
                stmt.order_by(JobPost.date, JobPost.id).limit(common.PAY_SORT_CANDIDATES)
            ).unique()
        )
        scored = [(common.reference_total(post.terms_dict()), post) for post in candidates]
        if filters.min_pay:
            scored = [item for item in scored if item[0] >= filters.min_pay]
        if filters.sort == "pay":
            scored.sort(key=lambda item: (-item[0], item[1].date, item[1].id))
        page = common.paginate_list([post for _, post in scored], per_page=BOARD_PER_PAGE)
    else:
        order = JobPost.created_at.desc() if filters.sort == "new" else JobPost.date.asc()
        page = paginate(stmt.order_by(order, JobPost.id.desc() if filters.sort == "new" else JobPost.id), per_page=BOARD_PER_PAGE)
    viewer = get_user()
    return render_template(
        "marketplace/jobs.html",
        page=page,
        cards=cards.build_cards(page.items, viewer),
        filters=filters,
        cities=queries.board_cities(today),
        viewer=viewer,
    )


def _applied(post: JobPost, user: Any) -> Optional[JobApplication]:
    if user is None or user.role != Role.STAFF:
        return None
    return db.session.scalar(
        sa.select(JobApplication).where(JobApplication.post_id == post.id, JobApplication.user_id == user.id)
    )


def render_detail(post: JobPost, *, form: Optional[dict] = None, errors: Optional[dict] = None,
                  apply_error: Optional[str] = None, status: int = 200):
    viewer = get_user()
    is_owner = viewer is not None and post.atelier.manager_id == viewer.id
    card = cards.build_cards([post], viewer, with_applications=is_owner)[0]
    application = _applied(post, viewer)
    blocker: Optional[str] = None
    can_try = application is None or application.status == ApplicationStatus.WITHDRAWN
    if apply_error is None and viewer is not None and viewer.role == Role.STAFF and can_try and post.status == JobPostStatus.OPEN:
        try:
            services.check_can_apply(post, viewer)
        except services.MarketplaceError as exc:
            blocker = str(exc)
    needs_skill = bool(
        viewer is not None
        and viewer.role == Role.STAFF
        and (viewer.staff_profile is None or not viewer.staff_profile.has_skill(post.role))
    )
    return (
        render_template(
            "marketplace/job_detail.html",
            job=card,
            post=post,
            application=application,
            apply_blocker=blocker,
            apply_error=apply_error,
            needs_skill=needs_skill,
            profile_url=web.staff_profile_url() if needs_skill else None,
            is_owner=is_owner,
            report_reasons=services.REPORT_REASONS,
            scenarios=common.pay_scenarios(card.terms),
            viewer=viewer,
            login_next=url_for("auth.login", next=request.path) if viewer is None else None,
            form=form or {},
            errors=errors or {},
        ),
        status,
    )


@bp.get("/jobs/<int:post_id>")
def job_detail(post_id: int):
    post = queries.get_visible_post(post_id, get_user())
    if post is None:
        abort(404)
    return render_detail(post)


@bp.post("/jobs/<int:post_id>/apply")
@role_required(Role.STAFF)
def job_apply(post_id: int):
    user = get_user()
    post = queries.get_visible_post(post_id, user)
    if post is None:
        abort(404)
    form = Form(json_body() if request.is_json else request.form)
    message = forms.read_message(form, "message", "پیام شما")
    if not form.ok:
        if wants_json():
            return json_error(next(iter(form.errors.values())), 422, "validation_error", fields=form.errors)
        return render_detail(post, form=form.values, errors=form.errors, status=422)
    try:
        services.apply_to_post(post, user, message)
        db.session.commit()
    except services.MarketplaceError as exc:
        db.session.rollback()
        if wants_json():
            return json_error(str(exc), exc.status, exc.code)
        return render_detail(post, form=form.values, apply_error=str(exc), status=exc.status)
    except IntegrityError:  # the same person applied twice at once (unique post/user)
        db.session.rollback()
        return web.fail("درخواست شما همین حالا ثبت شد.", "marketplace.job_detail", post_id=post_id)
    return web.done(
        "درخواست شما ارسال شد؛ نتیجه را در «درخواست‌های من» دنبال کنید و پس از پذیرفته‌شدن به شما اعلان می‌دهیم.",
        "marketplace.my_applications",
    )


@bp.post("/jobs/<int:post_id>/report")
@login_required
def job_report(post_id: int):
    """Report a post (fake call, asking for money, abuse ...); an admin decides whether to hide it."""
    user = get_user()
    post = queries.get_visible_post(post_id, user)
    if post is None or post.atelier.manager_id == user.id:
        abort(404)
    form = Form(json_body() if request.is_json else request.form)
    reason = form.choice("reason", "دلیل گزارش", [key for key, _ in services.REPORT_REASONS], required=True)
    details = form.text("details", "توضیح", max_len=300, multiline=True)
    if not form.ok:
        return web.fail(next(iter(form.errors.values())), "marketplace.job_detail", 422, "validation_error", post_id=post_id)
    try:
        services.report_entity(user, "job_post.reported", post, reason, details, url_for("marketplace.job_detail", post_id=post.id))
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.job_detail", post_id=post_id)
    return web.done("گزارش شما ثبت شد؛ مدیر سایت آن را بررسی می‌کند. سپاس.", "marketplace.job_detail", post_id=post_id)
