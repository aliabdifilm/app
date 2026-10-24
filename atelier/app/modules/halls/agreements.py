"""Hall-atelier agreements: period, exclusivity, rent per event or revenue share, deposit, contact."""

from __future__ import annotations

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.orm import contains_eager

from app.extensions import db
from app.models import Atelier, Hall, HallAgreement
from app.modules.halls import bp, forms, rules, search
from app.modules.halls.common import ERRORS, MANAGER, describe_error, fail, hall_filter, hall_options, my_halls, ok
from app.services import security
from app.services.forms import Form
from app.services.http import paginate
from app.services.permissions import get_user, hall_or_404, my_hall_ids, owned_or_404, role_required

PER_PAGE = 15
TABS = (("active", "فعال"), ("past", "پایان‌یافته"), ("all", "همه"))


def _agreement_or_404(agreement_id: int) -> HallAgreement:
    return owned_or_404(HallAgreement, agreement_id, lambda agreement, user: agreement.hall_id in my_hall_ids(user))


@bp.get("/agreements")
@role_required(MANAGER)
def agreements():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    today = security.today()
    tab = request.args.get("tab", "active")
    if tab not in dict(TABS):
        tab = "active"
    current = sa.and_(
        HallAgreement.terminated_at.is_(None),
        sa.or_(HallAgreement.end_date.is_(None), HallAgreement.end_date >= today),
    )
    stmt = (
        sa.select(HallAgreement)
        .join(Hall, Hall.id == HallAgreement.hall_id)
        .join(Atelier, Atelier.id == HallAgreement.atelier_id)
        .where(HallAgreement.hall_id.in_(ids))
        .options(contains_eager(HallAgreement.hall), contains_eager(HallAgreement.atelier))
    )
    if tab == "active":
        stmt = stmt.where(current)
    elif tab == "past":
        stmt = stmt.where(sa.not_(current))
    page = paginate(stmt.order_by(HallAgreement.start_date.desc(), HallAgreement.id.desc()), per_page=PER_PAGE)
    return render_template(
        "halls/agreements.html",
        halls=halls,
        selected=selected,
        hall_choices=hall_options(halls),
        page=page,
        tab=tab,
        tabs=TABS,
        today=today,
        status_of=rules.agreement_status,
        hall_arg=selected.id if selected else None,
    )


def _form_page(form: Form, halls, atelier, agreement=None, status: int = 200):
    return (
        render_template(
            "halls/agreement_form.html",
            halls=halls,
            hall_choices=hall_options(halls),
            atelier=atelier,
            agreement=agreement,
            today=security.today(),
            **form.context(),
        ),
        status,
    )


@bp.route("/agreements/new", methods=["GET", "POST"])
@role_required(MANAGER)
def agreement_new():
    halls = my_halls()
    source = request.form if request.method == "POST" else request.args
    raw = (source.get("atelier") or source.get("atelier_id") or "").strip()
    atelier = search.active_atelier(int(raw)) if raw.isdigit() else None
    if atelier is None:
        flash("ابتدا اتلیه را از فهرست اتلیه‌ها انتخاب کنید.", "info")
        return redirect(url_for("halls.directory"))
    if request.method == "GET":
        form = Form({})
        selected, _ = hall_filter(halls)
        form.values.update({"hall_id": selected.id if selected else halls[0].id, "start_date": security.today().isoformat()})
        return _form_page(form, halls, atelier)
    form = Form(request.form)
    if len(halls) == 1:
        hall = halls[0]
    else:
        hall_raw = form.raw("hall_id")
        hall = hall_or_404(int(hall_raw)) if hall_raw.isdigit() else None
        if hall is None:
            form.error("hall_id", "تالار را انتخاب کنید.")
    form.values["hall_id"] = hall.id if hall else ""
    data = forms.parse_agreement(form, atelier)
    if not form.ok or data is None or hall is None:
        return _form_page(form, halls, atelier, status=422)
    try:
        agreement = rules.create_agreement(hall, get_user(), data)
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        form.error("start_date", describe_error(exc))
        return _form_page(form, halls, atelier, status=422)
    flash("توافق‌نامه ثبت شد و مدیر اتلیه باخبر شد.", "success")
    return redirect(url_for("halls.agreements", hall=agreement.hall_id))


@bp.route("/agreements/<int:agreement_id>/edit", methods=["GET", "POST"])
@role_required(MANAGER)
def agreement_edit(agreement_id: int):
    agreement = _agreement_or_404(agreement_id)
    halls = my_halls()
    if agreement.terminated_at is not None:
        return fail("این توافق‌نامه پایان یافته و قابل ویرایش نیست.", "halls.agreements", hall=agreement.hall_id, tab="past")
    atelier = agreement.atelier
    if request.method == "GET":
        form = Form({})
        form.values.update(forms.agreement_values(agreement))
        form.values["hall_id"] = agreement.hall_id
        return _form_page(form, halls, atelier, agreement)
    form = Form(request.form)
    data = forms.parse_agreement(form, atelier)
    if not form.ok or data is None:
        return _form_page(form, halls, atelier, agreement, 422)
    try:
        rules.update_agreement(agreement, get_user(), data)
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        form.error("start_date", describe_error(exc))
        return _form_page(form, halls, atelier, agreement, 422)
    return ok("توافق‌نامه ذخیره شد و مدیر اتلیه باخبر شد.", "halls.agreements", hall=agreement.hall_id)


@bp.post("/agreements/<int:agreement_id>/end")
@role_required(MANAGER)
def agreement_end(agreement_id: int):
    agreement = _agreement_or_404(agreement_id)
    try:
        rules.terminate_agreement(agreement, get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.agreements")
    return ok("توافق‌نامه پایان یافت.", "halls.agreements", hall=agreement.hall_id, tab="past")


