"""The atelier manager's side: posts, the applicants inbox, accept / reject, close / reopen.

Every object is loaded through an ownership helper (404 for anybody else's), lists are scoped with
the manager's atelier ids in SQL, and nothing trusts an id from the body: an event picked in a form is
re-checked against the post's atelier by the service.
"""

from __future__ import annotations

from typing import Any, Optional

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Assignment, Event, JobPost
from app.modules.marketplace import bp, cards, common, forms, queries, services, web
from app.services import jalali, payroll, security, staffing
from app.services.constants import ApplicationStatus, Role, Skill
from app.services.forms import Form
from app.services.http import json_body, json_error, json_ok, paginate
from app.services.money import format_toman, to_persian_digits
from app.services.permissions import (
    get_my_atelier,
    get_my_ateliers,
    get_user,
    job_application_or_404,
    job_post_or_404,
    my_atelier_ids,
    role_required,
)

MANAGER = Role.ATELIER_MANAGER
PER_PAGE = 10


def _event_options(events: list[Event]) -> list[tuple[str, str]]:
    return [
        (
            str(event.id),
            f"{event.hall.name if event.hall else 'مراسم'} · "
            f"{jalali.to_jalali_str(event.date, 'weekday_long', persian_digits=True)}",
        )
        for event in events
    ]


# --------------------------------------------------------------------------
# lists
# --------------------------------------------------------------------------


@bp.get("/jobs/manage")
@role_required(MANAGER)
def manage_jobs():
    """My posts (every atelier of mine), tabbed by state, with applicant counts."""
    get_my_atelier()  # no atelier yet -> the onboarding page
    user = get_user()
    today = security.today()
    tab = request.args.get("tab", "")
    if tab not in queries.MANAGE_TABS:
        tab = "open"
    ids = sorted(my_atelier_ids())
    page = paginate(queries.manage_select(ids, tab, today), per_page=PER_PAGE)
    return render_template(
        "marketplace/manage.html",
        page=page,
        cards=cards.build_cards(page.items, user, with_applications=True),
        tab=tab,
        counts=queries.manage_tab_counts(ids, today),
        pending_total=queries.pending_applicant_count(user.id),
        multi=len(ids) > 1,
    )


def _render_inbox(post: Optional[JobPost]):
    get_my_atelier()
    ids = sorted(my_atelier_ids())
    tab = request.args.get("tab", "")
    if tab not in queries.INBOX_TABS:
        tab = "pending"
    post_id = post.id if post is not None else None
    page = paginate(queries.inbox_select(ids, tab, post_id), per_page=15)
    counts = queries.inbox_counts(ids, post_id)
    card = cards.build_cards([post], get_user(), with_applications=True)[0] if post is not None else None
    return render_template(
        "marketplace/inbox.html",
        page=page,
        tab=tab,
        counts=counts,
        post=post,
        job=card,
    )


@bp.get("/jobs/manage/inbox")
@role_required(MANAGER)
def inbox():
    """Applicants of all my posts."""
    return _render_inbox(None)


@bp.get("/jobs/<int:post_id>/applicants")
@role_required(MANAGER)
def job_applicants(post_id: int):
    """Applicants of one post."""
    return _render_inbox(job_post_or_404(post_id))


# --------------------------------------------------------------------------
# create / edit
# --------------------------------------------------------------------------


def _presets(ateliers) -> dict[str, dict[str, Any]]:
    """``{atelier_id: {role: terms}}`` for the form's "fill from my preset" behaviour."""
    return {str(atelier.id): dict(atelier.default_pay_presets or {}) for atelier in ateliers}


def _render_form(post: Optional[JobPost], ateliers, *, values: dict, errors: dict, status: int = 200,
                 locked: bool = False, accepted: int = 0, alert: Optional[str] = None):
    today = security.today()
    events = queries.upcoming_events([a.id for a in ateliers], today)
    if post is not None and post.event is not None and post.event not in events:
        events = [post.event, *events]  # keep the linked event selectable even when it is no longer upcoming
    return (
        render_template(
            "marketplace/job_form.html",
            post=post,
            ateliers=ateliers,
            atelier_options=[(str(atelier.id), atelier.name) for atelier in ateliers],
            event_options=_event_options(events),
            presets=_presets(ateliers),
            locked=locked,
            accepted=accepted,
            problem=alert,
            today=today,
            form=values,
            errors=errors,
        ),
        status,
    )


def _prefill(ateliers, args) -> dict[str, Any]:
    """Form values for ``GET /jobs/new``: the query-string contract of the atelier panel
    (``event, role, date, city, title, slots, base, commission, tiers``), then the atelier preset."""
    today = security.today()
    atelier = ateliers[0]
    values: dict[str, Any] = {"atelier_id": str(atelier.id), "slots": 1}
    event: Optional[Event] = None
    raw_event = (args.get("event") or "").strip()
    if raw_event.isdigit() and len(raw_event) <= 12:
        candidate = db.session.get(Event, int(raw_event))
        if candidate is not None and candidate.atelier_id in {a.id for a in ateliers} and candidate.date >= today:
            event = candidate
            atelier = next(a for a in ateliers if a.id == candidate.atelier_id)
            values["atelier_id"] = str(atelier.id)
            values["event"] = str(event.id)
    form = Form({key: args.get(key, "") for key in ("role", "date", "city", "title", "slots", "base", "commission")})
    role = form.enum("role", "نقش", Skill)
    day = form.date("date", "تاریخ")
    form.integer("slots", "تعداد", min=1, max=forms.MAX_SLOTS)
    form.text("title", "عنوان", max_len=150)
    form.text("city", "شهر", max_len=80)
    terms_given = False
    base = form.money("base", "پایه")
    commission = form.money("commission", "پورسانت")
    tiers = common.parse_tiers_query(args.get("tiers"))
    if base is not None or commission is not None or tiers:
        terms_given = True
    clean = {key: value for key, value in form.values.items() if key not in form.errors}
    values.update({key: clean[key] for key in ("title", "city") if clean.get(key)})
    if role is not None:
        values["role"] = role.value
    if day is not None and day >= today:
        values["date"] = day.isoformat()
    if event is not None:
        values["date"] = event.date.isoformat()
        if not values.get("city") and event.hall is not None:
            values["city"] = event.hall.city
    if "slots" in clean and clean["slots"]:
        values["slots"] = clean["slots"]
    if terms_given:
        values.update(forms.terms_to_form({"base": base or 0, "commission_per_unit": commission or 0, "bonus_tiers": tiers}))
    elif role is not None and atelier.preset_for(role):
        values.update(forms.terms_to_form(atelier.preset_for(role)))
    else:
        values.update(forms.terms_to_form(None, blank_zero=True))
    values.setdefault("city", atelier.city)
    return values


@bp.route("/jobs/new", methods=["GET", "POST"])
@role_required(MANAGER)
def job_new():
    get_my_atelier()  # onboarding first
    ateliers = get_my_ateliers(active_only=True)
    if not ateliers:
        flash("اتلیهٔ شما غیرفعال است؛ برای ثبت آگهی ابتدا آن را در تنظیمات اتلیه فعال کنید.", "error")
        return redirect(url_for("marketplace.manage_jobs"))
    user = get_user()
    if request.method == "GET":
        return _render_form(None, ateliers, values=_prefill(ateliers, request.args), errors={})

    form = Form(request.form)
    data = forms.read_job(form, ateliers=ateliers, today=security.today())
    if data is None:
        return _render_form(None, ateliers, values=form.values, errors=form.errors, status=422)
    try:
        post = services.create_post(user, data)
        db.session.commit()
    except services.MarketplaceError as exc:
        db.session.rollback()
        return _render_form(None, ateliers, values=form.values, errors={}, status=exc.status, alert=str(exc))
    return web.done("آگهی منتشر شد؛ نیروها همین حالا آن را در بازار کار می‌بینند.", "marketplace.job_applicants", post_id=post.id)


@bp.route("/jobs/<int:post_id>/edit", methods=["GET", "POST"])
@role_required(MANAGER)
def job_edit(post_id: int):
    post = job_post_or_404(post_id)
    user = get_user()
    ateliers = get_my_ateliers()
    locked = services.post_is_locked(post)
    accepted = queries.count_accepted(post.id)
    if request.method == "GET":
        values = {
            "atelier_id": str(post.atelier_id),
            "title": post.title,
            "description": post.description or "",
            "role": post.role.value,
            "date": post.date.isoformat(),
            "city": post.city,
            "slots": post.slots,
            "event": post.event_id or "",
            **forms.terms_to_form(post.terms_dict()),
        }
        return _render_form(post, ateliers, values=values, errors={}, locked=locked, accepted=accepted)

    form = Form(request.form)
    data = forms.read_job(form, ateliers=ateliers, today=security.today(), post=post, locked=locked, accepted=accepted)
    if data is None:
        return _render_form(post, ateliers, values=form.values, errors=form.errors, status=422, locked=locked, accepted=accepted)
    try:
        services.update_post(post, data, user)
        db.session.commit()
    except services.MarketplaceError as exc:
        db.session.rollback()
        return _render_form(post, ateliers, values=form.values, errors={}, status=exc.status, locked=locked, accepted=accepted, alert=str(exc))
    return web.done("آگهی ذخیره شد.", "marketplace.manage_jobs")


@bp.post("/jobs/<int:post_id>/close")
@role_required(MANAGER)
def job_close(post_id: int):
    """Close a post; by default the applicants still waiting are rejected with a notification (bulk close)."""
    post = job_post_or_404(post_id)
    reject_pending = Form(request.form).boolean("reject_pending")
    try:
        rejected = services.close_post(post, get_user(), reject_pending=reject_pending)
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.manage_jobs")
    message = "آگهی بسته شد."
    if rejected:
        message += f" {to_persian_digits(rejected)} درخواست در انتظار رد شد و به متقاضیان اطلاع داده شد."
    return web.done(message, "marketplace.manage_jobs", tab="closed")


@bp.post("/jobs/<int:post_id>/reopen")
@role_required(MANAGER)
def job_reopen(post_id: int):
    post = job_post_or_404(post_id)
    try:
        services.reopen_post(post, get_user())
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.manage_jobs", tab="closed")
    return web.done("آگهی دوباره باز شد.", "marketplace.manage_jobs")


# --------------------------------------------------------------------------
# one application
# --------------------------------------------------------------------------


@bp.get("/jobs/manage/applications/<int:application_id>")
@role_required(MANAGER)
def manage_application(application_id: int):
    """Review one applicant: profile summary, the offered terms with a worked example, accept / reject."""
    application = job_application_or_404(application_id, as_=("atelier",))
    post = application.post
    applicant = application.user
    profile = applicant.staff_profile
    if post.event is not None:
        candidates = [post.event] if (post.event.atelier_id == post.atelier_id and post.event.date == post.date) else []
    else:
        candidates = queries.candidate_events(post.atelier_id, post.date)
    busy = bool(staffing.conflicts_for(post.date, user_id=applicant.id))
    unavailable = staffing.is_unavailable([applicant.id], post.date) is not None
    card = cards.build_cards([post], get_user(), with_applications=True)[0]
    accepted = ApplicationStatus.coerce(application.status) == ApplicationStatus.ACCEPTED
    assignment = None
    if accepted:
        assignment = db.session.scalar(
            sa.select(Assignment).where(Assignment.job_application_id == application.id).order_by(Assignment.id.desc()).limit(1)
        )
    return render_template(
        "marketplace/application.html",
        application=application,
        post=post,
        job=card,
        applicant=applicant,
        profile=profile,
        rating=queries.Rating(profile.rating_avg, profile.rating_count) if profile is not None else queries.NO_RATING,
        candidates=candidates,
        event_options=_event_options(candidates),
        busy=busy,
        unavailable=unavailable,
        reveal_contact=accepted,
        afish_url=web.optional_url("ateliers.event_detail", event_id=assignment.event_id) if assignment else None,
        events_url=web.optional_url("ateliers.events"),
        scenarios=common.pay_scenarios(card.terms),
        form={},
        errors={},
    )


@bp.post("/jobs/manage/applications/<int:application_id>/accept")
@role_required(MANAGER)
def application_accept(application_id: int):
    """Accept: the afish row is created through ``staffing.assign_person`` with the post's terms as snapshot."""
    application = job_application_or_404(application_id, as_=("atelier",))
    post = application.post
    form = Form(request.form)
    chosen = form.integer("event", "مراسم", min=1)
    event: Optional[Event] = None
    if post.event_id is not None:
        event = post.event  # a linked post always staffs its own event; the form value is ignored
    elif chosen is not None and "event" not in form.errors:
        event = db.session.get(Event, chosen)
    else:
        candidates = queries.candidate_events(post.atelier_id, post.date)
        if len(candidates) == 1:
            event = candidates[0]
    try:
        assignment = services.accept_application(application, event, get_user())
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.manage_application", application_id=application_id)
    except IntegrityError:  # two requests staffed the same person at once (partial unique index)
        return web.fail("این نفر همین حالا برای این نقش افیش شد.", "marketplace.manage_application", application_id=application_id)
    return web.done(
        f"«{assignment.person_name}» به‌عنوان {assignment.role.label} پذیرفته و افیش شد.",
        "marketplace.job_applicants",
        data={"assignment_id": assignment.id},
        post_id=post.id,
    )


@bp.post("/jobs/manage/applications/<int:application_id>/reject")
@role_required(MANAGER)
def application_reject(application_id: int):
    application = job_application_or_404(application_id, as_=("atelier",))
    post_id = application.post_id
    form = Form(request.form)
    message = forms.read_message(form, "message", "پیام", max_len=300)
    if not form.ok:
        return web.fail(next(iter(form.errors.values())), "marketplace.manage_application", 422, "validation_error", application_id=application_id)
    try:
        services.reject_application(application, get_user(), message)
        db.session.commit()
    except services.MarketplaceError as exc:
        return web.refuse(exc, "marketplace.manage_application", application_id=application_id)
    return web.done("درخواست رد شد و به متقاضی اطلاع داده شد.", "marketplace.job_applicants", post_id=post_id)


# --------------------------------------------------------------------------
# JSON: what do these terms pay?  (the arithmetic stays in payroll.compute_pay)
# --------------------------------------------------------------------------


@bp.post("/jobs/api/pay-preview")
@role_required(MANAGER)
def pay_preview():
    data = json_body()
    try:
        terms = {
            "base": data.get("base") or 0,
            "commission_per_unit": data.get("commission_per_unit") or 0,
            "bonus_tiers": data.get("bonus_tiers") or [],
        }
        units = int(data.get("units", common.REFERENCE_UNITS))
        if not 0 <= units <= 100_000:
            raise ValueError("units out of range")
        result = payroll.compute_pay(payroll.terms_from_dict(terms), units)
    except (ValueError, TypeError, KeyError):
        return json_error("شرایط واردشده برای محاسبهٔ نمونه معتبر نیست.", 422)
    payload = result.as_dict()
    payload["sentence"] = f"اگر {to_persian_digits(units)} عکس فروخته شود: {format_toman(result.total, persian_digits=True)} تومان"
    return json_ok(payload)
