"""Routes of the ``accounting`` module (atelier managers only, own ateliers only)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import sqlalchemy as sa
from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, Invoice, PayrollLine
from app.modules.accounting import bp, exports, forms, queries, reports
from app.services import eventbus, jalali, ledger, security
from app.services.constants import EventStatus, InvoiceStatus, PayoutMethod, Role
from app.services.forms import Form, clean_text
from app.services.http import json_body, json_error, json_ok, paginate, wants_json
from app.services.money import to_persian_digits
from app.services.permissions import (
    event_or_404,
    get_my_atelier,
    get_my_ateliers,
    get_user,
    invoice_or_404,
    my_atelier_ids,
    role_required,
    set_current_atelier,
)
from app.services.security import safe_next

logger = logging.getLogger(__name__)

CARDS_PER_PAGE = 12
PEOPLE_PER_PAGE = 10
EVENT_ROWS_PER_PAGE = 15
MAX_BULK = 200

STATUS_TONES = {
    "draft": "neutral",
    "open": "info",
    "offered": "warning",
    "confirmed": "accent",
    "done": "info",
    "invoiced": "success",
    "cancelled": "danger",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _atelier():
    """The atelier the page is about: ``?atelier=<id>`` (404 unless mine), else the current one."""
    raw = request.values.get("atelier", type=int)
    if raw is not None:
        atelier = get_my_atelier(raw)
        set_current_atelier(atelier)
        return atelier
    return get_my_atelier()


def _invoice_of(event: Event) -> Optional[Invoice]:
    return db.session.execute(sa.select(Invoice).where(Invoice.event_id == event.id)).scalar_one_or_none()


def _check_invoiceable(event: Event, invoice: Optional[Invoice]) -> None:
    if event.atelier_id is None or event.status not in ledger.INVOICEABLE_EVENT_STATUSES:
        abort(409, f"برای مراسمی که «{event.status.label}» است هنوز نمی‌توان فاکتور ثبت کرد.")
    if invoice is not None and invoice.status == InvoiceStatus.VOID:
        abort(409, "فاکتور این مراسم باطل شده است و دیگر قابل ویرایش نیست.")


def _index_url(event: Optional[Event] = None, **extra: Any) -> str:
    """The accounting page, on the month of *event* when given (so a saved card is where the user left it)."""
    if event is not None:
        jy, jm, _ = jalali.to_jalali(event.date)
        extra.update(y=jy, m=jm, _anchor=f"event-{event.id}")
    return url_for("accounting.index", **extra)


def _back(default: str) -> str:
    return safe_next(request.form.get("next") or request.args.get("next"), default)


def _status_filter(raw: str) -> str:
    return raw if (raw == queries.AWAITING or EventStatus.is_valid(raw)) else ""


def _tabs(period: queries.Period) -> list[dict[str, str]]:
    args = period.args
    return [
        {"key": "invoices", "label": "فاکتورها", "url": url_for("accounting.index", **args)},
        {"key": "payroll", "label": "حقوق نیروها", "url": url_for("accounting.payroll_index", **args)},
        {"key": "reports", "label": "گزارش‌ها", "url": url_for("accounting.reports_index", y=period.jy)},
    ]


def _period_context(atelier, period: queries.Period, today) -> dict[str, Any]:
    ateliers = get_my_ateliers()
    return {
        "atelier": atelier,
        "ateliers": ateliers if len(ateliers) > 1 else [],
        "period": period,
        "prev_period": period.shifted(-1),
        "next_period": period.shifted(+1),
        "years": queries.year_choices(atelier.id, today),
        "month_choices": [(0, "همهٔ ماه‌ها")] + [(i + 1, name) for i, name in enumerate(jalali.JALALI_MONTHS)],
        "status_tones": STATUS_TONES,
    }


# --------------------------------------------------------------------------
# event cards
# --------------------------------------------------------------------------


@bp.get("/")
@role_required(Role.ATELIER_MANAGER)
def index():
    atelier = _atelier()
    today = security.today()
    period = queries.resolve_period(request.args, today)
    status = _status_filter(request.args.get("status", ""))
    q = clean_text(request.args.get("q", ""))[:80]
    page = paginate(queries.events_query(atelier.id, period, status, q, today), per_page=CARDS_PER_PAGE)
    cards = []
    for event in page.items:
        invoice = event.invoice
        can_invoice = event.status in ledger.INVOICEABLE_EVENT_STATUSES and (
            invoice is None or invoice.status != InvoiceStatus.VOID
        )
        cards.append(
            {
                "event": event,
                "invoice": invoice,
                "crew": queries.crew_by_role(event),
                "can_invoice": can_invoice,
                "is_future": event.date > today,
            }
        )
    context = _period_context(atelier, period, today)
    return render_template(
        "accounting/index.html",
        cards=cards,
        page=page,
        status=status,
        q=q,
        summary=queries.period_summary(atelier.id, period, today),
        statuses=EventStatus.choices() + [(queries.AWAITING, "منتظر فاکتور")],
        tab_items=_tabs(period),
        today=today,
        **context,
    )


# --------------------------------------------------------------------------
# the invoice popup
# --------------------------------------------------------------------------


def _form_context(event, invoice, state, errors, comp, suggestions, *, in_modal: bool) -> dict[str, Any]:
    return {
        "in_modal": in_modal,
        "event": event,
        "invoice": invoice,
        "atelier": event.atelier,
        "state": state,
        "errors": errors,
        "comp": comp,
        "groups": forms.GROUPS,
        "suggestions": suggestions,
        "extra_kinds": [(kind.value, kind.label) for kind in ledger.EXTRA_EXPENSE_KINDS],
        "is_edit": invoice is not None,
        "is_future": event.date > security.today(),
        "paid_lines": {l.assignment_id for l in invoice.payroll_lines if l.is_paid} if invoice is not None else set(),
        "limits": {"qty": ledger.MAX_QTY, "weight": ledger.MAX_WEIGHT, "amount": ledger.MAX_AMOUNT},
        "max_custom": forms.MAX_CUSTOM_ROWS,
        "max_extra": forms.MAX_EXTRA_ROWS,
    }


def _safe_evaluate(event, data):
    try:
        return ledger.evaluate(event, data)
    except ledger.LedgerError as exc:
        logger.warning("invoice of event %s cannot be evaluated: %s", event.id, exc)
        return None


def _wants_fragment() -> bool:
    return request.args.get("fragment") == "1"


@bp.get("/events/<int:event_id>/invoice")
@role_required(Role.ATELIER_MANAGER)
def invoice_form(event_id: int):
    event = event_or_404(event_id, as_=("atelier",))
    invoice = _invoice_of(event)
    _check_invoiceable(event, invoice)
    suggestions = ledger.suggest_expenses(event)
    state = forms.state_from_invoice(invoice, suggestions) if invoice is not None else forms.blank_state(suggestions)
    comp = _safe_evaluate(event, forms.invoice_to_data(invoice, suggestions))
    context = _form_context(event, invoice, state, {}, comp, suggestions, in_modal=_wants_fragment())
    if _wants_fragment():
        return render_template("accounting/_invoice_form.html", **context)
    return render_template("accounting/invoice_page.html", index_url=_index_url(event), **context)


def _invalid(event, invoice, parsed: forms.Parsed, suggestions):
    """Re-show the form with the typed values and the Persian errors (422)."""
    message = parsed.errors.get("__all__") or "اطلاعات واردشده معتبر نیست؛ موارد مشخص‌شده را اصلاح کنید."
    ajax = wants_json()
    context = _form_context(event, invoice, parsed.state, parsed.errors, None, suggestions, in_modal=ajax)
    if ajax:
        html = render_template("accounting/_invoice_form.html", **context)
        return json_error(message, 422, fields=parsed.errors, html=html)
    return render_template("accounting/invoice_page.html", index_url=_index_url(event), **context), 422


def _done(event, message: str, level: str, extra: dict[str, Any]):
    flash(message, level)
    target = _index_url(event)
    if wants_json():
        return json_ok({"redirect": target, **extra})
    return redirect(target)


@bp.post("/events/<int:event_id>/invoice")
@role_required(Role.ATELIER_MANAGER)
def invoice_save(event_id: int):
    event = event_or_404(event_id, as_=("atelier",))
    user = get_user()
    invoice = _invoice_of(event)
    _check_invoiceable(event, invoice)
    suggestions = ledger.suggest_expenses(event)
    parsed = forms.parse(request.form, suggestions)
    if not parsed.ok:
        return _invalid(event, invoice, parsed, suggestions)
    try:
        result = ledger.save_invoice(
            event, parsed.data, user, expected_revision=parsed.base_revision, nonce=parsed.nonce
        )
        db.session.commit()
    except ledger.StaleInvoice as exc:
        db.session.rollback()
        if exc.replay:  # the same form posted twice: it is already saved
            return _done(event, "این فاکتور قبلاً ثبت شده بود؛ تغییر تازه‌ای اعمال نشد.", "info", {"replay": True})
        abort(409, str(exc))
    except ledger.LedgerError as exc:
        db.session.rollback()
        parsed.errors["__all__"] = str(exc)
        return _invalid(event, _invoice_of(event), parsed, suggestions)
    except IntegrityError:  # two requests created the invoice of this event at the same moment
        db.session.rollback()
        event = db.session.get(Event, event_id)
        existing = _invoice_of(event)
        if existing is not None and ledger.was_saved_with_nonce(existing, parsed.nonce):
            return _done(event, "این فاکتور قبلاً ثبت شده بود؛ تغییر تازه‌ای اعمال نشد.", "info", {"replay": True})
        abort(409, "این فاکتور هم‌زمان ثبت شد؛ صفحه را تازه کنید.")

    saved = result.invoice
    ids = {
        "invoice_id": saved.id,
        "event_id": event.id,
        "atelier_id": saved.atelier_id,
        "actor_id": user.id,
        "created": result.created,
    }
    eventbus.publish(eventbus.INVOICE_SAVED, **ids)
    try:  # subscribers (stock deduction ...) may have written; they never commit
        db.session.commit()
    except Exception:  # pragma: no cover - the invoice itself is already safe
        db.session.rollback()
        logger.exception("could not persist the work of invoice.saved subscribers")
    event = db.session.get(Event, ids["event_id"])
    for warning in result.warnings:
        flash(warning, "warning")
    if result.created or result.resubmitted:
        message = "فاکتور ثبت شد و حقوق نیروها محاسبه شد."
    else:
        message = "تغییرات فاکتور ذخیره شد و حقوق نیروها دوباره محاسبه شد."
    return _done(event, message, "success", {"invoice_id": ids["invoice_id"], "created": result.created})


@bp.post("/events/<int:event_id>/invoice/preview")
@role_required(Role.ATELIER_MANAGER)
def invoice_preview(event_id: int):
    """The numbers the popup shows, computed by the same code that saves (JavaScript never does the maths)."""
    event = event_or_404(event_id, as_=("atelier",))
    invoice = _invoice_of(event)
    suggestions = ledger.suggest_expenses(event)
    payload = request.form if request.form else {k: v for k, v in json_body().items() if isinstance(v, (str, int))}
    parsed = forms.parse(payload, suggestions)
    if not parsed.ok:
        message = parsed.errors.get("__all__") or "مقدارهای واردشده معتبر نیستند."
        return json_error(message, 422, fields=parsed.errors)
    try:
        comp = ledger.evaluate(event, parsed.data)
    except ledger.LedgerError as exc:
        return json_error(str(exc), 422, code="validation_error")
    data = comp.as_dict()
    paid = {line.assignment_id for line in invoice.payroll_lines if line.is_paid} if invoice is not None else set()
    for person in data["people"]:
        person["paid"] = person["assignment_id"] in paid
    data["warnings"] = ledger.paid_differences(invoice, comp) if invoice is not None else []
    if event.date > security.today():
        data["warnings"].append("تاریخ این مراسم هنوز نرسیده است.")
    return json_ok(data)


# --------------------------------------------------------------------------
# a saved invoice: print, CSV, lifecycle
# --------------------------------------------------------------------------


@bp.get("/invoices/<int:invoice_id>/print")
@role_required(Role.ATELIER_MANAGER)
def invoice_print(invoice_id: int):
    invoice = invoice_or_404(invoice_id)
    return render_template(
        "accounting/invoice_print.html", invoice=invoice, event=invoice.event, index_url=_index_url(invoice.event)
    )


@bp.get("/invoices/<int:invoice_id>/export.csv")
@role_required(Role.ATELIER_MANAGER)
def invoice_csv(invoice_id: int):
    invoice = invoice_or_404(invoice_id)
    return exports.csv_response(f"invoice-{invoice.id}.csv", exports.invoice_csv(invoice))


def _lifecycle(invoice_id: int, action):
    invoice = invoice_or_404(invoice_id)
    user = get_user()
    event = invoice.event
    reason = Form(request.form).text("reason", "دلیل", required=True, max_len=300)
    try:
        if not reason:
            raise ledger.LedgerError("دلیل را بنویسید (دست‌کم سه نویسه).", code="reason_required")
        action(invoice, user, reason)
        db.session.commit()
    except ledger.LedgerError as exc:
        db.session.rollback()
        if wants_json():
            return json_error(str(exc), 422, code=exc.code)
        flash(str(exc), "error")
        return redirect(_index_url(db.session.get(Event, event.id)))
    return None


@bp.post("/invoices/<int:invoice_id>/reopen")
@role_required(Role.ATELIER_MANAGER)
def invoice_reopen(invoice_id: int):
    failed = _lifecycle(invoice_id, ledger.reopen_invoice)
    if failed is not None:
        return failed
    invoice = invoice_or_404(invoice_id)
    flash("فاکتور بازگشایی شد؛ شرایط پرداخت نیروها دوباره قابل ویرایش است. پس از اصلاح، فاکتور را دوباره ثبت کنید.", "success")
    return redirect(_index_url(invoice.event))


@bp.post("/invoices/<int:invoice_id>/void")
@role_required(Role.ATELIER_MANAGER)
def invoice_void(invoice_id: int):
    failed = _lifecycle(invoice_id, ledger.void_invoice)
    if failed is not None:
        return failed
    invoice = invoice_or_404(invoice_id)
    flash("فاکتور باطل شد.", "success")
    return redirect(_index_url(invoice.event))


# --------------------------------------------------------------------------
# payroll
# --------------------------------------------------------------------------


def _payroll_context(atelier, period, today, *, state_filter: str, q: str, form_state: Optional[dict] = None) -> dict[str, Any]:
    sheet = queries.payroll_sheet(atelier.id, period, state_filter, q)
    all_lines = [line for person in sheet for line in person.lines]
    page = queries.ListPage(sheet, request.args.get("page", 1, type=int), PEOPLE_PER_PAGE)
    totals = {
        "people": len(sheet),
        "events": len({line.invoice_id for line in all_lines}),
        "total": sum(line.total for line in all_lines),
        "paid": sum(line.total for line in all_lines if line.is_paid),
        "unpaid": sum(line.total for line in all_lines if not line.is_paid),
        "mismatches": sum(1 for line in all_lines if line.mismatch),
    }
    return {
        **_period_context(atelier, period, today),
        "page": page,
        "people": page.items,
        "totals": totals,
        "state_filter": state_filter,
        "q": q,
        "tab_items": _tabs(period),
        "methods": PayoutMethod.choices(),
        "today": today,
        "next_url": request.full_path.rstrip("?") if request.method == "GET" else (form_state or {}).get("next", ""),
        "form_state": form_state or {},
    }


@bp.get("/payroll")
@role_required(Role.ATELIER_MANAGER)
def payroll_index():
    atelier = _atelier()
    today = security.today()
    period = queries.resolve_period(request.args, today)
    state_filter = request.args.get("state", "")
    state_filter = state_filter if state_filter in ("paid", "unpaid") else ""
    q = clean_text(request.args.get("q", ""))[:80]
    return render_template(
        "accounting/payroll.html", **_payroll_context(atelier, period, today, state_filter=state_filter, q=q)
    )


@bp.get("/payroll/export.csv")
@role_required(Role.ATELIER_MANAGER)
def payroll_csv():
    atelier = _atelier()
    period = queries.resolve_period(request.args, security.today())
    state_filter = request.args.get("state", "")
    lines = queries.payroll_rows(
        atelier.id, period, state_filter if state_filter in ("paid", "unpaid") else "", request.args.get("q", "")
    )
    name = f"payroll-{period.jy}-{period.jm:02d}.csv" if not period.is_year else f"payroll-{period.jy}.csv"
    return exports.csv_response(name, exports.payroll_csv(lines))


def _my_payroll_lines(ids: list[int]) -> list[PayrollLine]:
    """Payroll lines of my ateliers; an id that is not mine is a 404 for the whole request."""
    unique = sorted(set(ids))
    if not unique or len(unique) > MAX_BULK:
        abort(404)
    rows = (
        db.session.execute(
            sa.select(PayrollLine)
            .join(Invoice, Invoice.id == PayrollLine.invoice_id)
            .where(PayrollLine.id.in_(unique), Invoice.atelier_id.in_(sorted(my_atelier_ids())))
            .options(
                joinedload(PayrollLine.invoice),
                joinedload(PayrollLine.assignment).joinedload(Assignment.roster_member),
            )
        )
        .unique()
        .scalars()
        .all()
    )
    if len(rows) != len(unique):
        abort(404)
    return rows


def _rerender_payroll(form: Form, ids: list[int]):
    """Show the payroll page again with the payment form's errors (422); the modal re-opens."""
    atelier = _atelier()
    today = security.today()
    source = request.form
    period = queries.resolve_period(source, today)
    state_filter = source.get("state", "")
    context = _payroll_context(
        atelier,
        period,
        today,
        state_filter=state_filter if state_filter in ("paid", "unpaid") else "",
        q=clean_text(source.get("q", ""))[:80],
        form_state={
            "errors": form.errors,
            "values": form.values,
            "selected": set(ids),
            "next": safe_next(source.get("next"), url_for("accounting.payroll_index", **period.args)),
        },
    )
    return render_template("accounting/payroll.html", **context), 422


@bp.post("/payroll/pay")
@role_required(Role.ATELIER_MANAGER)
def payroll_pay():
    form = Form(request.form)
    ids = form.int_list("line_id")
    paid_on = form.date("paid_on", "تاریخ پرداخت", required=True, max=security.today())
    method = form.enum("pay_method", "روش پرداخت", PayoutMethod, required=True)
    note = form.text("note", "یادداشت", max_len=300)
    if not ids:
        form.error("line_id", "دست‌کم یک ردیف حقوق را انتخاب کنید.")
        return _rerender_payroll(form, ids)
    lines = _my_payroll_lines(ids)
    if not form.ok:
        return _rerender_payroll(form, ids)
    try:
        result = ledger.mark_paid(lines, get_user(), paid_on=paid_on, method=method, note=note)
        db.session.commit()
    except ledger.LedgerError as exc:
        db.session.rollback()
        form.error("line_id", str(exc))
        return _rerender_payroll(form, ids)
    count = to_persian_digits(len(result.paid))
    if result.paid:
        flash(f"پرداخت {count} ردیف حقوق ثبت شد.", "success")
    if result.skipped:
        flash(f"{to_persian_digits(len(result.skipped))} ردیف از قبل پرداخت‌شده بود و تغییری نکرد.", "info")
    return redirect(_back(url_for("accounting.payroll_index")))


@bp.post("/payroll/unpay")
@role_required(Role.ATELIER_MANAGER)
def payroll_unpay():
    ids = Form(request.form).int_list("unpay_id")
    if len(ids) != 1:
        abort(404)
    (line,) = _my_payroll_lines(ids)
    try:
        ledger.unmark_paid(line, get_user())
        db.session.commit()
    except ledger.LedgerError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("ثبت پرداخت برگشت داده شد.", "success")
    return redirect(_back(url_for("accounting.payroll_index")))


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


def _reports_data(atelier, period: queries.Period):
    year = queries.make_period(period.jy, 0)
    year_rows = queries.invoice_rows(atelier.id, year.start, year.end)
    rows = [row for row in year_rows if period.start <= row.date <= period.end]
    return year_rows, rows


@bp.get("/reports")
@role_required(Role.ATELIER_MANAGER)
def reports_index():
    atelier = _atelier()
    today = security.today()
    args = {**request.args}
    args.setdefault("m", "0")  # reports default to the whole year
    period = queries.resolve_period(args, today)
    year_rows, rows = _reports_data(atelier, period)
    months = reports.month_buckets(year_rows, period.jy)
    totals = {
        "events": len(rows),
        "income": sum(r.income for r in rows),
        "expenses": sum(r.expenses for r in rows),
        "net": sum(r.net for r in rows),
        "units": sum(r.units for r in rows),
    }
    page = queries.ListPage(rows, request.args.get("page", 1, type=int), EVENT_ROWS_PER_PAGE)
    return render_template(
        "accounting/reports.html",
        months=months,
        chart=reports.trend_chart(months),
        totals=totals,
        halls=reports.hall_table(rows),
        leaders=reports.leaderboard(queries.staff_rows(atelier.id, period.start, period.end)),
        page=page,
        tab_items=_tabs(period),
        **_period_context(atelier, period, today),
    )


@bp.get("/reports/export.csv")
@role_required(Role.ATELIER_MANAGER)
def reports_csv():
    atelier = _atelier()
    args = {**request.args}
    args.setdefault("m", "0")
    period = queries.resolve_period(args, security.today())
    _, rows = _reports_data(atelier, period)
    name = f"events-{period.jy}-{period.jm:02d}.csv" if not period.is_year else f"events-{period.jy}.csv"
    return exports.csv_response(name, exports.events_csv(rows))
