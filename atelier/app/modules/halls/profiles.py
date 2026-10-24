"""Hall profiles: create, edit, photos, activate / deactivate, delete."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Event, Hall, HallAgreement, HallPhoto
from app.models.venue import MAX_HALL_PHOTOS
from app.modules.halls import bp, forms, queries, rules
from app.modules.halls.common import MANAGER, fail, my_halls, ok
from app.services import audit, security, uploads
from app.services.constants import CeremonyType
from app.services.forms import Form
from app.services.permissions import get_my_halls, get_user, hall_or_404, role_required

NAME_TAKEN = "تالاری با این نام قبلاً ثبت کرده‌اید؛ نام دیگری بنویسید."


def _apply(hall: Hall, data: forms.HallData, *, editing: bool) -> None:
    hall.name = data.name
    hall.city = data.city
    hall.address = data.address
    hall.phone = data.phone
    hall.notes = data.notes
    hall.map_url = data.map_url
    hall.logistics = data.logistics
    hall.default_ceremony_types = data.ceremony_types
    hall.default_rent = data.default_rent
    hall.single_atelier_per_day = data.single_atelier_per_day
    if editing:
        hall.is_active = data.is_active


def _render_form(mode: str, form: Form, status: int = 200, hall: Hall | None = None):
    return (
        render_template(
            "halls/hall_form.html",
            mode=mode,
            hall=hall,
            ceremony_choices=CeremonyType.choices(),
            logistics_labels=forms.LOGISTICS_LABELS,
            **form.context(),
        ),
        status,
    )


def _blank_form() -> Form:
    form = Form({})
    form.values.update({"is_active": True, "ceremony_types": [CeremonyType.WEDDING.value]})
    return form


def _create_hall() -> Any:
    """Shared POST handler of ``setup`` and ``hall_new``."""
    mode = "setup" if request.endpoint == "halls.setup" else "new"
    user = get_user()
    form = Form(request.form)
    data = forms.parse_hall(form)
    if data is not None and rules.hall_name_taken(user.id, data.name):
        form.error("name", NAME_TAKEN)
    if not form.ok or data is None:
        return _render_form(mode, form, 422)
    hall = Hall(manager=user, name=data.name, city=data.city)
    _apply(hall, data, editing=False)
    db.session.add(hall)
    try:
        db.session.flush()
    except IntegrityError:  # unique (manager, name): two taps at once
        db.session.rollback()
        form.error("name", NAME_TAKEN)
        return _render_form(mode, form, 422)
    audit.log(user, "hall.created", hall, {"name": hall.name, "city": hall.city})
    db.session.commit()
    flash("تالار ثبت شد. حالا می‌توانید مراسم‌ها را اضافه کنید.", "success")
    return redirect(url_for("halls.hall_show", hall_id=hall.id))


# --------------------------------------------------------------------------
# onboarding and list
# --------------------------------------------------------------------------


@bp.route("/setup", methods=["GET", "POST"])
@role_required(MANAGER, allow_pending=True)
def setup():
    """Onboarding: the first hall.  Managers who already have one are sent on to the dashboard."""
    if get_my_halls():
        return redirect(url_for("halls.dashboard"))
    if request.method == "POST":
        return _create_hall()
    return _render_form("setup", _blank_form())


@bp.get("/halls")
@role_required(MANAGER)
def index():
    halls = my_halls()
    today = security.today()
    ids = [hall.id for hall in halls]
    upcoming = dict(
        db.session.execute(
            sa.select(Event.hall_id, sa.func.count(Event.id))
            .where(Event.hall_id.in_(ids), Event.date >= today, Event.status.in_(queries.WORKING_STATUSES))
            .group_by(Event.hall_id)
        ).all()
    )
    free = dict(
        db.session.execute(
            sa.select(Event.hall_id, sa.func.count(Event.id))
            .where(
                Event.hall_id.in_(ids),
                Event.date >= today,
                Event.atelier_id.is_(None),
                Event.status.in_(queries.FREE_STATUSES),
            )
            .group_by(Event.hall_id)
        ).all()
    )
    covers: dict[int, str] = {}
    for photo in db.session.execute(
        sa.select(HallPhoto).where(HallPhoto.hall_id.in_(ids)).order_by(HallPhoto.hall_id, HallPhoto.sort_order, HallPhoto.id)
    ).scalars():
        covers.setdefault(photo.hall_id, photo.image_path)
    return render_template("halls/index.html", halls=halls, upcoming=upcoming, free=free, covers=covers)


@bp.route("/halls/new", methods=["GET", "POST"])
@role_required(MANAGER)
def hall_new():
    if request.method == "POST":
        return _create_hall()
    return _render_form("new", _blank_form())


# --------------------------------------------------------------------------
# one hall
# --------------------------------------------------------------------------


@bp.get("/halls/<int:hall_id>")
@role_required(MANAGER)
def hall_show(hall_id: int):
    hall = hall_or_404(hall_id)
    today = security.today()
    upcoming = list(
        db.session.execute(
            queries.events_of([hall.id])
            .where(queries.tab_condition("upcoming", today))
            .order_by(*queries.order_for("upcoming"))
            .limit(6)
        ).scalars().unique()
    )
    counts = queries.tab_counts([hall.id], today)
    agreements = list(
        db.session.execute(
            sa.select(HallAgreement)
            .where(HallAgreement.hall_id == hall.id, HallAgreement.terminated_at.is_(None))
            .order_by(HallAgreement.start_date.desc())
            .limit(5)
        ).scalars()
    )
    return render_template(
        "halls/hall_show.html",
        hall=hall,
        photos=list(hall.photos),
        upcoming=upcoming,
        pending=queries.pending_offer_counts([event.id for event in upcoming]),
        counts=counts,
        agreements=agreements,
        max_photos=MAX_HALL_PHOTOS,
        today=today,
        logistics_labels=forms.LOGISTICS_LABELS,
    )


@bp.route("/halls/<int:hall_id>/edit", methods=["GET", "POST"])
@role_required(MANAGER)
def hall_edit(hall_id: int):
    hall = hall_or_404(hall_id)
    user = get_user()
    if request.method == "GET":
        form = Form({})
        form.values.update(forms.hall_values(hall))
        return _render_form("edit", form, hall=hall)
    form = Form(request.form)
    data = forms.parse_hall(form, editing=True)
    if data is not None and rules.hall_name_taken(user.id, data.name, exclude_id=hall.id):
        form.error("name", NAME_TAKEN)
    if not form.ok or data is None:
        return _render_form("edit", form, 422, hall)
    was_active = hall.is_active
    _apply(hall, data, editing=True)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        form.error("name", NAME_TAKEN)
        return _render_form("edit", form, 422, hall)
    audit.log(user, "hall.updated", hall, {"name": hall.name, "active": hall.is_active, "was_active": was_active})
    db.session.commit()
    flash("مشخصات تالار ذخیره شد.", "success")
    return redirect(url_for("halls.hall_show", hall_id=hall.id))


@bp.post("/halls/<int:hall_id>/delete")
@role_required(MANAGER)
def hall_delete(hall_id: int):
    hall = hall_or_404(hall_id)
    try:
        paths = rules.delete_hall(hall, get_user())
        db.session.commit()
    except rules.RuleError as exc:
        db.session.rollback()
        return fail(str(exc), "halls.hall_show", hall_id=hall_id)
    for path in paths:
        uploads.delete_image(path)
    return ok("تالار حذف شد.", "halls.index")


# --------------------------------------------------------------------------
# photos
# --------------------------------------------------------------------------


@bp.post("/halls/<int:hall_id>/photos")
@role_required(MANAGER)
def hall_photo_add(hall_id: int):
    hall = hall_or_404(hall_id)
    user = get_user()
    files = [file for file in request.files.getlist("photo") if file and file.filename]
    caption = Form(request.form).text("caption", "توضیح عکس", max_len=200)
    if not files:
        return fail("عکسی انتخاب نشده است.", "halls.hall_show", hall_id=hall_id)
    added, problems = 0, []
    for file in files:
        try:
            with db.session.begin_nested():
                rules.add_photo(hall, user, file, caption if len(files) == 1 else None)
            added += 1
        except rules.RuleError as exc:
            problems.append(str(exc))
    if added:
        db.session.commit()
        flash(f"{_fa(added)} عکس اضافه شد.", "success")
    else:
        db.session.rollback()
    for message in dict.fromkeys(problems):
        flash(message, "error")
    return redirect(url_for("halls.hall_show", hall_id=hall_id, _anchor="photos"))


@bp.post("/halls/<int:hall_id>/photos/<int:photo_id>/remove")
@role_required(MANAGER)
def hall_photo_remove(hall_id: int, photo_id: int):
    hall = hall_or_404(hall_id)
    try:
        path = rules.remove_photo(hall, get_user(), photo_id)
        db.session.commit()
    except rules.RuleError as exc:
        db.session.rollback()
        return fail(str(exc), "halls.hall_show", hall_id=hall_id)
    uploads.delete_image(path)
    return ok("عکس حذف شد.", "halls.hall_show", hall_id=hall_id, _anchor="photos")


def _fa(number: int) -> str:
    from app.services.money import to_persian_digits

    return to_persian_digits(number)
