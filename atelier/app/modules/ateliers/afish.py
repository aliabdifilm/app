"""The staffing board (افیش): assign people to the role slots of an event.

Flow for one slot::

    event page ──► pick a person ──► assignment form ──► staffing.assign_person (terms snapshot)
                    roster | directory | marketplace job post          │
                                                                        └─► invited (platform user) / confirmed (roster)

Every write goes through ``app.services.staffing``; this module parses forms, scopes everything to
the manager's atelier, shows the warnings (double booking, unavailable, duplicates) and maps the
service errors to Persian messages.  Ids that arrive from the browser (roster member, user,
assignment, replacement) are re-loaded through the ownership helpers on every request.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import sqlalchemy as sa
from flask import abort, flash, render_template, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, RosterMember, User, normalize_terms
from app.modules.ateliers import bp, forms, links
from app.modules.ateliers import board as boards
from app.modules.ateliers import queries
from app.modules.ateliers.common import (
    MANAGER,
    current_atelier,
    describe_staffing_error,
    fail,
    my_event_or_404,
    spend_lookup_budget,
    to_event,
)
from app.services import payroll, security, staffing
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    Skill,
)
from app.services.forms import Form
from app.services.http import json_body, json_error, json_ok, paginate
from app.services.permissions import (
    assignment_or_404,
    get_user,
    my_atelier_ids,
    role_required,
    roster_member_or_404,
)

RANGES = ("upcoming", "week", "past")
ACTIONS = {
    "confirm": AssignmentStatus.CONFIRMED,
    "cancel": AssignmentStatus.CANCELLED,
    "reinvite": AssignmentStatus.INVITED,
}
ACTION_DONE = {
    "confirm": "افیش قطعی شد.",
    "cancel": "از افیش حذف شد.",
    "reinvite": "دعوت دوباره ارسال شد.",
}
PREVIEW_UNITS = 80  # the AGENTS.md test vector: 80 photos
MAX_PREVIEW_UNITS = 100_000


# --------------------------------------------------------------------------
# argument helpers (everything from the browser is re-checked here)
# --------------------------------------------------------------------------


def _role_arg() -> Skill:
    try:
        return Skill.coerce(request.values.get("role", ""))
    except ValueError:
        abort(404)


def _staffable_or_redirect(event: Event):
    """``None`` when the afish of *event* can change, else a redirect with the reason."""
    if boards.is_staffable(event):
        return None
    return fail("این مراسم بسته شده است و افیش آن دیگر قابل تغییر نیست.", "ateliers.event_detail", event_id=event.id)


def _person(event: Event) -> tuple[Optional[RosterMember], Optional[User]]:
    """The person picked in the request: exactly one of a roster member of this atelier or a directory user."""
    member_id = request.values.get("member", type=int)
    user_id = request.values.get("user", type=int)
    if bool(member_id) == bool(user_id):
        abort(404)
    if member_id:
        member = roster_member_or_404(member_id)
        if member.atelier_id != event.atelier_id:
            abort(404)
        return member, None
    user = boards.directory_user(get_user().id, user_id, request.values.get("k", ""))
    if user is None:
        abort(404)
    return None, user


def _replace_arg(event: Event, role: Skill) -> Optional[Assignment]:
    rid = request.values.get("replace", type=int)
    if not rid:
        return None
    assignment = assignment_or_404(rid, as_=("atelier",))
    if assignment.event_id != event.id or Skill.coerce(assignment.role) != role:
        abort(404)
    return assignment


def _event_assignment(event: Event, assignment_id: int) -> Assignment:
    assignment = assignment_or_404(assignment_id, as_=("atelier",))
    if assignment.event_id != event.id:
        abort(404)
    return assignment


# --------------------------------------------------------------------------
# overview of all upcoming afish
# --------------------------------------------------------------------------


@bp.get("/afish")
@role_required(MANAGER)
def afish_board():
    atelier = current_atelier()
    today = security.today()
    scope = request.args.get("range", "upcoming")
    if scope not in RANGES:
        scope = "upcoming"
    if scope == "past":
        stmt = queries.past(atelier.id, today)
    else:
        stmt = queries.upcoming(atelier.id, today)
        if scope == "week":
            stmt = stmt.where(Event.date <= today + timedelta(days=7))
    page = paginate(stmt, per_page=8)
    event_boards = boards.load_boards(list(page.items))
    gaps = sum(b.completeness.missing + b.completeness.invited for b in event_boards.values())
    return render_template(
        "ateliers/afish_board.html",
        atelier=atelier,
        page=page,
        boards=event_boards,
        scope=scope,
        gaps=gaps,
        today=today,
    )


# --------------------------------------------------------------------------
# step 1: pick the person
# --------------------------------------------------------------------------


@bp.get("/events/<int:event_id>/afish/new")
@role_required(MANAGER)
def afish_pick(event_id: int):
    event = my_event_or_404(event_id)
    blocked = _staffable_or_redirect(event)
    if blocked is not None:
        return blocked
    role = _role_arg()
    replace = _replace_arg(event, role)
    via = request.args.get("via", "roster")
    if via not in ("roster", "search"):
        via = "roster"
    q = (request.args.get("q") or "").strip()[:80]
    if via == "search" and boards.looks_like_mobile(q):
        spend_lookup_budget()
    atelier = event.atelier
    board = boards.load_board(event)
    taken = board.summary[role].assignments
    taken_members = {a.roster_member_id for a in taken if a.roster_member_id}
    taken_users = {a.user_id for a in taken if a.user_id}
    busy = boards.busy_index(event.date, exclude_event_id=event.id)

    rows: list[dict[str, Any]] = []
    show_all = request.args.get("all") == "1"
    hidden_count = 0
    if via == "roster":
        members = list(
            db.session.execute(
                sa.select(RosterMember)
                .where(RosterMember.atelier_id == atelier.id, RosterMember.is_active.is_(True))
                .options(joinedload(RosterMember.user))
                .order_by(RosterMember.display_name, RosterMember.id)
            ).scalars()
        )
        unavailable = boards.unavailable_user_ids({m.user_id for m in members if m.user_id}, event.date)
        for member in members:
            can_do = member.can_do(role)
            if not can_do and not show_all:
                hidden_count += 1
                continue
            rows.append(
                {
                    "member": member,
                    "can_do": can_do,
                    "busy": busy.member_busy(member),
                    "unavailable": member.user_id in unavailable if member.user_id else False,
                    "taken": member.id in taken_members,
                    "terms": member.terms_for(role),
                }
            )
    else:
        hits = boards.search_directory(get_user().id, role, q)
        unavailable = boards.unavailable_user_ids({hit.user.id for hit in hits}, event.date)
        for hit in hits:
            rows.append(
                {
                    "hit": hit,
                    "busy": busy.user_busy(hit.user),
                    "unavailable": hit.user.id in unavailable,
                    "taken": hit.user.id in taken_users,
                }
            )
    terms, _ = boards.prefill_terms(atelier, role)
    return render_template(
        "ateliers/afish_pick.html",
        event=event,
        atelier=atelier,
        role=role,
        via=via,
        q=q,
        rows=rows,
        show_all=show_all,
        hidden_count=hidden_count,
        replace=replace,
        job_url=links.marketplace_job_url(event, role, terms, slots=max(1, board.summary[role].shortfall)),
        roster_total=db.session.scalar(
            sa.select(sa.func.count(RosterMember.id)).where(
                RosterMember.atelier_id == atelier.id, RosterMember.is_active.is_(True)
            )
        ),
    )


# --------------------------------------------------------------------------
# step 2: terms and checks, then assign
# --------------------------------------------------------------------------


def pay_example(terms: dict[str, Any], units: int = PREVIEW_UNITS) -> dict[str, Any]:
    """What the terms pay for *units* sold photos (the formula is ``payroll.compute_pay``)."""
    return payroll.compute_pay(terms, units).as_dict()


def _render_assign(
    event: Event,
    role: Skill,
    member: Optional[RosterMember],
    user: Optional[User],
    replace: Optional[Assignment],
    *,
    values: dict[str, Any],
    errors: dict[str, str],
    source: str,
    override: bool = False,
    status: int = 200,
):
    atelier = event.atelier
    check = staffing.check_assignable(event, role, user=user, roster_member=member)
    board = boards.load_board(event)
    person_user = user or (member.user if member is not None else None)
    others = [
        a
        for a in board.assignments
        if a.status in ACTIVE_ASSIGNMENT_STATUSES
        and Skill.coerce(a.role) != role
        and (
            (user is not None and a.user_id == user.id)
            or (member is not None and a.roster_member_id == member.id)
        )
    ]
    needs_override = bool(check.conflicts or check.unavailable)
    blocked = needs_override and atelier.block_double_booking
    try:
        example = pay_example(forms_terms_for_preview(values), PREVIEW_UNITS)
    except (ValueError, TypeError):
        example = None
    return (
        render_template(
            "ateliers/afish_assign.html",
            event=event,
            atelier=atelier,
            role=role,
            member=member,
            user=user,
            person_user=person_user,
            token=request.values.get("k", ""),
            replace=replace,
            duplicates=check.duplicates,
            conflicts=boards.describe_conflicts(check.conflicts, my_atelier_ids()),
            unavailable=check.unavailable,
            others=others,
            needs_override=needs_override,
            blocked=blocked,
            override=override,
            source_text=boards.TERMS_SOURCE_LABELS.get(source, ""),
            example=example,
            example_units=PREVIEW_UNITS,
            expected=(user.staff_profile.expected_terms() if user is not None and user.staff_profile else None),
            skill_ok=(member.can_do(role) if member is not None else True),
            form=values,
            errors=errors,
        ),
        status,
    )


def forms_terms_for_preview(values: dict[str, Any]) -> dict[str, Any]:
    """Terms dict from redisplay values (``base``, ``commission``, ``tiers``) for the example line."""
    tiers = []
    for row in values.get("tiers") or []:
        if str(row.get("threshold", "")).strip() and str(row.get("amount", "")).strip() != "":
            tiers.append({"threshold": row["threshold"], "amount": row["amount"]})
    return normalize_terms(
        {
            "base": values.get("base") or 0,
            "commission_per_unit": values.get("commission") or 0,
            "bonus_tiers": tiers,
        }
    )


@bp.route("/events/<int:event_id>/afish/assign", methods=["GET", "POST"])
@role_required(MANAGER)
def afish_assign(event_id: int):
    event = my_event_or_404(event_id)
    blocked = _staffable_or_redirect(event)
    if blocked is not None:
        return blocked
    role = _role_arg()
    member, user = _person(event)
    replace = _replace_arg(event, role)
    actor = get_user()
    atelier = event.atelier
    if member is not None and not member.is_active:
        return fail("این نیرو غیرفعال است و افیش نمی‌شود.", "ateliers.afish_pick", event_id=event.id, role=role.value)
    if replace is not None and (
        replace.status in (AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED) or replace.is_terms_locked
    ):
        return fail("جای این نفر قابل جایگزینی نیست؛ پس از ثبت فاکتور شرایط قفل می‌شود.", "ateliers.event_detail", event_id=event.id)

    if request.method == "GET":
        previous = replace
        terms, source = boards.prefill_terms(atelier, role, member=member, user=user, previous=previous)
        values = forms.terms_to_form(terms)
        values["call_time"] = ""
        return _render_assign(event, role, member, user, replace, values=values, errors={}, source=source)

    form = Form(request.form)
    terms = forms.read_terms(form)
    call_time = form.time("call_time", "ساعت حضور")
    notes = form.text("notes", "یادداشت", max_len=500, multiline=True)
    override = form.boolean("override")
    check = staffing.check_assignable(event, role, user=user, roster_member=member)
    if check.duplicates:
        form.error("person", "این نفر همین نقش را از قبل در این مراسم دارد.")
    needs_override = bool(check.conflicts or check.unavailable)
    if needs_override and atelier.block_double_booking:
        form.error("override", "طبق تنظیمات اتلیه، افیش‌کردن کسی که همان روز مشغول است مجاز نیست.")
    elif needs_override and not override:
        form.error("override", "این نفر همان روز مشغول است؛ برای ادامه تیک «افیش با علم به تداخل» را بزنید.")
    if not form.ok or terms is None:
        return _render_assign(
            event, role, member, user, replace,
            values=form.values, errors=form.errors, source="", override=override, status=422,
        )
    try:
        assignment = staffing.assign_person(
            event,
            role,
            user=user,
            roster_member=member,
            terms=terms,
            actor=actor,
            call_time=call_time,
            notes=notes,
            allow_conflict=bool(check.conflicts and override),
        )
        if replace is not None:
            staffing.transition(replace, AssignmentStatus.CANCELLED, actor, reason="جایگزین شد")
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        form.error("person", describe_staffing_error(exc))
        return _render_assign(
            event, role, member, user, replace,
            values=form.values, errors=form.errors, source="", override=override, status=422,
        )
    except IntegrityError:  # two requests staffed the same person at once (partial unique index)
        db.session.rollback()
        return fail("این نفر همین حالا برای این نقش افیش شد.", "ateliers.event_detail", event_id=event_id)
    if assignment.status == AssignmentStatus.INVITED:
        flash(f"دعوت برای «{assignment.person_name}» ارسال شد؛ پس از پذیرفتن او قطعی می‌شود.", "success")
    else:
        flash(f"«{assignment.person_name}» به‌عنوان {role.label} افیش و قطعی شد.", "success")
    return to_event(event_id)


# --------------------------------------------------------------------------
# actions on one assignment
# --------------------------------------------------------------------------


@bp.post("/events/<int:event_id>/afish/<int:assignment_id>/<action>")
@role_required(MANAGER)
def afish_action(event_id: int, assignment_id: int, action: str):
    """confirm (skip the wait), cancel (remove from the afish) or reinvite (after a refusal)."""
    target = ACTIONS.get(action)
    if target is None:
        abort(404)
    event = my_event_or_404(event_id)
    assignment = _event_assignment(event, assignment_id)
    form = Form(request.form)
    reason = form.text("reason", "دلیل", max_len=200)
    try:
        staffing.transition(assignment, target, get_user(), reason=reason)
        db.session.commit()
    except staffing.AssignmentConflict:
        db.session.rollback()
        return fail("این نفر همان روز در مراسم قطعی دیگری هم هست؛ ابتدا یکی از آن‌ها را لغو کنید.", "ateliers.event_detail", event_id=event_id)
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash(ACTION_DONE[action], "success")
    return to_event(event_id)


@bp.post("/events/<int:event_id>/afish/confirm-all")
@role_required(MANAGER)
def afish_confirm_all(event_id: int):
    """Confirm everybody who accepted; people who have not answered stay invited."""
    event = my_event_or_404(event_id)
    blocked = _staffable_or_redirect(event)
    if blocked is not None:
        return blocked
    actor = get_user()
    accepted = [a for a in event.assignments if a.status == AssignmentStatus.ACCEPTED]
    done = 0
    problems: list[str] = []
    for assignment in accepted:
        try:
            with db.session.begin_nested():
                staffing.transition(assignment, AssignmentStatus.CONFIRMED, actor)
            done += 1
        except staffing.StaffingError as exc:
            problems.append(f"{assignment.person_name}: {describe_staffing_error(exc)}")
    db.session.commit()
    waiting = sum(1 for a in event.assignments if a.status == AssignmentStatus.INVITED)
    if done:
        flash(f"{done} نفر قطعی شدند." + (f" {waiting} نفر هنوز پاسخ نداده‌اند." if waiting else ""), "success")
    elif not accepted:
        flash("کسی نیست که پذیرفته باشد و قطعی نشده باشد." + (f" {waiting} نفر هنوز پاسخ نداده‌اند." if waiting else ""), "info")
    for problem in problems:
        flash(problem, "error")
    return to_event(event_id)


@bp.route("/events/<int:event_id>/afish/<int:assignment_id>/terms", methods=["GET", "POST"])
@role_required(MANAGER)
def afish_terms(event_id: int, assignment_id: int):
    """Change the pay terms of one assignment (audited; the person is notified; refused once locked)."""
    event = my_event_or_404(event_id)
    assignment = _event_assignment(event, assignment_id)
    editable = not assignment.is_terms_locked and assignment.status != AssignmentStatus.CANCELLED
    if request.method == "GET" or not editable:
        values = forms.terms_to_form(assignment.terms_dict())
        return render_template(
            "ateliers/afish_terms.html",
            event=event,
            assignment=assignment,
            editable=editable,
            form=values,
            errors={},
        ), (200 if request.method == "GET" else 409)
    form = Form(request.form)
    terms = forms.read_terms(form)
    reason = form.text("reason", "دلیل تغییر", max_len=200)
    if not form.ok or terms is None:
        return render_template(
            "ateliers/afish_terms.html", event=event, assignment=assignment, editable=True, **form.context()
        ), 422
    try:
        staffing.set_terms(assignment, terms, get_user(), reason=reason)
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash("شرایط پرداخت ذخیره شد." + (" نیرو باخبر شد." if assignment.person_user_id else ""), "success")
    return to_event(event_id)


# --------------------------------------------------------------------------
# copy the afish of another event
# --------------------------------------------------------------------------


@bp.route("/events/<int:event_id>/afish/copy", methods=["GET", "POST"])
@role_required(MANAGER)
def afish_copy(event_id: int):
    event = my_event_or_404(event_id)
    blocked = _staffable_or_redirect(event)
    if blocked is not None:
        return blocked
    source_id = request.values.get("source", type=int)
    source: Optional[Event] = None
    if source_id:
        source = my_event_or_404(source_id)
        if source.id == event.id or source.atelier_id != event.atelier_id:
            abort(404)
    if request.method == "GET":
        plan = boards.copy_plan(source, event) if source is not None else []
        return render_template(
            "ateliers/afish_copy.html",
            event=event,
            source=source,
            previous=boards.previous_events(event) if source is None else [],
            plan=plan,
        )
    if source is None:
        return fail("مراسم مبدأ را انتخاب کنید.", "ateliers.afish_copy", event_id=event_id)
    actor = get_user()
    plan = {row.assignment.id: row for row in boards.copy_plan(source, event)}
    created: list[str] = []
    skipped: list[str] = []
    for assignment_id in Form(request.form).int_list("pick"):
        row = plan.get(assignment_id)
        if row is None:
            continue  # not an assignment of the chosen source event: ignore
        source_assignment = row.assignment
        if not row.copyable:
            skipped.append(f"{source_assignment.person_name}: {row.skip_reason}")
            continue
        try:
            with db.session.begin_nested():
                staffing.assign_person(
                    event,
                    source_assignment.role,
                    user=source_assignment.user,
                    roster_member=source_assignment.roster_member,
                    terms=source_assignment.terms_dict(),
                    actor=actor,
                )
            created.append(source_assignment.person_name)
        except (staffing.StaffingError, IntegrityError) as exc:
            skipped.append(
                f"{source_assignment.person_name}: "
                + (describe_staffing_error(exc) if isinstance(exc, staffing.StaffingError) else "هم‌زمان افیش شد.")
            )
    db.session.commit()
    if created:
        flash(f"{len(created)} نفر از مراسم قبلی افیش شدند: " + "، ".join(created) + ".", "success")
    elif not skipped:
        flash("کسی انتخاب نشده بود.", "info")
    for message in skipped:
        flash(message, "warning")
    return to_event(event_id)


# --------------------------------------------------------------------------
# JSON: example of what terms pay (the formula stays in app.services.payroll)
# --------------------------------------------------------------------------


@bp.post("/api/pay-preview")
@role_required(MANAGER)
def pay_preview():
    data = json_body()
    try:
        terms = normalize_terms(
            {
                "base": data.get("base") or 0,
                "commission_per_unit": data.get("commission_per_unit") or 0,
                "bonus_tiers": data.get("bonus_tiers") or [],
            }
        )
        units = int(data.get("units", PREVIEW_UNITS))
        if units < 0 or units > MAX_PREVIEW_UNITS:
            raise ValueError("units out of range")
        result = payroll.compute_pay(terms, units)
    except (ValueError, TypeError):
        return json_error("شرایط واردشده برای محاسبهٔ نمونه معتبر نیست.", 422)
    return json_ok(result.as_dict())
