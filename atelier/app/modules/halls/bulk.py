"""Adding many events at once (a whole season): the multi-date page and the rule-expansion endpoint."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from app.extensions import db
from app.modules.halls import bp, dates, forms, rules
from app.modules.halls.common import ERRORS, MANAGER, describe_error, hall_filter, hall_options, my_halls
from app.services import jalali, security
from app.services.constants import CeremonyType
from app.services.forms import Form
from app.services.http import json_body, json_error, json_ok
from app.services.money import normalize_digits, to_persian_digits
from app.services.permissions import get_user, hall_or_404, role_required


def _page(form: Form, halls, status: int = 200):
    today = security.today()
    return (
        render_template(
            "halls/bulk.html",
            halls=halls,
            hall_choices=hall_options(halls),
            ceremony_choices=CeremonyType.choices(),
            weekday_choices=dates.WEEKDAY_CHOICES,
            today=today,
            max_dates=dates.MAX_BULK_DATES,
            expand_url=url_for("halls.api_dates_expand"),
            **form.context(),
        ),
        status,
    )


def _pick_hall(form: Form, halls):
    if len(halls) == 1:
        return halls[0]
    raw = form.raw("hall_id")
    if not raw.isdigit():
        form.error("hall_id", "تالار را انتخاب کنید.")
        return None
    return hall_or_404(int(raw))


@bp.route("/events/bulk", methods=["GET", "POST"])
@role_required(MANAGER)
def bulk_new():
    halls = my_halls()
    if request.method == "GET":
        form = Form({})
        selected, _ = hall_filter(halls)
        form.values.update(
            {
                "hall_id": selected.id if selected else halls[0].id,
                "ceremony_type": CeremonyType.WEDDING.value,
                "rule_mode": "weekly",
                "rule_weekdays": [str(day) for day in dates.DEFAULT_WEEKDAYS],
                "rule_short": "clamp",
                "dates": [],
            }
        )
        return _page(form, halls)
    form = Form(request.form)
    hall = _pick_hall(form, halls)
    form.values["hall_id"] = hall.id if hall else form.raw("hall_id")
    parsed = forms.parse_bulk(form)
    if not form.ok or parsed is None or hall is None:
        return _page(form, halls, 422)
    try:
        outcome = rules.create_events(hall, get_user(), parsed.days, parsed.times, parsed.data)
    except ERRORS as exc:
        db.session.rollback()
        form.error("dates", describe_error(exc))
        return _page(form, halls, 422)
    db.session.commit()
    created = len(outcome.created)
    if created:
        flash(f"{to_persian_digits(created)} مراسم در «{hall.name}» ثبت شد. حالا آن‌ها را به اتلیه‌ها پیشنهاد دهید.", "success")
    else:
        flash("هیچ مراسم تازه‌ای ثبت نشد.", "info")
    if outcome.duplicates:
        flash(f"{to_persian_digits(len(outcome.duplicates))} مورد از قبل ثبت شده بود و دوباره ساخته نشد.", "warning")
    if outcome.past:
        flash(f"{to_persian_digits(len(outcome.past))} تاریخ گذشته نادیده گرفته شد.", "warning")
    if outcome.too_far:
        flash(f"{to_persian_digits(len(outcome.too_far))} تاریخ خیلی دور (بیش از سه سال) نادیده گرفته شد.", "warning")
    return redirect(url_for("halls.events", tab="free", hall=hall.id))


@bp.post("/api/dates/expand")
@role_required(MANAGER)
def api_dates_expand():
    """Turn a repeat rule into dates, with the same code the form uses on submit.

    Body: ``{"mode": "weekly", "from": "1404/01/01", "to": "1404/06/31", "weekdays": [5, 6]}`` or
    ``{"mode": "monthly", "from": ..., "to": ..., "days": [1, 15, 30], "short": "clamp"}``.
    Answers ``{"ok": true, "data": {"dates": ["2025-03-20", ...], "count": n}}``.
    """
    body = json_body()
    mode = body.get("mode")
    fields: dict[str, str] = {}
    parsed = {}
    for key in ("from", "to"):
        try:
            parsed[key] = dates.parse_token(str(body.get(key, "")))
        except ValueError as exc:
            fields[key] = str(exc)
    if fields:
        return json_error("تاریخ‌های بازه را درست وارد کنید.", 422, fields=fields)
    try:
        if mode == "weekly":
            raw = body.get("weekdays")
            weekdays = [int(normalize_digits(str(item))) for item in raw if str(item).strip().isdigit()] if isinstance(raw, list) else []
            found = dates.weekly(parsed["from"], parsed["to"], weekdays)
        elif mode == "monthly":
            raw = body.get("days")
            days = [int(normalize_digits(str(item))) for item in raw if normalize_digits(str(item)).isdigit()] if isinstance(raw, list) else []
            short = body.get("short") if body.get("short") in ("clamp", "skip") else "clamp"
            found = dates.monthly(parsed["from"], parsed["to"], days, short_month=short)
        else:
            return json_error("الگوی تکرار نامعتبر است.", 422, fields={"mode": "نامعتبر"})
    except ValueError as exc:
        return json_error(str(exc), 422)
    if len(found) > dates.MAX_BULK_DATES:
        return json_error(f"بیش از {to_persian_digits(dates.MAX_BULK_DATES)} تاریخ ساخته می‌شود؛ بازهٔ کوتاه‌تری بگیرید.", 422)
    today = security.today()
    return json_ok(
        {
            "dates": [day.isoformat() for day in found],
            "count": len(found),
            "past": sum(1 for day in found if day < today),
            "first_jalali": jalali.to_jalali_str(found[0], "long", persian_digits=True) if found else "",
        }
    )
