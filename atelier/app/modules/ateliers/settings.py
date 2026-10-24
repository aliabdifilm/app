"""Atelier profile and settings: identity, logo, venue rents per hall, default pay presets."""

from __future__ import annotations

import re
from typing import Any, Optional

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Atelier, AtelierHall, Event, EventOffer, Hall
from app.modules.ateliers import bp, forms
from app.modules.ateliers.common import MANAGER, current_atelier
from app.services import audit, uploads
from app.services.constants import ROLE_DISPLAY_ORDER
from app.services.forms import Form
from app.services.money import MAX_TOMAN
from app.services.permissions import (
    get_my_atelier,
    get_my_ateliers,
    get_user,
    my_atelier_ids,
    owned_or_404,
    role_required,
)

PHONE_RE = re.compile(r"^[0-9+\-\s()]{5,20}$")
SESSION_ATELIER_KEY = "atelier_id"
SECTION_ANCHORS = {"profile": "profile", "halls": "halls", "presets": "presets"}


# --------------------------------------------------------------------------
# reading the profile form (shared by setup and settings)
# --------------------------------------------------------------------------


def _read_profile(form: Form, *, with_policy: bool) -> dict[str, Any]:
    name = form.text("name", "نام اتلیه", required=True, max_len=150)
    city = form.text("city", "شهر", required=True, max_len=80)
    phone = form.text("phone", "تلفن", max_len=30, digits=True)
    if phone and not PHONE_RE.match(phone):
        form.error("phone", "تلفن نامعتبر است؛ فقط رقم، + و - وارد کنید.")
    description = form.text("description", "توضیحات", max_len=1000, multiline=True)
    rent = form.money("default_venue_rent", "اجارهٔ پیش‌فرض باغ/تالار", default=0)
    data: dict[str, Any] = {
        "name": name,
        "city": city,
        "phone": phone,
        "description": description,
        "default_venue_rent": rent if rent is not None else 0,
    }
    if with_policy:
        data["block_double_booking"] = form.boolean("block_double_booking")
    return data


def _read_logo(form: Form) -> Optional[uploads.StoredImage]:
    """The uploaded logo (stored on disk already) or ``None``; an error is recorded on the form."""
    file = request.files.get("logo")
    if file is None or not file.filename:
        return None
    try:
        return uploads.save_image(file, "logo", min_side=64)
    except uploads.UploadError as exc:
        form.error("logo", str(exc))
        return None


# --------------------------------------------------------------------------
# onboarding: create an atelier
# --------------------------------------------------------------------------


@bp.route("/setup", methods=["GET", "POST"])
@role_required(MANAGER, allow_pending=True)
def setup():
    user = get_user()
    existing = get_my_atelier(required=False)  # onboarding page: must not redirect to itself
    adding = request.args.get("new") == "1"
    if request.method == "GET":
        if existing is not None and not adding:
            return redirect(url_for("ateliers.settings"))
        return render_template("ateliers/setup.html", form={}, errors={}, adding=adding, first=existing is None)

    form = Form(request.form)
    data = _read_profile(form, with_policy=False)
    stored = _read_logo(form)
    if not form.ok:
        if stored:
            uploads.delete_image(stored.path)
        return render_template("ateliers/setup.html", adding=adding, first=existing is None, **form.context()), 422
    atelier = Atelier(manager=user, logo_path=stored.path if stored else None, **data)
    db.session.add(atelier)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        if stored:
            uploads.delete_image(stored.path)
        form.error("name", "اتلیه‌ای با این نام قبلاً ثبت کرده‌اید.")
        return render_template("ateliers/setup.html", adding=adding, first=existing is None, **form.context()), 422
    audit.log(user, "atelier.created", atelier, {"name": atelier.name, "city": atelier.city})
    db.session.commit()
    session[SESSION_ATELIER_KEY] = atelier.id
    flash("اتلیه ثبت شد. حالا نیروهای ثابت و پیش‌فرض حقوق را تنظیم کنید.", "success")
    return redirect(url_for("ateliers.dashboard"))


# --------------------------------------------------------------------------
# settings page
# --------------------------------------------------------------------------


def _candidate_halls(atelier: Atelier, taken: set[int]) -> list[Hall]:
    """Halls this atelier may price: halls it has worked or been offered, and active halls in its city."""
    worked = sa.select(Event.hall_id).where(Event.atelier_id == atelier.id)
    offered = sa.select(Event.hall_id).join(EventOffer, EventOffer.event_id == Event.id).where(
        EventOffer.atelier_id == atelier.id
    )
    stmt = (
        sa.select(Hall)
        .where(
            Hall.is_active.is_(True),
            sa.or_(Hall.city == atelier.city, Hall.id.in_(worked), Hall.id.in_(offered)),
        )
        .order_by(Hall.city, Hall.name)
        .limit(200)
    )
    if taken:
        stmt = stmt.where(Hall.id.not_in(sorted(taken)))
    return list(db.session.execute(stmt).scalars())


def _profile_values(atelier: Atelier) -> dict[str, Any]:
    return {
        "name": atelier.name,
        "city": atelier.city,
        "phone": atelier.phone or "",
        "description": atelier.description or "",
        "default_venue_rent": atelier.default_venue_rent,
        "block_double_booking": atelier.block_double_booking,
    }


def _preset_values(atelier: Atelier) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for role in ROLE_DISPLAY_ORDER:
        values.update(forms.terms_to_form(atelier.preset_for(role), forms.role_suffix(role), blank_zero=True))
    return values


def _render_settings(atelier: Atelier, *, section: Optional[str] = None, form: Optional[Form] = None, status: int = 200):
    links = list(
        db.session.execute(
            sa.select(AtelierHall)
            .where(AtelierHall.atelier_id == atelier.id)
            .join(Hall, Hall.id == AtelierHall.hall_id)
            .options(joinedload(AtelierHall.hall))
            .order_by(Hall.name)
        ).scalars()
    )
    values = {**_profile_values(atelier), **_preset_values(atelier)}
    for link in links:
        values[f"rent_{link.id}"] = link.rent_amount if link.rent_amount is not None else ""
    errors: dict[str, str] = {}
    if form is not None:
        values.update(form.values)
        errors = form.errors
    return (
        render_template(
            "ateliers/settings.html",
            atelier=atelier,
            ateliers=get_my_ateliers(),
            hall_links=links,
            hall_options=[
                (hall.id, f"{hall.name} · {hall.city}" if hall.city else hall.name)
                for hall in _candidate_halls(atelier, {link.hall_id for link in links})
            ],
            roles=ROLE_DISPLAY_ORDER,
            suffix=forms.role_suffix,
            form=values,
            errors=errors,
            section=section,
        ),
        status,
    )


@bp.get("/settings")
@role_required(MANAGER)
def settings():
    return _render_settings(current_atelier())


@bp.post("/settings/profile")
@role_required(MANAGER)
def settings_profile():
    atelier = current_atelier()
    form = Form(request.form)
    data = _read_profile(form, with_policy=True)
    stored = _read_logo(form)
    remove_logo = form.boolean("remove_logo")
    if not form.ok:
        if stored:
            uploads.delete_image(stored.path)
        return _render_settings(atelier, section="profile", form=form, status=422)
    before = _profile_values(atelier)
    for key, value in data.items():
        setattr(atelier, key, value)
    old_logo: Optional[str] = None
    if stored:
        old_logo, atelier.logo_path = atelier.logo_path, stored.path
    elif remove_logo and atelier.logo_path:
        old_logo, atelier.logo_path = atelier.logo_path, None
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        if stored:
            uploads.delete_image(stored.path)
        form.error("name", "اتلیه‌ای با این نام قبلاً ثبت کرده‌اید.")
        return _render_settings(db.session.get(Atelier, atelier.id), section="profile", form=form, status=422)
    changed = {key: [before[key], data[key]] for key in data if before.get(key) != data[key]}
    audit.log(get_user(), "atelier.updated", atelier, {"changed": sorted(changed), "logo": bool(stored)})
    db.session.commit()
    uploads.delete_image(old_logo)  # only after the new state is safe in the database
    flash("مشخصات اتلیه ذخیره شد.", "success")
    return redirect(url_for("ateliers.settings", _anchor="profile"))


@bp.post("/settings/halls")
@role_required(MANAGER)
def settings_halls():
    """Save the rent per hall; a blank field means "use the atelier default"."""
    atelier = current_atelier()
    form = Form(request.form)
    links = list(db.session.execute(sa.select(AtelierHall).where(AtelierHall.atelier_id == atelier.id)).scalars())
    updates: dict[int, Optional[int]] = {}
    for link in links:
        updates[link.id] = form.money(f"rent_{link.id}", "اجارهٔ این تالار", min=0, max=MAX_TOMAN)
    new_hall: Optional[Hall] = None
    new_rent: Optional[int] = None
    new_id = forms.read_id(form, "new_hall_id")
    if new_id is not None:
        new_hall = db.session.get(Hall, new_id)
        if new_hall is None or not new_hall.is_active or any(link.hall_id == new_id for link in links):
            form.error("new_hall_id", "این تالار قابل افزودن نیست.")
        new_rent = form.money("new_rent", "اجارهٔ تالار جدید", min=0, max=MAX_TOMAN)
    if not form.ok:
        return _render_settings(atelier, section="halls", form=form, status=422)
    for link in links:
        link.rent_amount = updates[link.id]
    if new_hall is not None:
        db.session.add(AtelierHall(atelier=atelier, hall=new_hall, rent_amount=new_rent))
    try:
        db.session.flush()
    except IntegrityError:  # a double submit added the same hall twice
        db.session.rollback()
        flash("این تالار قبلاً اضافه شده است.", "warning")
        return redirect(url_for("ateliers.settings", _anchor="halls"))
    audit.log(
        get_user(),
        "atelier.hall_rents_changed",
        atelier,
        {"rents": {str(link.hall_id): link.rent_amount for link in links}, "added": new_hall.id if new_hall else None},
    )
    db.session.commit()
    flash("اجارهٔ تالارها ذخیره شد.", "success")
    return redirect(url_for("ateliers.settings", _anchor="halls"))


@bp.post("/settings/halls/<int:link_id>/delete")
@role_required(MANAGER)
def settings_hall_delete(link_id: int):
    link = owned_or_404(AtelierHall, link_id, lambda row, user: row.atelier_id in my_atelier_ids(user))
    audit.log(get_user(), "atelier.hall_removed", link.atelier, {"hall_id": link.hall_id, "rent": link.rent_amount})
    db.session.delete(link)
    db.session.commit()
    flash("تالار از فهرست اجاره‌ها حذف شد؛ از این پس اجارهٔ پیش‌فرض اتلیه اعمال می‌شود.", "success")
    return redirect(url_for("ateliers.settings", _anchor="halls"))


@bp.post("/settings/presets")
@role_required(MANAGER)
def settings_presets():
    """Default pay terms per role.  They pre-fill the afish form; existing afish rows never change."""
    atelier = current_atelier()
    form = Form(request.form)
    presets: dict[str, dict[str, Any]] = {}
    for role in ROLE_DISPLAY_ORDER:
        terms = forms.read_terms(form, forms.role_suffix(role), role.label)
        if terms is not None and not forms.terms_are_empty(terms):
            presets[role.value] = terms
    if not form.ok:
        return _render_settings(atelier, section="presets", form=form, status=422)
    before = dict(atelier.default_pay_presets or {})
    atelier.default_pay_presets = presets
    db.session.flush()
    audit.log(get_user(), "atelier.presets_changed", atelier, {"old": before, "new": presets})
    db.session.commit()
    flash("پیش‌فرض حقوق نقش‌ها ذخیره شد. افیش‌های قبلی تغییری نکردند.", "success")
    return redirect(url_for("ateliers.settings", _anchor="presets"))
