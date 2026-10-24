"""Read-side queries of the accounting pages (every one is scoped to an atelier id the caller already owns).

Aggregates are computed in SQL; the narrow tuple selects used for the payroll sheet and the reports
fetch only the columns they show (a month of one atelier is a few hundred rows at most).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from app.extensions import db
from app.models import Assignment, Event, Hall, Invoice, PayrollLine, RosterMember, User
from app.services import jalali, ledger
from app.services.constants import (
    ROLE_DISPLAY_ORDER,
    EventStatus,
    Skill,
)
from app.services.forms import clean_text

__all__ = [
    "AWAITING",
    "Period",
    "ListPage",
    "resolve_period",
    "year_choices",
    "events_query",
    "period_summary",
    "PayLine",
    "PersonSheet",
    "payroll_sheet",
    "InvoiceRow",
    "invoice_rows",
    "staff_rows",
]

#: Pseudo status filter: events that still need an invoice (confirmed / done, date reached, no invoice yet).
AWAITING = "awaiting"
MIN_YEAR, MAX_YEAR = 1300, 1600
_FINAL = list(ledger.FINAL_STATUSES)


# --------------------------------------------------------------------------
# periods (Jalali month / year)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """A Jalali month (``jm`` 1..12) or a whole Jalali year (``jm == 0``) as a Gregorian date range."""

    jy: int
    jm: int
    start: dt.date
    end: dt.date

    @property
    def is_year(self) -> bool:
        return self.jm == 0

    @property
    def label(self) -> str:
        if self.is_year:
            return f"سال {self.jy}"
        return f"{jalali.JALALI_MONTHS[self.jm - 1]} {self.jy}"

    @property
    def args(self) -> dict[str, int]:
        return {"y": self.jy, "m": self.jm}

    def shifted(self, steps: int) -> "Period":
        """The previous (``-1``) or next (``+1``) month - or year for a whole-year period."""
        if self.is_year:
            return make_period(self.jy + steps, 0)
        index = self.jy * 12 + (self.jm - 1) + steps
        return make_period(index // 12, index % 12 + 1)


def make_period(jy: int, jm: int) -> Period:
    jy = min(max(jy, MIN_YEAR), MAX_YEAR)
    if jm == 0:
        return Period(jy, 0, jalali.jalali_month_range(jy, 1)[0], jalali.jalali_month_range(jy, 12)[1])
    start, end = jalali.jalali_month_range(jy, jm)
    return Period(jy, jm, start, end)


def _int_arg(args: Mapping[str, Any], name: str) -> Optional[int]:
    try:
        return int(str(args.get(name, "")).strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
    except (TypeError, ValueError):
        return None


def resolve_period(args: Mapping[str, Any], today: dt.date) -> Period:
    """The period of ``?y=&m=`` (junk falls back to the current Jalali month; ``m=0`` is the whole year)."""
    jy_now, jm_now, _ = jalali.to_jalali(today)
    jy = _int_arg(args, "y")
    jm = _int_arg(args, "m")
    if jy is None or not (MIN_YEAR <= jy <= MAX_YEAR):
        jy = jy_now
        jm = jm_now if jm is None else jm
    if jm is None:
        jm = jm_now if jy == jy_now else 0
    if not (0 <= jm <= 12):
        jm = jm_now
    return make_period(jy, jm)


def year_choices(atelier_id: int, today: dt.date) -> list[int]:
    """Jalali years that have events of this atelier (plus the current one), newest last."""
    first, last = db.session.execute(
        sa.select(sa.func.min(Event.date), sa.func.max(Event.date)).where(Event.atelier_id == atelier_id)
    ).one()
    years = {jalali.to_jalali(today)[0]}
    if first is not None:
        years.update(range(jalali.to_jalali(first)[0], jalali.to_jalali(last)[0] + 1))
    return sorted(years)


# --------------------------------------------------------------------------
# a tiny page object for in-memory lists (same API the `pagination` macro uses)
# --------------------------------------------------------------------------


class ListPage:
    """Paginate a Python list with the attributes of a Flask-SQLAlchemy ``Pagination``."""

    def __init__(self, items: Sequence[Any], page: int, per_page: int) -> None:
        self.total = len(items)
        self.per_page = max(1, per_page)
        self.pages = max(1, -(-self.total // self.per_page))
        self.page = min(max(1, page), self.pages)
        start = (self.page - 1) * self.per_page
        self.items = list(items[start : start + self.per_page])

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> Optional[int]:
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self) -> Optional[int]:
        return self.page + 1 if self.has_next else None

    def iter_pages(
        self, left_edge: int = 2, left_current: int = 2, right_current: int = 4, right_edge: int = 2
    ) -> Iterator[Optional[int]]:
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - left_current <= num <= self.page + right_current)
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


# --------------------------------------------------------------------------
# event cards
# --------------------------------------------------------------------------


def events_query(atelier_id: int, period: Period, status: str, q: str, today: dt.date) -> sa.Select:
    """The event cards of one atelier in *period*, newest first, with everything a card shows preloaded."""
    stmt = (
        sa.select(Event)
        .join(Hall, Hall.id == Event.hall_id)
        .where(Event.atelier_id == atelier_id, Event.date >= period.start, Event.date <= period.end)
        .options(
            contains_eager(Event.hall),
            selectinload(Event.assignments).options(joinedload(Assignment.user), joinedload(Assignment.roster_member)),
            selectinload(Event.invoice).options(
                selectinload(Invoice.lines),
                selectinload(Invoice.payments),
                selectinload(Invoice.expenses),
                selectinload(Invoice.payroll_lines),
            ),
        )
        .order_by(Event.date.desc(), Event.start_time.desc(), Event.id.desc())
    )
    if status == AWAITING:
        stmt = stmt.where(
            Event.status.in_([EventStatus.CONFIRMED, EventStatus.DONE]),
            Event.date <= today,
            ~sa.exists().where(Invoice.event_id == Event.id),
        )
    elif EventStatus.is_valid(status):
        stmt = stmt.where(Event.status == EventStatus.coerce(status))
    text = clean_text(q)[:80]
    if text:
        by_roster = (
            sa.exists()
            .where(
                Assignment.event_id == Event.id,
                Assignment.roster_member_id == RosterMember.id,
                RosterMember.display_name.icontains(text, autoescape=True),
            )
        )
        by_user = (
            sa.exists()
            .where(
                Assignment.event_id == Event.id,
                Assignment.user_id == User.id,
                User.full_name.icontains(text, autoescape=True),
            )
        )
        stmt = stmt.where(
            sa.or_(
                Event.title.icontains(text, autoescape=True),
                Hall.name.icontains(text, autoescape=True),
                Hall.city.icontains(text, autoescape=True),
                by_roster,
                by_user,
            )
        )
    return stmt


def period_summary(atelier_id: int, period: Period, today: dt.date) -> dict[str, int]:
    """Income, expenses, net profit and units of the final invoices in *period*, plus event counters."""
    income, expenses, net, units, invoices = db.session.execute(
        sa.select(
            sa.func.coalesce(sa.func.sum(Invoice.income_total), 0),
            sa.func.coalesce(sa.func.sum(Invoice.expense_total), 0),
            sa.func.coalesce(sa.func.sum(Invoice.net_profit), 0),
            sa.func.coalesce(sa.func.sum(Invoice.units_total), 0),
            sa.func.count(Invoice.id),
        )
        .join(Event, Event.id == Invoice.event_id)
        .where(
            Invoice.atelier_id == atelier_id,
            Invoice.status.in_(_FINAL),
            Event.date >= period.start,
            Event.date <= period.end,
        )
    ).one()
    events = db.session.execute(
        sa.select(sa.func.count(Event.id)).where(
            Event.atelier_id == atelier_id,
            Event.date >= period.start,
            Event.date <= period.end,
            Event.status != EventStatus.CANCELLED,
        )
    ).scalar_one()
    awaiting = db.session.execute(
        sa.select(sa.func.count(Event.id)).where(
            Event.atelier_id == atelier_id,
            Event.date >= period.start,
            Event.date <= period.end,
            Event.status.in_([EventStatus.CONFIRMED, EventStatus.DONE]),
            Event.date <= today,
            ~sa.exists().where(Invoice.event_id == Event.id),
        )
    ).scalar_one()
    return {
        "income": int(income),
        "expenses": int(expenses),
        "net": int(net),
        "units": int(units),
        "invoices": int(invoices),
        "events": int(events),
        "awaiting": int(awaiting),
    }


def crew_by_role(event: Event) -> list[dict[str, Any]]:
    """``[{role, people: [{name, pending}]}]`` in workflow order for the card (declined / cancelled people hidden)."""
    groups = []
    for role in ROLE_DISPLAY_ORDER:
        people = [
            {"name": a.person_name or "—", "pending": a.status.value == "invited"}
            for a in event.assignments
            if a.role == role and a.status.value in ("invited", "accepted", "confirmed", "completed")
        ]
        groups.append({"role": role, "people": people})
    return groups


# --------------------------------------------------------------------------
# payroll sheet
# --------------------------------------------------------------------------


@dataclass
class PayLine:
    """One payroll line as the sheet shows it."""

    id: int
    invoice_id: int
    event_id: int
    event_date: dt.date
    hall: str
    role: Optional[Skill]
    name: str
    units: int
    base: int
    commission: int
    bonus: int
    total: int
    paid_at: Optional[dt.datetime]
    paid_method: Any
    paid_note: Optional[str]
    person_key: str
    paid_amount: Optional[int] = None  # what was recorded when it was paid (from the audit trail)

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None

    @property
    def mismatch(self) -> bool:
        """Paid, but the invoice was recalculated afterwards."""
        return self.is_paid and self.paid_amount is not None and self.paid_amount != self.total


@dataclass
class PersonSheet:
    """One person's month: totals and their lines."""

    key: str
    name: str
    lines: list[PayLine] = field(default_factory=list)

    @property
    def roles(self) -> list[Skill]:
        seen = {line.role for line in self.lines if line.role is not None}
        return [role for role in ROLE_DISPLAY_ORDER if role in seen]

    @property
    def events(self) -> int:
        return len({line.invoice_id for line in self.lines})

    @property
    def units(self) -> int:
        per_invoice = {line.invoice_id: line.units for line in self.lines}  # a second role at the same event adds none
        return sum(per_invoice.values())

    @property
    def base(self) -> int:
        return sum(line.base for line in self.lines)

    @property
    def commission(self) -> int:
        return sum(line.commission for line in self.lines)

    @property
    def bonus(self) -> int:
        return sum(line.bonus for line in self.lines)

    @property
    def total(self) -> int:
        return sum(line.total for line in self.lines)

    @property
    def paid(self) -> int:
        return sum(line.total for line in self.lines if line.is_paid)

    @property
    def unpaid(self) -> int:
        return self.total - self.paid

    @property
    def unpaid_lines(self) -> list[PayLine]:
        return [line for line in self.lines if not line.is_paid]

    @property
    def has_mismatch(self) -> bool:
        return any(line.mismatch for line in self.lines)

    @property
    def state(self) -> str:
        if not self.lines:
            return "unpaid"
        if self.unpaid == 0 and not self.unpaid_lines:
            return "paid"
        return "partial" if self.paid else "unpaid"


def _person_key(user_id: Optional[int], roster_id: Optional[int], roster_user_id: Optional[int]) -> str:
    if user_id is not None:
        return f"u{user_id}"
    if roster_user_id is not None:
        return f"u{roster_user_id}"  # a roster entry linked to an account is the same person
    return f"r{roster_id}"


def payroll_rows(atelier_id: int, period: Period, state: str = "", q: str = "") -> list[PayLine]:
    """Payroll lines of the final invoices of an atelier in *period* (``state``: paid / unpaid / any)."""
    stmt = (
        sa.select(
            PayrollLine.id,
            PayrollLine.invoice_id,
            Invoice.event_id,
            Event.date,
            Hall.name,
            PayrollLine.role,
            PayrollLine.person_name,
            PayrollLine.units,
            PayrollLine.base,
            PayrollLine.commission,
            PayrollLine.bonus,
            PayrollLine.total,
            PayrollLine.paid_at,
            PayrollLine.paid_method,
            PayrollLine.paid_note,
            Assignment.user_id,
            Assignment.roster_member_id,
            RosterMember.user_id,
        )
        .join(Invoice, Invoice.id == PayrollLine.invoice_id)
        .join(Event, Event.id == Invoice.event_id)
        .join(Hall, Hall.id == Event.hall_id)
        .join(Assignment, Assignment.id == PayrollLine.assignment_id)
        .outerjoin(RosterMember, RosterMember.id == Assignment.roster_member_id)
        .where(
            Invoice.atelier_id == atelier_id,
            Invoice.status.in_(_FINAL),
            Event.date >= period.start,
            Event.date <= period.end,
        )
        .order_by(Event.date, PayrollLine.id)
    )
    if state == "paid":
        stmt = stmt.where(PayrollLine.paid_at.is_not(None))
    elif state == "unpaid":
        stmt = stmt.where(PayrollLine.paid_at.is_(None))
    text = clean_text(q)[:80]
    if text:
        stmt = stmt.where(PayrollLine.person_name.icontains(text, autoescape=True))
    lines = []
    for row in db.session.execute(stmt):
        lines.append(
            PayLine(
                id=row[0], invoice_id=row[1], event_id=row[2], event_date=row[3], hall=row[4], role=row[5],
                name=row[6], units=row[7], base=row[8], commission=row[9], bonus=row[10], total=row[11],
                paid_at=row[12], paid_method=row[13], paid_note=row[14], person_key=_person_key(row[15], row[16], row[17]),
            )
        )
    paid_ids = [line.id for line in lines if line.is_paid]
    amounts = ledger.paid_amounts(paid_ids)
    for line in lines:
        line.paid_amount = amounts.get(line.id)
    return lines


def payroll_sheet(atelier_id: int, period: Period, state: str = "", q: str = "") -> list[PersonSheet]:
    """People of the period with their lines, those with money still owed first, then by name."""
    people: dict[str, PersonSheet] = {}
    for line in payroll_rows(atelier_id, period, state, q):
        sheet = people.setdefault(line.person_key, PersonSheet(line.person_key, line.name))
        sheet.name = line.name  # the newest snapshot wins
        sheet.lines.append(line)
    return sorted(people.values(), key=lambda p: (p.unpaid == 0, p.name))


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceRow:
    """One final invoice with the little the reports need."""

    invoice_id: int
    event_id: int
    date: dt.date
    hall_id: int
    hall: str
    title: Optional[str]
    income: int
    expenses: int
    net: int
    units: int

    @property
    def margin(self) -> Optional[int]:
        """Net profit as a whole percentage of income (None without income)."""
        if self.income <= 0:
            return None
        percent = abs(self.net) * 100 // self.income
        return -percent if self.net < 0 else percent


def invoice_rows(atelier_id: int, start: dt.date, end: dt.date) -> list[InvoiceRow]:
    rows = db.session.execute(
        sa.select(
            Invoice.id,
            Event.id,
            Event.date,
            Hall.id,
            Hall.name,
            Event.title,
            Invoice.income_total,
            Invoice.expense_total,
            Invoice.net_profit,
            Invoice.units_total,
        )
        .join(Event, Event.id == Invoice.event_id)
        .join(Hall, Hall.id == Event.hall_id)
        .where(
            Invoice.atelier_id == atelier_id,
            Invoice.status.in_(_FINAL),
            Event.date >= start,
            Event.date <= end,
        )
        .order_by(Event.date.desc(), Invoice.id.desc())
    )
    return [InvoiceRow(*row) for row in rows]


def staff_rows(atelier_id: int, start: dt.date, end: dt.date) -> list[tuple[str, str, int, int, int]]:
    """``(person key, name, invoice id, units, total)`` of every payroll line in the range."""
    rows = db.session.execute(
        sa.select(
            Assignment.user_id,
            Assignment.roster_member_id,
            RosterMember.user_id,
            PayrollLine.person_name,
            PayrollLine.invoice_id,
            PayrollLine.units,
            PayrollLine.total,
        )
        .join(Invoice, Invoice.id == PayrollLine.invoice_id)
        .join(Event, Event.id == Invoice.event_id)
        .join(Assignment, Assignment.id == PayrollLine.assignment_id)
        .outerjoin(RosterMember, RosterMember.id == Assignment.roster_member_id)
        .where(
            Invoice.atelier_id == atelier_id,
            Invoice.status.in_(_FINAL),
            Event.date >= start,
            Event.date <= end,
        )
        .order_by(Event.date, PayrollLine.id)
    )
    return [(_person_key(r[0], r[1], r[2]), r[3], r[4], r[5], r[6]) for r in rows]
