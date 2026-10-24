"""CSV exports that open cleanly in Excel: UTF-8 with a BOM, ASCII digits, Gregorian and Jalali date columns.

Cells that start with ``= + - @`` (or a tab / carriage return) are prefixed with ``'`` so a hostile
name or label can never run as a spreadsheet formula.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any, Iterable, Sequence

from flask import Response

from app.models import Invoice
from app.services import jalali
from app.services.constants import PRODUCT_CATALOG, PaymentMethod

__all__ = ["csv_response", "jalali_ascii", "invoice_csv", "payroll_csv", "events_csv"]

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
BOM = "﻿"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)  # numbers (also negative ones) are not text: no escaping, ASCII digits
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if text.startswith(_DANGEROUS):
        text = "'" + text
    return text


def jalali_ascii(value: Any) -> str:
    """``1404/04/26`` with ASCII digits ('' for a missing date); a timestamp is shown as its Iran-time day."""
    if value is None:
        return ""
    return jalali.to_jalali_str(value, "numeric", assume_utc=isinstance(value, dt.datetime))


def gregorian(value: Any) -> str:
    """ISO date (``2025-07-17``); a timestamp is converted to its Iran-time day first."""
    if value is None:
        return ""
    return jalali.to_date(value, assume_utc=isinstance(value, dt.datetime)).isoformat()


def csv_response(filename: str, rows: Iterable[Sequence[Any]]) -> Response:
    """A ``text/csv`` download of *rows* (the first row is the header)."""
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


def invoice_csv(invoice: Invoice) -> list[list[Any]]:
    """One invoice as titled blocks separated by blank rows (info, products, payments, expenses, payroll, result)."""
    event = invoice.event
    rows: list[list[Any]] = [
        ["فاکتور مراسم"],
        ["شمارهٔ فاکتور", invoice.id],
        ["اتلیه", invoice.atelier.name],
        ["تالار", event.hall.name],
        ["تاریخ مراسم (میلادی)", gregorian(event.date)],
        ["تاریخ مراسم (شمسی)", jalali_ascii(event.date)],
        ["وضعیت فاکتور", invoice.status.label],
        ["نسخه", invoice.revision],
        [],
        ["محصولات فروش‌رفته"],
        ["دسته", "ابعاد", "تعداد", "تعداد محاسبه برای حقوق", "شمارش در پورسانت", "واحد پورسانت"],
    ]
    for line in invoice.lines:
        rows.append(
            [
                PRODUCT_CATALOG[line.category]["label"],
                line.size_label,
                line.qty,
                line.commission_weight,
                "بله" if line.counts_for_commission else "خیر",
                line.qty * line.commission_weight if line.counts_for_commission else 0,
            ]
        )
    rows += [["جمع واحدها", "", "", "", "", invoice.units_total], [], ["دریافتی‌ها (تومان)"], ["روش", "مبلغ"]]
    pays = {p.method: p.amount for p in invoice.payments}
    for method in PaymentMethod:
        rows.append([method.label, pays.get(method, 0)])
    rows += [["جمع دریافتی", invoice.income_total], [], ["هزینه‌ها (تومان)"], ["نوع", "شرح", "مبلغ"]]
    for expense in invoice.expenses:
        rows.append([expense.kind.label, expense.label, expense.amount])
    rows += [["جمع هزینه‌ها", "", invoice.expense_total], [], ["حقوق نیروها (تومان)"]]
    rows.append(["نام", "نقش", "واحد", "پایه", "پورسانت", "پاداش", "جمع", "پرداخت‌شده", "تاریخ پرداخت (میلادی)", "تاریخ پرداخت (شمسی)"])
    for line in invoice.payroll_lines:
        rows.append(
            [
                line.person_name,
                line.role.label if line.role else "",
                line.units,
                line.base,
                line.commission,
                line.bonus,
                line.total,
                "بله" if line.is_paid else "خیر",
                gregorian(line.paid_at),
                jalali_ascii(line.paid_at),
            ]
        )
    rows += [[], ["سود خالص (تومان)", invoice.net_profit]]
    if invoice.notes:
        rows += [[], ["یادداشت", invoice.notes]]
    return rows


def payroll_csv(lines: Iterable[Any]) -> list[list[Any]]:
    """The payroll sheet (``queries.PayLine`` rows) as one flat table."""
    rows: list[list[Any]] = [
        [
            "نام", "نقش", "تاریخ مراسم (میلادی)", "تاریخ مراسم (شمسی)", "تالار", "واحد", "پایه", "پورسانت",
            "پاداش", "جمع", "وضعیت", "تاریخ پرداخت (میلادی)", "تاریخ پرداخت (شمسی)", "روش پرداخت", "یادداشت پرداخت",
        ]
    ]
    for line in lines:
        rows.append(
            [
                line.name,
                line.role.label if line.role else "",
                gregorian(line.event_date),
                jalali_ascii(line.event_date),
                line.hall,
                line.units,
                line.base,
                line.commission,
                line.bonus,
                line.total,
                "پرداخت شده" if line.is_paid else "پرداخت نشده",
                gregorian(line.paid_at),
                jalali_ascii(line.paid_at),
                line.paid_method.label if line.paid_method else "",
                line.paid_note or "",
            ]
        )
    return rows


def events_csv(rows: Iterable[Any]) -> list[list[Any]]:
    """Per-event profitability (``queries.InvoiceRow``) as one flat table."""
    out: list[list[Any]] = [
        ["تاریخ (میلادی)", "تاریخ (شمسی)", "تالار", "مراسم", "واحد فروش", "درآمد", "هزینه", "سود خالص", "حاشیهٔ سود (درصد)"]
    ]
    for row in rows:
        out.append(
            [
                gregorian(row.date), jalali_ascii(row.date), row.hall, row.title or "", row.units,
                row.income, row.expenses, row.net, row.margin,
            ]
        )
    return out
