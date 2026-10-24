"""Fixed staff (نیروهای ثابت) of the atelier: list, create, edit, activate, delete.

A roster member may have no account (assumption A7): then only a name and, optionally, a
mobile number are stored.  When the number belongs to a registered staff account the member is
*linked* to it, so their afish rows show up in their own panel and they receive notifications.
Roster members are confirmed immediately when they are staffed (no acceptance step).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Assignment, Atelier, Event, RosterMember, User
from app.modules.ateliers import bp, forms
from app.modules.ateliers import board as boards
from app.modules.ateliers.common import MANAGER, current_atelier, paginate_list, spend_lookup_budget
from app.services import audit, security
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    ROLE_DISPLAY_ORDER,
    EventStatus,
    Role,
    Skill,
    UserStatus,
)
from app.services.forms import Form
from app.services.http import paginate
from app.services.permissions import get_user, role_required, roster_member_or_404

PER_PAGE = 20


def _upcoming_counts(member_ids: list[int], today: dt.date) -> dict[int, int]:
    """Active future assignments per roster member in one query."""
    if not member_ids:
        return {}
    rows = db.session.execute(
        sa.select(Assignment.roster_member_id, sa.func.count(Assignment.id))
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Assignment.roster_member_id.in_(member_ids),
            Assignment.status.in_(sorted(ACTIVE_ASSIGNMENT_STATUSES)),
            Event.date >= today,
            Event.status != EventStatus.CANCELLED,
        )
        .group_by(Assignment.roster_member_id)
    )
    return {member_id: int(count) for member_id, count in rows}


@bp.get("/roster")
@role_required(MANAGER)
def roster():
    atelier = current_atelier()
    q = (request.args.get("q") or "").strip()[:80]
    skill = request.args.get("skill", "")
    state = request.args.get("state", "active")
    stmt = sa.select(RosterMember).where(RosterMember.atelier_id == atelier.id)
    if state == "active":
        stmt = stmt.where(RosterMember.is_active.is_(True))
    elif state == "inactive":
        stmt = stmt.where(RosterMember.is_active.is_(False))
    if q:
        pattern = boards.like_pattern(q)
        stmt = stmt.where(
            sa.or_(
                RosterMember.display_name.ilike(pattern, escape="\\"),
                RosterMember.mobile.ilike(pattern, escape="\\"),
            )
        )
    stmt = stmt.order_by(RosterMember.display_name, RosterMember.id)
    if Skill.is_valid(skill):
        # skills is a JSON list (never queried in SQL); a roster is small, so filter in Python
        wanted = Skill.coerce(skill).value
        rows = [m for m in db.session.execute(stmt.limit(500)).scalars() if wanted in (m.skills or [])]
        page = paginate_list(rows, per_page=PER_PAGE)
    else:
        page = paginate(stmt, per_page=PER_PAGE)
    members = page.items
    totals = db.session.execute(
        sa.select(RosterMember.is_active, sa.func.count(RosterMember.id))
        .where(RosterMember.atelier_id == atelier.id)
        .group_by(RosterMember.is_active)
    ).all()
    counts = {bool(flag): int(count) for flag, count in totals}
    return render_template(
        "ateliers/roster_list.html",
        atelier=atelier,
        page=page,
        members=members,
        upcoming=_upcoming_counts([m.id for m in members], security.today()),
        q=q,
        skill=skill if Skill.is_valid(skill) else "",
        state=state if state in ("active", "inactive", "all") else "active",
        counts=counts,
    )


# --------------------------------------------------------------------------
# create / edit
# --------------------------------------------------------------------------


def _linkable_user(mobile: Optional[str]) -> Optional[User]:
    """The registered staff account with this number (the only kind a roster entry may link to)."""
    if not mobile:
        return None
    user = db.session.execute(sa.select(User).where(User.mobile == mobile)).scalar_one_or_none()
    if user is None or user.role != Role.STAFF or user.status == UserStatus.SUSPENDED:
        return None
    return user


def _member_values(member: Optional[RosterMember]) -> dict[str, Any]:
    if member is None:
        return {"is_active": True, "skills": []}
    values: dict[str, Any] = {
        "display_name": member.display_name,
        "mobile": member.mobile or "",
        "notes": member.notes or "",
        "is_active": member.is_active,
        "skills": list(member.skills or []),
    }
    for role in ROLE_DISPLAY_ORDER:
        values.update(forms.terms_to_form((member.default_terms or {}).get(role.value), forms.role_suffix(role), blank_zero=True))
    return values


def _render_form(atelier: Atelier, member: Optional[RosterMember], values: dict, errors: dict, status: int = 200):
    return (
        render_template(
            "ateliers/roster_form.html",
            atelier=atelier,
            member=member,
            form=values,
            errors=errors,
            roles=ROLE_DISPLAY_ORDER,
            suffix=forms.role_suffix,
            presets={role.value: atelier.preset_for(role) for role in ROLE_DISPLAY_ORDER},
        ),
        status,
    )


def _save(atelier: Atelier, member: Optional[RosterMember]):
    """Validate the roster form and create / update *member*; returns ``(member, response)``."""
    form = Form(request.form)
    name = form.text("display_name", "نام", required=True, max_len=120)
    mobile = form.mobile("mobile", "شمارهٔ موبایل")
    notes = form.text("notes", "یادداشت", max_len=500, multiline=True)
    active = form.boolean("is_active")
    skills = forms.read_skills(form)
    if not skills and "skills" not in form.errors:
        form.error("skills", "دست‌کم یک مهارت انتخاب کنید.")
    defaults: dict[str, dict] = {}
    for role in ROLE_DISPLAY_ORDER:
        terms = forms.read_terms(form, forms.role_suffix(role), role.label)
        if terms is not None and role.value in skills and not forms.terms_are_empty(terms):
            defaults[role.value] = terms
    form.values["skills"] = skills
    if not form.ok:
        return None, _render_form(atelier, member, form.values, form.errors, 422)

    if mobile and (member is None or member.mobile != mobile):
        spend_lookup_budget()  # linking reveals whether the number is registered
    user = _linkable_user(mobile)
    creating = member is None
    member_id = None if creating else member.id
    if creating:
        member = RosterMember(
            atelier=atelier,
            display_name=name,
            mobile=mobile,
            notes=notes,
            skills=skills,
            default_terms=defaults,
            is_active=True,
            user=user,
        )
        db.session.add(member)
    else:
        member.display_name = name
        member.mobile = mobile
        member.notes = notes
        member.skills = skills
        member.default_terms = defaults
        member.is_active = active
        member.user = user
    try:
        db.session.flush()
    except IntegrityError:  # unique (atelier, user): this account is already on the roster
        db.session.rollback()
        form.error("mobile", "نیرویی با همین حساب کاربری قبلاً در نیروهای ثابت شما ثبت شده است.")
        again = db.session.get(RosterMember, member_id) if member_id else None
        return None, _render_form(atelier, again, form.values, form.errors, 422)
    audit.log(
        get_user(),
        "roster.created" if creating else "roster.updated",
        member,
        {"name": name, "skills": skills, "linked_user_id": user.id if user else None, "active": member.is_active},
    )
    return member, None


@bp.route("/roster/new", methods=["GET", "POST"])
@role_required(MANAGER)
def roster_new():
    atelier = current_atelier()
    if request.method == "GET":
        values = _member_values(None)
        prefill_skill = request.args.get("skill", "")
        if Skill.is_valid(prefill_skill):
            values["skills"] = [Skill.coerce(prefill_skill).value]
        return _render_form(atelier, None, values, {})
    member, response = _save(atelier, None)
    if response is not None:
        return response
    db.session.commit()
    linked = " و به حساب کاربری او متصل شد" if member.user_id else ""
    flash(f"«{member.display_name}» به نیروهای ثابت اضافه شد{linked}.", "success")
    return redirect(url_for("ateliers.roster"))


@bp.route("/roster/<int:member_id>/edit", methods=["GET", "POST"])
@role_required(MANAGER)
def roster_edit(member_id: int):
    member = roster_member_or_404(member_id)
    atelier = member.atelier
    if request.method == "GET":
        return _render_form(atelier, member, _member_values(member), {})
    saved, response = _save(atelier, member)
    if response is not None:
        return response
    db.session.commit()
    flash("اطلاعات نیرو ذخیره شد.", "success")
    return redirect(url_for("ateliers.roster"))


@bp.post("/roster/<int:member_id>/toggle")
@role_required(MANAGER)
def roster_toggle(member_id: int):
    """Activate or deactivate (an inactive member cannot be staffed but keeps their history)."""
    member = roster_member_or_404(member_id)
    member.is_active = not member.is_active
    db.session.flush()
    audit.log(get_user(), "roster.activated" if member.is_active else "roster.deactivated", member, None)
    db.session.commit()
    flash(
        f"«{member.display_name}» " + ("دوباره فعال شد." if member.is_active else "غیرفعال شد؛ افیش‌های گذشته‌اش حفظ می‌شود."),
        "success",
    )
    return redirect(security.safe_next(request.form.get("next"), url_for("ateliers.roster")))


@bp.post("/roster/<int:member_id>/delete")
@role_required(MANAGER)
def roster_delete(member_id: int):
    """Delete a member who was never staffed; anybody with afish history can only be deactivated."""
    member = roster_member_or_404(member_id)
    used = db.session.scalar(sa.select(sa.func.count(Assignment.id)).where(Assignment.roster_member_id == member.id))
    if used:
        flash("این نیرو در افیش‌ها سابقه دارد و حذف نمی‌شود؛ می‌توانید او را غیرفعال کنید.", "error")
        return redirect(url_for("ateliers.roster"))
    audit.log(get_user(), "roster.deleted", member, {"name": member.display_name})
    name = member.display_name
    db.session.delete(member)
    db.session.commit()
    flash(f"«{name}» از نیروهای ثابت حذف شد.", "success")
    return redirect(url_for("ateliers.roster"))


@bp.post("/roster/add-user")
@role_required(MANAGER)
def roster_add_user():
    """Add a platform staff account found in the directory to the roster (and link it)."""
    atelier = current_atelier()
    form = Form(request.form)
    user_id = forms.read_id(form, "user_id")
    user = boards.directory_user(get_user().id, user_id, form.raw("k")) if user_id else None
    if user is None:
        flash("این نیرو پیدا نشد یا قابل افزودن نیست.", "error")
        return redirect(url_for("ateliers.roster"))
    existing = db.session.execute(
        sa.select(RosterMember).where(RosterMember.atelier_id == atelier.id, RosterMember.user_id == user.id)
    ).scalar_one_or_none()
    if existing is not None:
        flash(f"«{existing.display_name}» قبلاً در نیروهای ثابت شما هست.", "info")
        return redirect(url_for("ateliers.roster_edit", member_id=existing.id))
    skills = user.staff_profile.skills if user.staff_profile is not None else []
    member = RosterMember(
        atelier=atelier,
        user=user,
        display_name=user.display_name,
        mobile=user.mobile,
        skills=skills or [Skill.PHOTOGRAPHER.value],
    )
    db.session.add(member)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("این نیرو قبلاً اضافه شده است.", "info")
        return redirect(url_for("ateliers.roster"))
    audit.log(get_user(), "roster.created", member, {"name": member.display_name, "linked_user_id": user.id, "from": "directory"})
    db.session.commit()
    flash(f"«{member.display_name}» به نیروهای ثابت اضافه شد؛ مهارت‌ها و پیش‌فرض حقوق را بررسی کنید.", "success")
    return redirect(url_for("ateliers.roster_edit", member_id=member.id))
