"""My afish: invitations (accept / decline), one assignment, the schedule and the .ics downloads."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from flask import Response, abort, current_app, render_template, request, url_for
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event
from app.modules.staff import availability, bp, common, ics, paytext, queries, web
from app.services import jalali, security, staffing
from app.services.constants import (
    COMMITTED_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    EventStatus,
    Role,
)
from app.services.forms import Form
from app.services.http import paginate
from app.services.permissions import assignment_or_404, get_user, role_required

STAFF = Role.STAFF
PER_PAGE = 10
REASON_MAX = 300


def _mine(assignment_id: int) -> Assignment:
    """An afish row of mine (directly or through a linked roster entry); anybody else's is a 404."""
    return assignment_or_404(assignment_id, as_=("assignee",))


def _conflicts_by_date(user_id: int, rows: list[Assignment]) -> dict[dt.date, list[Assignment]]:
    """My committed afish rows on the dates of *rows* - one query for the whole page."""
    dates = {row.event.date for row in rows}
    if not dates:
        return {}
    found = db.session.execute(
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Assignment.involving_user(user_id),
            Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
            Event.date.in_(sorted(dates)),
            Event.status != EventStatus.CANCELLED,
        )
        .options(joinedload(Assignment.event).joinedload(Event.hall))
    ).scalars().unique()
    by_date: dict[dt.date, list[Assignment]] = {}
    for row in found:
        by_date.setdefault(row.event.date, []).append(row)
    return by_date


def _card(assignment: Assignment, conflicts: dict[dt.date, list[Assignment]], today: dt.date) -> dict[str, Any]:
    event = assignment.event
    terms = assignment.terms_dict()
    clashes = [row for row in conflicts.get(event.date, []) if row.id != assignment.id]
    return {
        "assignment": assignment,
        "event": event,
        "hall": event.hall,
        "atelier": event.atelier,
        "terms": terms,
        "lines": paytext.terms_lines(terms),
        "example": paytext.example(terms),
        "clashes": clashes,
        "venue": common.venue_visible(assignment),
        "call_time": common.call_time_of(assignment),
        "expired": event.date < today or event.status in queries.CLOSED_EVENT,
    }


# -- invitations -----------------------------------------------------------------


@bp.get("/invitations")
@role_required(STAFF)
def invitations():
    user = get_user()
    today = security.today()
    tab = "history" if request.args.get("tab") == "history" else "pending"
    select = (
        queries.invitation_history_select(user.id, today)
        if tab == "history"
        else queries.invitations_select(user.id, today)
    )
    page = paginate(select, per_page=PER_PAGE)
    conflicts = _conflicts_by_date(user.id, page.items)
    cards = [_card(row, conflicts, today) for row in page.items]
    pending_count, history_count = queries.invitation_counts(user.id, today)
    return render_template(
        "staff/invitations.html",
        tab=tab,
        page=page,
        cards=cards,
        pending_count=pending_count,
        history_count=history_count,
    )


# -- one assignment ---------------------------------------------------------------


@bp.get("/assignments/<int:assignment_id>")
@role_required(STAFF)
def assignment(assignment_id: int):
    user = get_user()
    row = _mine(assignment_id)
    today = security.today()
    event = row.event
    terms = row.terms_dict()
    venue = common.venue_visible(row)
    committed = row.status in COMMITTED_ASSIGNMENT_STATUSES
    live = row.status != AssignmentStatus.CANCELLED and event.status != EventStatus.CANCELLED
    clashes = queries.same_day_others(user.id, row) if row.status == AssignmentStatus.INVITED else []
    committed_clashes = [other for other in clashes if other.status in COMMITTED_ASSIGNMENT_STATUSES]
    atelier = event.atelier
    future = event.date >= today
    return render_template(
        "staff/assignment.html",
        row=row,
        event=event,
        hall=event.hall,
        atelier=atelier,
        terms=terms,
        lines=paytext.terms_lines(terms),
        example=paytext.example(terms),
        scenarios=paytext.scenarios(terms),
        several_tiers=paytext.has_several_tiers(terms),
        venue=venue,
        committed=committed,
        call_time=common.call_time_of(row),
        clashes=committed_clashes,
        show_override=bool(committed_clashes) or request.args.get("conflict") == "1",
        can_respond=row.status == AssignmentStatus.INVITED and future and live and event.status not in queries.CLOSED_EVENT,
        can_withdraw=row.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED)
        and future
        and live
        and not row.is_terms_locked,
        can_calendar=live and row.status != AssignmentStatus.DECLINED,
        expired=(event.date < today and row.status == AssignmentStatus.INVITED),
        team=queries.teammates(row) if committed and live else [],
        share=common.share_text(row, include_venue=venue),
        person_label=common.person_label,
        today=today,
        reason_max=REASON_MAX,
    )


def _respond_target(row: Assignment) -> str:
    return web.back_url("staff.assignment", assignment_id=row.id)


@bp.post("/assignments/<int:assignment_id>/accept")
@role_required(STAFF)
def assignment_accept(assignment_id: int):
    user = get_user()
    row = _mine(assignment_id)
    target = _respond_target(row)
    if row.status in COMMITTED_ASSIGNMENT_STATUSES:
        return web.done("این افیش را قبلاً پذیرفته‌اید.", target, level="info")
    if row.status != AssignmentStatus.INVITED:
        return web.refuse("این دعوت دیگر فعال نیست؛ اگر لازم است اتلیه باید دوباره شما را دعوت کند.", target, code="not_invited")
    if row.event.date < security.today():
        return web.refuse("تاریخ این مراسم گذشته است و دیگر نمی‌توان آن را پذیرفت.", target, code="expired")
    override = Form(request.form).boolean("allow_conflict")
    try:
        staffing.transition(row, AssignmentStatus.ACCEPTED, user, allow_conflict=override)
        db.session.commit()
    except staffing.AssignmentConflict:
        conflict_target = url_for("staff.assignment", assignment_id=row.id, conflict=1)
        return web.refuse(
            "در همین روز مراسم دیگری دارید. اگر می‌خواهید هر دو را انجام دهید، گزینهٔ «پذیرفتن با وجود تداخل» را بزنید.",
            conflict_target,
            code="conflict",
            level="warning",
        )
    except staffing.EventNotStaffable:
        return web.refuse("این مراسم لغو یا بسته شده است و دیگر قابل پذیرفتن نیست.", target, code="event_closed")
    except staffing.InvalidTransition:
        return web.refuse("وضعیت این دعوت تغییر کرده است؛ صفحه را تازه کنید.", target, code="stale")
    except staffing.StaffingPermissionError:
        abort(404)
    return web.done(
        "دعوت را پذیرفتید؛ اتلیه باخبر شد.",
        target,
        data={"status": AssignmentStatus.ACCEPTED.value},
    )


@bp.post("/assignments/<int:assignment_id>/decline")
@role_required(STAFF)
def assignment_decline(assignment_id: int):
    user = get_user()
    row = _mine(assignment_id)
    target = _respond_target(row)
    if row.status == AssignmentStatus.DECLINED:
        return web.done("این دعوت را قبلاً رد کرده‌اید.", target, level="info")
    if row.status not in (AssignmentStatus.INVITED, AssignmentStatus.ACCEPTED):
        return web.refuse("این افیش را نمی‌توان رد کرد.", target, code="not_declinable")
    form = Form(request.form)
    reason = form.text("reason", "دلیل", max_len=REASON_MAX, multiline=True)
    if not form.ok:
        return web.refuse(form.errors["reason"], target, status=422, code="validation_error", fields=form.errors)
    try:
        staffing.transition(row, AssignmentStatus.DECLINED, user, reason=reason)
        db.session.commit()
    except staffing.TermsLocked:
        return web.refuse("پس از ثبت فاکتور مراسم، امکان رد کردن نیست؛ با اتلیه هماهنگ کنید.", target, code="locked")
    except staffing.InvalidTransition:
        return web.refuse("وضعیت این دعوت تغییر کرده است؛ صفحه را تازه کنید.", target, code="stale")
    except staffing.StaffingPermissionError:
        abort(404)
    return web.done("دعوت را رد کردید؛ اتلیه باخبر شد.", target, data={"status": AssignmentStatus.DECLINED.value})


@bp.post("/assignments/<int:assignment_id>/withdraw")
@role_required(STAFF)
def assignment_withdraw(assignment_id: int):
    """Back out of an afish row I had said yes to (the atelier is told, and may staff somebody else)."""
    user = get_user()
    row = _mine(assignment_id)
    target = web.back_url("staff.assignment", assignment_id=row.id)
    if row.status == AssignmentStatus.CANCELLED:
        return web.done("این افیش قبلاً لغو شده است.", target, level="info")
    if row.status not in (AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED):
        return web.refuse("فقط افیشی که پذیرفته یا قطعی شده را می‌توان لغو کرد.", target, code="not_cancellable")
    if row.event.date < security.today():
        return web.refuse("مراسم گذشته است و لغو افیش معنایی ندارد.", target, code="expired")
    form = Form(request.form)
    reason = form.text("reason", "دلیل", max_len=REASON_MAX, multiline=True)
    if not form.ok:
        return web.refuse(form.errors["reason"], target, status=422, code="validation_error", fields=form.errors)
    try:
        staffing.transition(row, AssignmentStatus.CANCELLED, user, reason=reason)
        db.session.commit()
    except staffing.TermsLocked:
        return web.refuse("پس از ثبت فاکتور مراسم، امکان لغو نیست؛ با اتلیه هماهنگ کنید.", target, code="locked")
    except staffing.InvalidTransition:
        return web.refuse("وضعیت این افیش تغییر کرده است؛ صفحه را تازه کنید.", target, code="stale")
    except staffing.StaffingPermissionError:
        abort(404)
    return web.done("همکاری شما لغو شد و اتلیه باخبر شد.", target, data={"status": AssignmentStatus.CANCELLED.value})


# -- schedule ---------------------------------------------------------------------


def _chip(row: Assignment) -> dict[str, str]:
    hall = row.event.hall.name if row.event.hall else ""
    return {
        "label": f"{row.role.label} · {hall}".strip(" ·"),
        "url": url_for("staff.assignment", assignment_id=row.id),
        "tone": common.CALENDAR_TONES.get(row.status, "neutral"),
    }


@bp.get("/schedule")
@role_required(STAFF)
def schedule():
    user = get_user()
    today = security.today()
    view = "list" if request.args.get("view") == "list" else "month"
    context: dict[str, Any] = {"view": view, "today": today}
    if view == "month":
        year, month = common.parse_month(request.args, today)
        first, last = common.month_bounds(year, month)
        rows = queries.schedule_between(user.id, first, last)
        events: dict[dt.date, list[dict[str, str]]] = {}
        for row in rows:
            events.setdefault(row.event.date, []).append(_chip(row))
        for day, mark in availability.marks_between(user.id, first, last).items():
            if mark.status.value == "unavailable" and day not in events:
                events.setdefault(day, []).append({"label": "در دسترس نیستم", "url": "", "tone": "neutral"})
        prev_y, prev_m = common.shift_month(year, month, -1)
        next_y, next_m = common.shift_month(year, month, 1)
        cy, cm, _ = common.jalali_today(today)
        context.update(
            grid=jalali.month_grid(year, month),
            events=events,
            title=common.month_title(year, month),
            prev_url=url_for("staff.schedule", y=prev_y, m=prev_m),
            next_url=url_for("staff.schedule", y=next_y, m=next_m),
            this_month_url=None if (year, month) == (cy, cm) else url_for("staff.schedule"),
            month_count=len(rows),
        )
    else:
        when = "past" if request.args.get("when") == "past" else "upcoming"
        page = paginate(queries.schedule_select(user.id, today, when), per_page=PER_PAGE)
        conflicts = _conflicts_by_date(user.id, page.items)
        context.update(when=when, page=page, cards=[_card(row, conflicts, today) for row in page.items])
    return render_template("staff/schedule.html", **context)


# -- .ics -------------------------------------------------------------------------


def _calendar_response(rows: list[Assignment], filename: str) -> Response:
    domain = current_app.config.get("SITE_DOMAIN") or "atelier.local"
    text = ics.build_calendar(
        rows,
        now=security.now(),
        domain=domain,
        venue_visible=common.venue_visible,
        url_for_assignment=lambda row: url_for("staff.assignment", assignment_id=row.id, _external=True),
    )
    response = Response(text.encode("utf-8"), mimetype="text/calendar")
    response.headers["Content-Type"] = "text/calendar; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/assignments/<int:assignment_id>/event.ics")
@role_required(STAFF)
def assignment_ics(assignment_id: int):
    row = _mine(assignment_id)
    if row.status in (AssignmentStatus.DECLINED, AssignmentStatus.CANCELLED) or row.event.status == EventStatus.CANCELLED:
        abort(404)
    return _calendar_response([row], f"afish-{row.id}.ics")


@bp.get("/schedule.ics")
@role_required(STAFF)
def schedule_ics():
    """Every future afish row (invited ones as tentative) in one calendar file."""
    user = get_user()
    rows = queries.upcoming_calendar_rows(user.id, security.today())
    return _calendar_response(rows, "afish-schedule.ics")

