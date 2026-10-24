"""``/me/earnings`` - what I earned, line by line, with totals, a chart and a CSV export.

Staff only ever see their *own* payroll lines: every query goes through :mod:`app.modules.staff.earnings`, which
joins the afish row and filters ``Assignment.involving_user``; the detail routes load the line with an explicit
owner check (404 for anybody else's id).
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from flask import Response, render_template, request, url_for

from app.extensions import db
from app.models import AuditLog, PayrollLine
from app.modules.staff import bp, common, earnings, paytext, web
from app.services import audit, jalali, notifications, security
from app.services.constants import Role
from app.services.forms import Form
from app.services.http import paginate
from app.services.money import normalize_digits, to_persian_digits
from app.services.permissions import get_user, owned_or_404, role_required

STAFF = Role.STAFF
PER_PAGE = 15
FLAG_LIMIT_PER_DAY = 3
FLAG_MAX = 500


def _filters() -> tuple[int, int, str, dt.date, dt.date]:
    """``(jalali year, month or 0 for the whole year, paid filter, first day, last day)`` of the query string."""
    today = security.today()
    year = common.parse_year(request.args, today)
    raw_month = normalize_digits(request.args.get("m", "")).strip()
    month = int(raw_month) if raw_month.isdigit() and 1 <= int(raw_month) <= 12 else 0
    status = request.args.get("status", "all")
    if status not in earnings.PAID_FILTERS:
        status = "all"
    if month:
        first, last = jalali.jalali_month_range(year, month)
    else:
        first, last = jalali.jalali_month_range(year, 1)[0], jalali.jalali_month_range(year, 12)[1]
    return year, month, status, first, last


def _owned_line(line_id: int) -> PayrollLine:
    """My payroll line (an invoice that counts; the afish row is mine) or a 404."""

    def mine(line: PayrollLine, user) -> bool:
        return line.invoice.status in earnings.FINAL_STATUSES and line.assignment.person_user_id == user.id

    return owned_or_404(PayrollLine, line_id, mine)


@bp.get("/earnings")
@role_required(STAFF)
def earnings_page():
    user = get_user()
    year, month, status, first, last = _filters()
    summary = earnings.totals(user.id, first, last)
    page = paginate(earnings.lines_select(user.id, first, last, status), per_page=PER_PAGE)
    rows = earnings.build_rows(list(page.items))
    series = earnings.year_series(user.id, year)
    chart = earnings.build_chart(series)
    period = common.month_title(year, month) if month else f"سال {to_persian_digits(year)}"
    return render_template(
        "staff/earnings.html",
        year=year,
        month=month,
        status=status,
        period=period,
        summary=summary,
        outstanding=earnings.outstanding(user.id),
        page=page,
        rows=rows,
        chart=chart,
        series=series,
        prev_year_url=url_for("staff.earnings_page", y=year - 1, status=status),
        next_year_url=url_for("staff.earnings_page", y=year + 1, status=status),
        export_url=url_for("staff.earnings_csv", y=year, m=month or None, status=status),
        months=[(index + 1, name) for index, name in enumerate(jalali.JALALI_MONTHS)],
        formula=paytext.FORMULA,
        units_note=paytext.UNITS_EXPLAINED,
    )


@bp.get("/earnings.csv")
@role_required(STAFF)
def earnings_csv():
    user = get_user()
    year, month, status, first, last = _filters()
    lines = list(
        db.session.execute(earnings.lines_select(user.id, first, last, status).limit(earnings.CSV_LIMIT))
        .scalars()
        .unique()
    )
    label = f"{year}-{month:02d}" if month else str(year)
    response = Response(earnings.build_csv(lines).encode("utf-8"), mimetype="text/csv")
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="earnings-{label}.csv"'
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/earnings/<int:line_id>")
@role_required(STAFF)
def earning(line_id: int):
    line = _owned_line(line_id)
    (row,) = earnings.build_rows([line])
    user = get_user()
    recent = _recent_flags(user.id, line.id)
    return render_template(
        "staff/earning.html",
        row=row,
        line=line,
        event=row.event,
        hall=row.event.hall,
        atelier=row.event.atelier,
        formula=paytext.FORMULA,
        units_note=paytext.UNITS_EXPLAINED,
        term_lines=paytext.terms_lines(row.terms),
        flags_today=recent,
        flag_limit=FLAG_LIMIT_PER_DAY,
        flag_max=FLAG_MAX,
        form={},
        errors={},
    )


def _recent_flags(user_id: int, line_id: int) -> int:
    since = security.now() - dt.timedelta(hours=24)
    return int(
        db.session.scalar(
            sa.select(sa.func.count(AuditLog.id)).where(
                AuditLog.actor_id == user_id,
                AuditLog.action == "payroll.flagged",
                AuditLog.entity_type == PayrollLine.__tablename__,
                AuditLog.entity_id == line_id,
                AuditLog.created_at >= since,
            )
        )
        or 0
    )


@bp.post("/earnings/<int:line_id>/flag")
@role_required(STAFF)
def earning_flag(line_id: int):
    """«گزارش مشکل»: tell the atelier manager that this line looks wrong (a notification + an audit entry)."""
    user = get_user()
    line = _owned_line(line_id)
    target = url_for("staff.earning", line_id=line.id)
    form = Form(request.form)
    message = form.text("message", "توضیح مشکل", required=True, min_len=5, max_len=FLAG_MAX, multiline=True)
    if not form.ok:
        return web.refuse(form.errors["message"], target, status=422, code="validation_error", fields=form.errors)
    if _recent_flags(user.id, line.id) >= FLAG_LIMIT_PER_DAY:
        return web.refuse(
            "امروز چند بار برای این ردیف گزارش داده‌اید؛ منتظر پاسخ اتلیه بمانید یا فردا دوباره امتحان کنید.",
            target,
            status=429,
            code="rate_limited",
        )
    event = line.assignment.event
    audit.log(user, "payroll.flagged", line, {"message": message, "total": line.total, "invoice_id": line.invoice_id})
    manager_id = event.atelier.manager_id if event.atelier is not None else None
    link = web.optional_url("accounting.payroll")
    when = jalali.to_jalali_str(event.date, "long", persian_digits=True)
    notifications.notify(
        manager_id,
        "payroll_flagged",
        f"{line.person_name or user.display_name} دربارهٔ حقوق یک مراسم پرسش دارد",
        f"مراسم {event.hall.name if event.hall else ''} - {when}\n{message}",
        link,
        entity=line,
    )
    db.session.commit()
    return web.done("گزارش شما برای مدیر اتلیه فرستاده شد.", target)
