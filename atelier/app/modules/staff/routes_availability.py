"""``/me/availability`` - mark the days I can or cannot work (read by the afish board, staffing and the talent search)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from flask import render_template, request, url_for

from app.extensions import db
from app.modules.staff import availability, bp, common, web
from app.services import audit, jalali, security
from app.services.constants import Role
from app.services.forms import Form
from app.services.http import json_error, wants_json
from app.services.money import normalize_digits, to_persian_digits
from app.services.permissions import get_user, role_required

STAFF = Role.STAFF
WEEKDAYS = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه")


def _parse_days(raw: list[str]) -> tuple[list[dt.date], int]:
    """ISO / Jalali dates from the checkboxes; returns ``(valid days, how many entries were junk)``."""
    days: list[dt.date] = []
    junk = 0
    for text in raw:
        text = normalize_digits(text).strip()
        try:
            if "-" in text and text[:4].isdigit() and int(text[:4]) > 1700:
                days.append(dt.date.fromisoformat(text))
            else:
                days.append(jalali.parse_jalali(text))
        except (ValueError, TypeError, OverflowError):
            junk += 1
    return days, junk


def _page_context(
    user_id: int, year: int, month: int, *, form: Optional[dict] = None, errors: Optional[dict] = None
) -> dict[str, Any]:
    today = security.today()
    first, last = common.month_bounds(year, month)
    marks = availability.marks_between(user_id, first, last)
    busy = availability.busy_days(user_id, first, last)
    horizon = today + dt.timedelta(days=availability.HORIZON_DAYS)
    prev_y, prev_m = common.shift_month(year, month, -1)
    next_y, next_m = common.shift_month(year, month, 1)
    cy, cm, _ = common.jalali_today(today)
    unavailable = sum(1 for mark in marks.values() if mark.status.value == "unavailable")
    free = sum(1 for mark in marks.values() if mark.status.value == "available")
    return {
        "year": year,
        "month": month,
        "title": common.month_title(year, month),
        "grid": jalali.month_grid(year, month),
        "marks": marks,
        "busy": busy,
        "today": today,
        "horizon": horizon,
        "weekdays": WEEKDAYS,
        "prev_url": url_for("staff.availability_page", y=prev_y, m=prev_m),
        "next_url": url_for("staff.availability_page", y=next_y, m=next_m),
        "this_month_url": None if (year, month) == (cy, cm) else url_for("staff.availability_page"),
        "unavailable_count": unavailable,
        "free_count": free,
        "form": form or {},
        "errors": errors or {},
        "max_range": availability.MAX_RANGE_DAYS,
    }


@bp.get("/availability")
@role_required(STAFF)
def availability_page():
    user = get_user()
    year, month = common.parse_month(request.args, security.today())
    return render_template("staff/availability.html", **_page_context(user.id, year, month))


def _log(user: Any, action: str, result: availability.ApplyResult, days: list[dt.date], source: str) -> None:
    audit.log(
        user,
        "availability.updated",
        None,
        {
            "action": action,
            "source": source,
            "marked": result.marked,
            "cleared": result.cleared,
            "skipped": result.skipped,
            "first": min(days).isoformat() if days else None,
            "last": max(days).isoformat() if days else None,
        },
    )


def _result_response(result: availability.ApplyResult, action: str, target: str):
    message = availability.summary_text(result, action)
    level = "success" if result.changed else "warning"
    return web.done(
        message,
        target,
        level=level,
        data={
            "marked": result.marked,
            "cleared": result.cleared,
            "skipped": result.skipped,
        },
    )


@bp.post("/availability")
@role_required(STAFF)
def availability_save():
    """Apply one action to the checked days: «مشغول‌ام» / «آزادم» / «پاک‌کردن علامت»."""
    user = get_user()
    today = security.today()
    form = Form(request.form)
    year, month = common.parse_month(request.form, today)
    target = url_for("staff.availability_page", y=year, m=month)
    action = form.choice("action", "عملیات", availability.ACTIONS, required=True)
    note = form.text("note", "توضیح", max_len=availability.MAX_NOTE)
    days, junk = _parse_days(form.getlist("days"))
    if not form.ok:
        return web.refuse(next(iter(form.errors.values())), target, status=422, code="validation_error", fields=form.errors)
    if not days:
        return web.refuse("ابتدا یک یا چند روز را انتخاب کنید.", target, status=422, code="no_days", level="warning")
    if len(days) > availability.MAX_DAYS_PER_REQUEST or junk:
        return web.refuse("تعداد یا قالب روزهای انتخاب‌شده معتبر نیست.", target, status=422, code="bad_days")
    result = availability.apply_marks(user.id, days, action, today=today, note=note)
    _log(user, action, result, days, "calendar")
    db.session.commit()
    return _result_response(result, action, target)


@bp.post("/availability/range")
@role_required(STAFF)
def availability_range():
    """Mark a whole range, optionally only some weekdays (e.g. every Friday of the next three months)."""
    user = get_user()
    today = security.today()
    form = Form(request.form)
    year, month = common.parse_month(request.form, today)
    start = form.date("start", "از تاریخ", required=True, min=today)
    end = form.date("end", "تا تاریخ", required=True, min=today)
    action = form.choice("action", "عملیات", availability.ACTIONS, required=True)
    note = form.text("note", "توضیح", max_len=availability.MAX_NOTE)
    weekday_numbers = form.int_list("weekdays")
    form.values["weekdays"] = [str(n) for n in weekday_numbers]
    days: list[dt.date] = []
    if form.ok:
        if any(n < 0 or n > 6 for n in weekday_numbers):
            form.error("weekdays", "روز هفته نامعتبر است.")
        else:
            try:
                days = availability.expand_range(start, end, weekday_numbers or None)
            except ValueError as exc:
                text = (
                    "تاریخ پایان نباید قبل از تاریخ شروع باشد."
                    if "inverted" in str(exc)
                    else f"بازهٔ انتخابی حداکثر {to_persian_digits(availability.MAX_RANGE_DAYS)} روز می‌تواند باشد."
                )
                form.error("end", text)
    if form.ok and not days:
        form.error("weekdays", "در این بازه هیچ روزی با روزهای هفتهٔ انتخابی نمی‌خورد.")
    if not form.ok:
        db.session.rollback()
        if wants_json():
            return json_error(next(iter(form.errors.values())), 422, "validation_error", fields=form.errors)
        context = _page_context(user.id, year, month, form=form.values, errors=form.errors)
        context["range_open"] = True
        return render_template("staff/availability.html", **context), 422
    result = availability.apply_marks(user.id, days, action, today=today, note=note)
    _log(user, action, result, days, "range")
    db.session.commit()
    return _result_response(result, action, url_for("staff.availability_page", y=year, m=month))
