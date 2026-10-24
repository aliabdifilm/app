"""Earnings: what the person was paid (or will be paid), straight from ``PayrollLine`` rows.

Pay is **never recomputed here**.  The accounting module writes one payroll line per afish row when an invoice
is submitted (``units, base, commission, bonus, total`` - a frozen calculation, AGENTS.md formula); this module
selects the lines of *one* person, adds them up in SQL and words them.  Only lines of invoices that count
(``submitted`` / ``settled``) are visible: a reopened draft or a void invoice shows nothing, because its
numbers may still change.

Ownership: every query joins ``Assignment`` and filters ``Assignment.involving_user(user_id)`` (the person's own
afish rows, directly or through a roster entry linked to their account).  There is no function in this module
that takes a payroll line id without also checking that owner rule.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, Invoice, PayrollLine
from app.services import jalali, ledger
from app.services.money import to_persian_digits

from . import paytext

FINAL_STATUSES = tuple(ledger.FINAL_STATUSES)
PAID_FILTERS = ("all", "paid", "unpaid")
CSV_LIMIT = 5000

# -- queries ----------------------------------------------------------------------


def _joined(stmt: sa.Select, user_id: int) -> sa.Select:
    return (
        stmt.join(Assignment, Assignment.id == PayrollLine.assignment_id)
        .join(Invoice, Invoice.id == PayrollLine.invoice_id)
        .join(Event, Event.id == Assignment.event_id)
        .where(Assignment.involving_user(user_id), Invoice.status.in_(FINAL_STATUSES))
    )


def _filtered(stmt: sa.Select, first: Optional[dt.date], last: Optional[dt.date], status: str) -> sa.Select:
    if first is not None and last is not None:
        stmt = stmt.where(Event.date.between(first, last))
    if status == "paid":
        stmt = stmt.where(PayrollLine.paid_at.is_not(None))
    elif status == "unpaid":
        stmt = stmt.where(PayrollLine.paid_at.is_(None))
    return stmt


def lines_select(
    user_id: int,
    first: Optional[dt.date] = None,
    last: Optional[dt.date] = None,
    status: str = "all",
) -> sa.Select:
    """My payroll lines (newest event first), with the afish row, event, hall and invoice loaded."""
    stmt = _joined(sa.select(PayrollLine), user_id).options(
        joinedload(PayrollLine.assignment).joinedload(Assignment.event).joinedload(Event.hall),
        joinedload(PayrollLine.invoice),
    )
    return _filtered(stmt, first, last, status).order_by(Event.date.desc(), PayrollLine.id.desc())


@dataclass(frozen=True)
class Totals:
    """Sums over a set of payroll lines (integers, Toman)."""

    count: int = 0
    total: int = 0
    paid: int = 0
    unpaid: int = 0
    units: int = 0


def totals(
    user_id: int,
    first: Optional[dt.date] = None,
    last: Optional[dt.date] = None,
    status: str = "all",
) -> Totals:
    """One aggregate query: number of lines, total, paid, unpaid and sold units."""
    is_paid = PayrollLine.paid_at.is_not(None)
    stmt = _joined(
        sa.select(
            sa.func.count(PayrollLine.id),
            sa.func.coalesce(sa.func.sum(PayrollLine.total), 0),
            sa.func.coalesce(sa.func.sum(sa.case((is_paid, PayrollLine.total), else_=0)), 0),
            sa.func.coalesce(sa.func.sum(PayrollLine.units), 0),
        ).select_from(PayrollLine),
        user_id,
    )
    count, total, paid, units = db.session.execute(_filtered(stmt, first, last, status)).one()
    total, paid = int(total), int(paid)
    return Totals(count=int(count), total=total, paid=paid, unpaid=total - paid, units=int(units))


def outstanding(user_id: int) -> int:
    """Everything still unpaid, over all time."""
    return totals(user_id, status="unpaid").unpaid


@dataclass(frozen=True)
class MonthBar:
    month: int  # Jalali month 1..12
    label: str  # full name with year, «مرداد ۱۴۰۴»
    short: str  # «مرد»
    paid: int
    unpaid: int

    @property
    def total(self) -> int:
        return self.paid + self.unpaid


def year_series(user_id: int, jalali_year: int) -> list[MonthBar]:
    """Paid / unpaid pay per Jalali month of *jalali_year* - a single query with 24 conditional sums."""
    is_paid = PayrollLine.paid_at.is_not(None)
    is_unpaid = PayrollLine.paid_at.is_(None)
    columns: list[Any] = []
    ranges = [jalali.jalali_month_range(jalali_year, month) for month in range(1, 13)]
    for first, last in ranges:
        in_month = Event.date.between(first, last)
        columns.append(
            sa.func.coalesce(sa.func.sum(sa.case((sa.and_(in_month, is_paid), PayrollLine.total), else_=0)), 0)
        )
        columns.append(
            sa.func.coalesce(sa.func.sum(sa.case((sa.and_(in_month, is_unpaid), PayrollLine.total), else_=0)), 0)
        )
    stmt = _joined(sa.select(*columns).select_from(PayrollLine), user_id).where(
        Event.date.between(ranges[0][0], ranges[-1][1])
    )
    values = db.session.execute(stmt).one()
    bars = []
    for index in range(12):
        name = jalali.JALALI_MONTHS[index]
        bars.append(
            MonthBar(
                month=index + 1,
                label=to_persian_digits(f"{name} {jalali_year}"),
                short=name[:3],
                paid=int(values[2 * index]),
                unpaid=int(values[2 * index + 1]),
            )
        )
    return bars


# -- the bar chart (geometry only; the template draws the SVG) ----------------------------


@dataclass
class Bar:
    month: int
    label: str
    short: str
    paid: int
    unpaid: int
    center: float
    hit_x: float
    paid_path: str = ""
    unpaid_path: str = ""
    label_y: float = 0.0
    title: str = ""


@dataclass
class Chart:
    width: int
    height: int
    plot_left: float
    plot_right: float
    baseline: float
    axis_x: float
    bars: list[Bar] = field(default_factory=list)
    ticks: list[dict[str, Any]] = field(default_factory=list)  # {y, label}
    unit_label: str = ""
    peak_month: Optional[int] = None
    peak_text: str = ""
    peak_x: float = 0.0
    peak_y: float = 0.0
    scale: int = 0
    slot: float = 0.0
    empty: bool = True


_NICE = (10, 12, 14, 16, 20, 30, 40, 50, 60, 80, 100)  # tenths of a power of ten


def nice_scale(peak: int) -> int:
    """The smallest "round" axis maximum (1, 1.2, 1.4, 1.6, 2, 3, 4, 5, 6, 8 x 10^k) at or above *peak*."""
    if peak <= 0:
        return 1_000_000
    magnitude = 10 ** (len(str(peak)) - 1)
    for step in _NICE:
        candidate = magnitude * step // 10
        if candidate >= peak:
            return candidate
    return magnitude * 10


def _tenths_text(value: int, unit: int) -> str:
    tenths = value * 10 // unit
    whole, frac = divmod(tenths, 10)
    return to_persian_digits(str(whole) if frac == 0 else f"{whole}٫{frac}")


def _bar_path(x: float, y: float, w: float, h: float, radius: float) -> str:
    """A column segment: square at the bottom, *radius* rounded at the top (0 = plain rectangle)."""
    if h <= 0:
        return ""
    r = min(radius, h / 2, w / 2)
    x2, y2 = x + w, y + h
    if r <= 0:
        return f"M{x:.1f},{y:.1f}H{x2:.1f}V{y2:.1f}H{x:.1f}Z"
    return (
        f"M{x:.1f},{y2:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f"H{x2 - r:.1f}Q{x2:.1f},{y:.1f} {x2:.1f},{y + r:.1f}V{y2:.1f}Z"
    )


def build_chart(series: list[MonthBar], width: int = 360, height: int = 176) -> Chart:
    """Stacked columns (paid below, unpaid above) for the 12 months, laid out right-to-left.

    Marks follow the data-viz spec: columns at most 24px wide, 4px rounded data end, square at the baseline,
    a 2px gap between the stacked segments, one value label on the tallest column.  The y-axis is on the
    right (RTL) with two gridlines; the unit is millions (or thousands) of Toman.
    """
    plot_left, plot_right = 6.0, float(width) - 44.0
    top, baseline = 22.0, float(height) - 26.0
    plot_h = baseline - top
    peak = max((bar.total for bar in series), default=0)
    scale = nice_scale(peak)
    unit = 1_000_000 if scale >= 1_000_000 else 1_000
    chart = Chart(
        width=width,
        height=height,
        plot_left=plot_left,
        plot_right=plot_right,
        baseline=baseline,
        axis_x=plot_right + 8,
        unit_label="میلیون تومان" if unit == 1_000_000 else "هزار تومان",
        scale=scale,
        empty=peak == 0,
    )
    for value in (0, scale // 2, scale):
        chart.ticks.append({"y": round(baseline - plot_h * value / scale, 1), "label": _tenths_text(value, unit)})
    slot = (plot_right - plot_left) / 12
    chart.slot = round(slot, 1)
    bar_w = min(24.0, slot - 8)
    gap = 2.0
    for index, item in enumerate(series):
        center = plot_right - (index + 0.5) * slot
        x = center - bar_w / 2
        paid_h = plot_h * item.paid / scale if item.paid else 0.0
        unpaid_h = plot_h * item.unpaid / scale if item.unpaid else 0.0
        if item.paid:
            paid_h = max(paid_h, 3.0)
        if item.unpaid:
            unpaid_h = max(unpaid_h, 3.0)
        stacked = bool(item.paid and item.unpaid)
        paid_top = baseline - paid_h
        bar = Bar(
            month=item.month,
            label=item.label,
            short=item.short,
            paid=item.paid,
            unpaid=item.unpaid,
            center=round(center, 1),
            hit_x=round(plot_right - (index + 1) * slot, 1),
            title=(
                f"{item.label}: پرداخت‌شده {paytext.money(item.paid)} تومان، "
                f"پرداخت‌نشده {paytext.money(item.unpaid)} تومان"
            ),
        )
        # the top-most segment carries the rounded data end; the lower one of a stack stays square
        bar.paid_path = _bar_path(x, paid_top, bar_w, paid_h, 0 if stacked else 4)
        if item.unpaid:
            unpaid_y = baseline - paid_h - (gap if stacked else 0) - unpaid_h
            bar.unpaid_path = _bar_path(x, unpaid_y, bar_w, unpaid_h, 4)
            bar.label_y = unpaid_y - 4
        else:
            bar.label_y = paid_top - 4
        chart.bars.append(bar)
    peak_bar = max(series, key=lambda bar: bar.total, default=None)
    if peak_bar is not None and peak_bar.total > 0:
        shown = chart.bars[peak_bar.month - 1]
        chart.peak_month = peak_bar.month
        chart.peak_text = _tenths_text(peak_bar.total, unit)
        chart.peak_x = shown.center
        chart.peak_y = max(shown.label_y, 11.0)
    return chart


# -- rows for the list, the detail page and the CSV ---------------------------------------------


@dataclass
class EarningRow:
    line: PayrollLine
    assignment: Assignment
    event: Event
    terms: dict[str, Any]
    breakdown: list[dict[str, str]]
    paid: bool
    recalculated_from: Optional[int] = None  # amount recorded when it was paid, if the total changed since

    @property
    def role(self):
        return self.line.role or self.assignment.role


def build_rows(lines: list[PayrollLine]) -> list[EarningRow]:
    """Wrap loaded lines for the templates (explanations come from the stored numbers)."""
    paid_ids = [line.id for line in lines if line.is_paid]
    recorded = ledger.paid_amounts(paid_ids) if paid_ids else {}
    rows = []
    for line in lines:
        assignment = line.assignment
        terms = assignment.terms_dict()
        before = recorded.get(line.id)
        rows.append(
            EarningRow(
                line=line,
                assignment=assignment,
                event=assignment.event,
                terms=terms,
                breakdown=paytext.breakdown_lines(line, terms),
                paid=line.is_paid,
                recalculated_from=before if (before is not None and before != line.total) else None,
            )
        )
    return rows


_CSV_HEADER = (
    "تاریخ مراسم (شمسی)",
    "تاریخ مراسم (میلادی)",
    "تالار",
    "نقش",
    "تعداد فروش",
    "پایه (تومان)",
    "پورسانت (تومان)",
    "پاداش (تومان)",
    "جمع (تومان)",
    "وضعیت پرداخت",
    "تاریخ پرداخت (شمسی)",
    "روش پرداخت",
)


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formulas: a text cell must not start with ``= + - @``, tab or CR."""
    if isinstance(value, str) and value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def build_csv(lines: list[PayrollLine]) -> str:
    """The lines as CSV text (UTF-8 with BOM so Excel shows Persian correctly); amounts are plain integers."""
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CSV_HEADER)
    for line in lines:
        event = line.assignment.event
        role = line.role or line.assignment.role
        writer.writerow(
            [
                csv_safe(jalali.to_jalali_str(event.date, "numeric")),
                event.date.isoformat(),
                csv_safe(event.hall.name if event.hall else ""),
                csv_safe(role.label if role else ""),
                line.units,
                line.base,
                line.commission,
                line.bonus,
                line.total,
                "پرداخت شده" if line.is_paid else "پرداخت نشده",
                csv_safe(jalali.to_jalali_datetime_str(line.paid_at, "numeric").split(" ")[0] if line.paid_at else ""),
                csv_safe(line.paid_method.label if line.paid_method else ""),
            ]
        )
    return buffer.getvalue()

