"""The talent directory (``/talent``) and direct invitations.

Visible to signed-in atelier managers, hall managers and admins.  Staff appear only when they
switched ``is_listed`` on.  A profile that is not listed can still be opened by the atelier that has an
application from, or an assignment for, that person.  The mobile number is revealed **only** for an
accepted application / committed assignment between the two parties (``queries.relation_to``).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from flask import abort, render_template, request
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Event, StaffProfile
from app.modules.marketplace import bp, cards, common, forms, queries, services, web
from app.services import jalali, security, staffing
from app.services.constants import Role, Skill, UserStatus
from app.services.forms import Form
from app.services.http import paginate
from app.services.permissions import get_my_atelier, get_user, my_atelier_ids, role_required

VIEWERS = (Role.ATELIER_MANAGER, Role.HALL_MANAGER, Role.ADMIN)
PER_PAGE = 12
_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def instagram_handle(value: Optional[str]) -> Optional[str]:
    """A safe Instagram username from the profile field (``@name`` or a plain name), else ``None``."""
    handle = (value or "").strip().lstrip("@")
    return handle if _HANDLE.match(handle) else None


def _visible_profile(viewer: Any, user_id: int) -> tuple[StaffProfile, queries.Relation]:
    """The profile if *viewer* may see it (listed, or connected through an application / assignment), else 404."""
    profile = queries.get_profile(user_id)
    if profile is None:
        abort(404)
    relation = queries.relation_to(viewer, user_id)
    listed = bool(profile.is_listed and profile.user.status == UserStatus.ACTIVE)
    if not (listed or relation.connected):
        abort(404)
    return profile, relation


@bp.get("/talent")
@role_required(*VIEWERS)
def talent_index():
    """Search listed staff by skill, city, rating, name and availability on a Jalali date."""
    viewer = get_user()
    filters = forms.read_talent_filters(request.args)
    stmt = queries.talent_select(
        skill=filters.skill,
        city=filters.city or None,
        min_rating=filters.min_rating,
        on_date=filters.on_date,
        name=filters.name or None,
        sort=filters.sort,
    )
    page = paginate(stmt, per_page=PER_PAGE)
    return render_template(
        "marketplace/talent.html",
        page=page,
        cards=cards.build_talent_cards(page.items, viewer),
        filters=filters,
        cities=queries.talent_cities(),
        viewer=viewer,
    )


@bp.get("/talent/<int:user_id>")
@role_required(*VIEWERS)
def talent_profile(user_id: int):
    viewer = get_user()
    profile, relation = _visible_profile(viewer, user_id)
    reviews = paginate(queries.reviews_about_user(user_id), per_page=5)
    handle = instagram_handle(profile.instagram)
    return render_template(
        "marketplace/talent_profile.html",
        profile=profile,
        person=profile.user,
        relation=relation,
        reviews=reviews,
        portfolio=queries.public_portfolio(user_id),
        rating=queries.Rating(profile.rating_avg, profile.rating_count) if profile.rating_count else queries.NO_RATING,
        handle=handle,
        can_invite=viewer.role == Role.ATELIER_MANAGER and bool(my_atelier_ids(viewer)),
        gear=[
            label
            for flag, label in (
                (profile.owns_camera, "دوربین"),
                (profile.owns_flash, "فلاش"),
                (profile.owns_lens, "لنز"),
                (profile.owns_phone_light, "فون عکاسی (نور)"),
                (profile.owns_printer, "پرینتر"),
                (profile.owns_laptop, "لپ‌تاپ"),
            )
            if flag
        ],
    )


# --------------------------------------------------------------------------
# invite
# --------------------------------------------------------------------------


def _event_label(event: Event) -> str:
    hall = event.hall.name if event.hall is not None else "مراسم"
    return f"{hall} · {jalali.to_jalali_str(event.date, 'weekday_long', persian_digits=True)}"


def _render_invite(profile, events, *, values, errors, status=200, alert=None, check=None, needs_override=False, blocked=False):
    return (
        render_template(
            "marketplace/invite.html",
            profile=profile,
            person=profile.user,
            events=events,
            event_options=[(str(e.id), _event_label(e)) for e in events],
            role_options=[(skill, Skill.coerce(skill).label) for skill in profile.skills],
            problem=alert,
            events_url=web.optional_url("ateliers.events"),
            check=check,
            needs_override=needs_override,
            blocked=blocked,
            form=values,
            errors=errors,
        ),
        status,
    )


@bp.route("/talent/<int:user_id>/invite", methods=["GET", "POST"])
@role_required(Role.ATELIER_MANAGER)
def talent_invite(user_id: int):
    """Invite a person to one role of one of my upcoming events (they accept or decline from their page)."""
    get_my_atelier()  # no atelier yet -> onboarding
    manager = get_user()
    profile, _relation = _visible_profile(manager, user_id)
    person = profile.user
    events = queries.upcoming_events(sorted(my_atelier_ids()), security.today())
    by_id = {event.id: event for event in events}

    if request.method == "GET":
        event = by_id.get(request.args.get("event", type=int) or 0)
        role = request.args.get("role") if request.args.get("role") in profile.skills else (profile.skills[:1] or [""])[0]
        terms = None
        if event is not None and role:
            terms = event.atelier.preset_for(role)
        if terms is None and role and events:
            terms = events[0].atelier.preset_for(role)
        if terms is None:
            terms = {"base": profile.expected_base or 0, "commission_per_unit": profile.expected_commission_per_unit or 0}
        values = {"event": str(event.id) if event else "", "role": role, **forms.terms_to_form(terms, blank_zero=True)}
        return _render_invite(profile, events, values=values, errors={})

    form = Form(request.form)
    chosen = form.integer("event", "مراسم", required=True, min=1)
    event = by_id.get(chosen) if chosen is not None else None
    if chosen is not None and event is None:
        form.error("event", "مراسم انتخاب‌شده معتبر نیست.")
    role = form.choice("role", "نقش", profile.skills, required=True)
    terms = forms.read_terms(form)
    notes = forms.read_message(form, "notes", "پیام شما", max_len=300)
    override = form.boolean("override")
    check = None
    needs_override = blocked = False
    if event is not None and role:
        check = staffing.check_assignable(event, role, user=person)
        if check.duplicates:
            form.error("role", "این نفر همین نقش را از قبل در آن مراسم دارد.")
        needs_override = bool(check.conflicts or check.unavailable)
        if needs_override and event.atelier.block_double_booking:
            blocked = True
            form.error("override", "طبق تنظیمات اتلیه، دعوت‌کردن کسی که همان روز مشغول است مجاز نیست.")
        elif needs_override and not override:
            form.error("override", "این نفر همان روز مشغول است؛ برای ادامه تیک «دعوت با علم به تداخل» را بزنید.")
    if not form.ok or terms is None or event is None or not role:
        return _render_invite(
            profile, events, values=form.values, errors=form.errors, status=422,
            check=check, needs_override=needs_override, blocked=blocked,
        )
    try:
        services.invite_talent(
            manager, person, event, Skill.coerce(role), terms, notes, allow_conflict=bool(check and check.conflicts and override)
        )
        db.session.commit()
    except services.MarketplaceError as exc:
        db.session.rollback()
        return _render_invite(
            profile, events, values=form.values, errors={}, status=exc.status, alert=str(exc),
            check=check, needs_override=needs_override, blocked=blocked,
        )
    except IntegrityError:
        db.session.rollback()
        return _render_invite(
            profile, events, values=form.values, errors={}, status=409,
            alert="این نفر همین حالا برای این نقش افیش شد.", check=check,
        )
    return web.done(
        f"دعوت برای «{common.person_name(person)}» ارسال شد؛ پس از پذیرفتن او افیش قطعی می‌شود.",
        "marketplace.talent_profile",
        user_id=user_id,
    )
