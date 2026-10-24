"""Event ledger: invoice arithmetic, expense suggestions and persistence.

This is the accounting engine behind ``/accounting`` (AGENTS.md "بخش مالی").  It
**never re-implements the pay formula**: units come from
:func:`app.services.payroll.units_from_lines` (so ``counts_for_commission`` and
weights are honoured) and every person is paid by
:func:`app.services.payroll.compute_pay` with the terms snapshot of *their own*
assignment.

Layers
------
pure (no database)
    :func:`build_lines`, :func:`normalize_payments`, :func:`compute_invoice`,
    :func:`paid_at_from_date`.  They take and return plain values / frozen
    dataclasses and are what the live preview endpoint and the tests exercise.
database
    :func:`payable_assignments`, :func:`suggest_expenses`, :func:`evaluate`,
    :func:`save_invoice`, :func:`recompute`, :func:`reopen_invoice`,
    :func:`void_invoice`, :func:`mark_paid`, :func:`unmark_paid`.  Like every
    service they ``add`` + ``flush`` and **never commit**; the route owns the
    transaction.

Formulas (assumptions A2 / A5)::

    units      = sum(qty * weight)  over lines that count for commission
    income     = card_reader + card_to_card + cash
    expenses   = venue_rent + staff_pay + equipment_rental + material + other
    net_profit = income - expenses                       (may be negative)

Invoice life cycle::

    (none) --save--> submitted --all payroll paid--> settled
                       |  ^                             |
                  reopen|  |save (re-submit)            +--(a payment is undone)--> submitted
                       v  |
                     draft          submitted/draft --void--> void   (terminal, kept for the audit trail)

Saving an invoice moves the event ``confirmed -> done -> invoiced``, locks the
pay terms of every committed assignment (``staffing.lock_event_terms``), writes
the audit trail and tells the staff.  Re-saving recomputes everything; payroll
lines that are already paid keep their paid markers and are reported in
``SaveResult.warnings`` when the new total differs.
"""

from __future__ import annotations

import datetime as dt
import logging
import numbers
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    Assignment,
    AuditLog,
    EquipmentUsage,
    Event,
    ExpenseLine,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    PayrollLine,
    utcnow,
)
from app.services import audit, jalali, notifications, payroll, staffing
from app.services.constants import (
    COMMITTED_ASSIGNMENT_STATUSES,
    PRODUCT_CATALOG,
    ROLE_DISPLAY_ORDER,
    STANDARD_SIZES,
    EquipmentOwnership,
    EventStatus,
    ExpenseKind,
    InvoiceStatus,
    NotificationKind,
    PaymentMethod,
    PayoutMethod,
    ProductCategory,
    Skill,
    format_size,
    normalize_size_label,
)
from app.services.money import MAX_TOMAN, format_toman, normalize_digits, to_persian_digits
from app.services.print_yield import normalize_size

__all__ = [
    "FINAL_STATUSES",
    "INVOICEABLE_EVENT_STATUSES",
    "MAX_AMOUNT",
    "MAX_QTY",
    "MAX_WEIGHT",
    "EXTRA_EXPENSE_KINDS",
    "LedgerError",
    "StaleInvoice",
    "LineSpec",
    "ExpenseSpec",
    "PersonSpec",
    "PersonPay",
    "InvoiceData",
    "InvoiceComputation",
    "EquipmentSuggestion",
    "ExpenseSuggestions",
    "PayChange",
    "RecomputeResult",
    "SaveResult",
    "PayResult",
    "build_lines",
    "normalize_payments",
    "compute_invoice",
    "payable_assignments",
    "person_specs",
    "suggest_expenses",
    "evaluate",
    "save_invoice",
    "recompute",
    "reopen_invoice",
    "void_invoice",
    "mark_paid",
    "unmark_paid",
    "paid_at_from_date",
    "paid_amounts",
    "paid_differences",
    "was_saved_with_nonce",
    "invoice_snapshot",
]

logger = logging.getLogger(__name__)

#: Largest quantity of one product row ("sane maximum"; an event sells hundreds, not millions).
MAX_QTY = 5_000
#: Largest "تعداد محاسبه برای حقوق نیرو" of one row.
MAX_WEIGHT = 50
#: Largest single money input (100 billion Toman); sums stay far below :data:`MAX_TOMAN`.
MAX_AMOUNT = 100_000_000_000

#: Invoices that count in payroll and reports (a reopened draft or a void invoice does not).
FINAL_STATUSES: tuple[InvoiceStatus, ...] = (InvoiceStatus.SUBMITTED, InvoiceStatus.SETTLED)
#: Event states in which an invoice may be written.
INVOICEABLE_EVENT_STATUSES = frozenset({EventStatus.CONFIRMED, EventStatus.DONE, EventStatus.INVOICED})
#: Kinds a user may pick for a free-form expense line (venue rent and staff pay are automatic).
EXTRA_EXPENSE_KINDS: tuple[ExpenseKind, ...] = (
    ExpenseKind.EQUIPMENT_RENTAL,
    ExpenseKind.MATERIAL,
    ExpenseKind.OTHER,
)

_CATEGORY_ORDER = {category: index for index, category in enumerate(PRODUCT_CATALOG)}
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_IRAN_OFFSET = dt.timedelta(hours=3, minutes=30)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class LedgerError(ValueError):
    """The invoice data or the requested action is not acceptable (message is Persian)."""

    def __init__(self, message: str, *, code: str = "invalid", field: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class StaleInvoice(LedgerError):
    """The form was built from an older revision of the invoice.

    ``replay`` is true when *this very submission* (same nonce) was already saved,
    i.e. a double submit: callers answer "already saved" instead of an error.
    """

    def __init__(self, current_revision: int, *, replay: bool = False) -> None:
        super().__init__(
            "این فاکتور قبلاً ثبت شده است." if replay else "این فاکتور در همین فاصله تغییر کرده است؛ صفحه را تازه کنید.",
            code="replay" if replay else "stale",
        )
        self.current_revision = current_revision
        self.replay = replay


# --------------------------------------------------------------------------
# small coercions
# --------------------------------------------------------------------------


def _as_int(value: Any, label: str, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    """A genuine integer (digits in any script, ``1,234`` allowed); never a float or bool."""
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerError(f"{label} باید عدد صحیح باشد.")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise LedgerError(f"{label} باید عدد صحیح باشد.")
        value = int(value)
    if isinstance(value, str):
        text = normalize_digits(value).replace(",", "").strip()
        if not re.fullmatch(r"[+-]?\d{1,18}", text):
            raise LedgerError(f"{label} باید عدد صحیح باشد.")
        value = int(text)
    if not isinstance(value, numbers.Integral):
        raise LedgerError(f"{label} باید عدد صحیح باشد.")
    number = int(value)
    if number < minimum:
        raise LedgerError(f"{label} نباید کمتر از {to_persian_digits(minimum)} باشد.")
    if maximum is not None and number > maximum:
        raise LedgerError(f"{label} نباید بیشتر از {to_persian_digits(format(maximum, ','))} باشد.")
    return number


def _guard_total(value: int, label: str) -> int:
    """Totals must stay inside the BIGINT-safe range (a huge rate times many units can overflow)."""
    if abs(value) > MAX_TOMAN:
        raise LedgerError(f"{label} از حد مجاز بیشتر می‌شود؛ مبالغ و تعدادها را بررسی کنید.", code="overflow")
    return value


def _money_text(value: int) -> str:
    return format_toman(value, persian_digits=True)


def _canonical_size(raw: Any) -> str:
    """``'۵۰ × ۷۰'`` / ``'70x50'`` -> ``'50x70'`` (integer centimetres, short side first)."""
    try:
        return normalize_size_label(normalize_size(str(raw)))
    except ValueError:
        raise LedgerError(
            f"ابعاد «{raw}» نامعتبر است؛ مثلاً ۵۰×۷۰ (به سانتی‌متر و با عدد صحیح).", code="bad_size"
        ) from None


# --------------------------------------------------------------------------
# sold lines
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineSpec:
    """One sold product row: *qty* items of a category/size, each counting *weight* units.

    Exposes ``qty`` / ``weight`` / ``counts`` so it can be handed straight to
    :func:`app.services.payroll.units_from_lines`.
    """

    category: ProductCategory
    size_label: str
    qty: int
    weight: int = 1
    counts: bool = True
    is_custom: bool = False

    @property
    def units(self) -> int:
        """Units this row contributes to commission (0 when it does not count)."""
        return self.qty * self.weight if self.counts else 0

    @property
    def label(self) -> str:
        """Persian row label, e.g. ``چاپ 16×21``; custom rows say so in their category label."""
        base = PRODUCT_CATALOG[self.category]["label"]
        return f"{base} {format_size(self.size_label)}" if self.size_label else base

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "size_label": self.size_label,
            "label": self.label,
            "qty": self.qty,
            "weight": self.weight,
            "counts": self.counts,
            "units": self.units,
            "is_custom": self.is_custom,
        }


def _row_value(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(row, Mapping):
            if name in row and row[name] is not None:
                return row[name]
        elif getattr(row, name, None) is not None:
            return getattr(row, name)
    return default


def build_lines(rows: Iterable[Any]) -> list[LineSpec]:
    """Validate and normalise sold rows into :class:`LineSpec` values.

    Each row is a mapping (or an object) with ``category``, ``size`` /
    ``size_label`` (required for printed / framed / chassis products, forbidden
    for files), ``qty``, optional ``weight`` (default 1), ``counts`` /
    ``counts_for_commission`` (default true) and ``custom`` / ``is_custom_size``
    (a "+ other size" row).  Sizes are canonicalised (``print_yield.normalize_size``
    then the integer ``WxH`` form the database stores).  Rows with ``qty == 0``
    are dropped.  The result is ordered like the popup: category order, standard
    sizes first, then custom rows in the order given.  Raises :class:`LedgerError`.
    """
    built: list[tuple[tuple, LineSpec]] = []
    for position, row in enumerate(rows or ()):
        if isinstance(row, LineSpec):
            category = row.category
            raw_size, qty, weight, counts, custom = row.size_label, row.qty, row.weight, row.counts, row.is_custom
        else:
            try:
                category = ProductCategory.coerce(_row_value(row, "category"))
            except ValueError:
                raise LedgerError("دستهٔ محصول نامعتبر است.", code="bad_category") from None
            raw_size = _row_value(row, "size", "size_label", default="")
            qty = _row_value(row, "qty", default=0)
            weight = _row_value(row, "weight", "commission_weight", default=1)
            counts = _row_value(row, "counts", "counts_for_commission", default=True)
            custom = bool(_row_value(row, "custom", "is_custom", "is_custom_size", default=False))
        meta = PRODUCT_CATALOG[category]
        name = meta["label"]
        qty = _as_int(qty, f"تعداد {name}", maximum=MAX_QTY)
        weight = _as_int(weight, f"تعداد محاسبه برای حقوق ({name})", maximum=MAX_WEIGHT)
        if isinstance(counts, str):
            counts = counts.strip().lower() not in ("0", "false", "off", "no", "")
        counts = bool(counts)
        if custom and not meta["allows_custom_sizes"]:
            raise LedgerError(f"برای «{name}» ابعاد دیگر تعریف نمی‌شود.", code="bad_category")
        if meta["requires_size"]:
            if raw_size is None or not str(raw_size).strip():
                raise LedgerError(f"ابعاد {name} را وارد کنید.", code="bad_size")
            size = _canonical_size(raw_size)
        else:
            size = ""
        if qty == 0:
            continue
        spec = LineSpec(category, size, qty, weight, counts, custom)
        std_index = STANDARD_SIZES.index(size) if (size in STANDARD_SIZES and not custom) else 0
        built.append(((_CATEGORY_ORDER[category], 1 if custom else 0, std_index, position), spec))
    built.sort(key=lambda item: item[0])
    return [spec for _, spec in built]


# --------------------------------------------------------------------------
# payments, expenses, people
# --------------------------------------------------------------------------


def normalize_payments(payments: Optional[Mapping[Any, Any]]) -> dict[PaymentMethod, int]:
    """``{method: amount}`` with all three methods present (missing = 0), integers in range."""
    out = {method: 0 for method in PaymentMethod}
    for key, value in (payments or {}).items():
        try:
            method = PaymentMethod.coerce(key)
        except ValueError:
            raise LedgerError("روش دریافت نامعتبر است.", code="bad_payment") from None
        out[method] = _as_int(value, f"جمع {method.label}", maximum=MAX_AMOUNT)
    return out


@dataclass(frozen=True)
class ExpenseSpec:
    """A non-staff expense line (staff pay is always derived from the payroll formula)."""

    kind: ExpenseKind
    label: str
    amount: int
    is_auto: bool = False
    usage_id: Optional[int] = None  # EquipmentUsage this rental line comes from

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "kind_label": self.kind.label,
            "label": self.label,
            "amount": self.amount,
            "is_auto": self.is_auto,
            "usage_id": self.usage_id,
        }


@dataclass(frozen=True)
class PersonSpec:
    """One paid person of the event: the assignment, who it is and the terms snapshot."""

    key: int  # assignment id
    name: str
    role: Skill
    terms: payroll.PayTerms
    user_id: Optional[int] = None  # account of the person (directly or via the roster), if any


@dataclass(frozen=True)
class PersonPay:
    person: PersonSpec
    pay: payroll.PayBreakdown

    def as_dict(self) -> dict[str, Any]:
        data = self.pay.as_dict()
        data.update(
            assignment_id=self.person.key,
            name=self.person.name,
            role=self.person.role.value,
            role_label=self.person.role.label,
        )
        return data


@dataclass(frozen=True)
class InvoiceComputation:
    """Everything the invoice popup shows, computed once and identically for preview and save."""

    lines: tuple[LineSpec, ...]
    payments: dict[PaymentMethod, int]
    expenses: tuple[ExpenseSpec, ...]  # without staff pay
    people: tuple[PersonPay, ...]
    units: int
    income: int
    staff_total: int
    expense_total: int
    net_profit: int
    expense_by_kind: dict[ExpenseKind, int]

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (what ``POST .../invoice/preview`` returns)."""
        return {
            "units": self.units,
            "lines": [line.as_dict() for line in self.lines],
            "income": {**{m.value: a for m, a in self.payments.items()}, "total": self.income},
            "expenses": {
                "venue_rent": self.expense_by_kind.get(ExpenseKind.VENUE_RENT, 0),
                "staff_pay": self.staff_total,
                "equipment_rental": self.expense_by_kind.get(ExpenseKind.EQUIPMENT_RENTAL, 0),
                "material": self.expense_by_kind.get(ExpenseKind.MATERIAL, 0),
                "other": self.expense_by_kind.get(ExpenseKind.OTHER, 0),
                "non_staff": self.expense_total - self.staff_total,
                "total": self.expense_total,
            },
            "staff_total": self.staff_total,
            "expense_total": self.expense_total,
            "net_profit": self.net_profit,
            "people": [person.as_dict() for person in self.people],
        }


def compute_invoice(
    lines: Iterable[Any],
    payments: Optional[Mapping[Any, Any]] = None,
    expenses: Iterable[ExpenseSpec] = (),
    people: Iterable[PersonSpec] = (),
) -> InvoiceComputation:
    """Totals and the pay breakdown of every person (pure; no database).

    ``lines`` are :class:`LineSpec` values (raw rows are passed through
    :func:`build_lines`), ``payments`` a ``{method: amount}`` mapping,
    ``expenses`` the non-staff expense lines and ``people`` the paid assignments.
    All people share the same ``units`` (A3) and are paid with their own terms.
    """
    items = list(lines or ())
    if not all(isinstance(item, LineSpec) for item in items):
        items = build_lines(items)
    paid_in = normalize_payments(payments)
    extra = tuple(expenses or ())
    for spec in extra:
        if spec.kind == ExpenseKind.STAFF_PAY:
            raise LedgerError("هزینهٔ نیروها خودکار محاسبه می‌شود و دستی وارد نمی‌شود.", code="bad_expense")
        _as_int(spec.amount, spec.label or spec.kind.label, maximum=MAX_AMOUNT)

    units = _guard_total(payroll.units_from_lines(items), "تعداد کل محصولات")
    paid_people: list[PersonPay] = []
    for person in people or ():
        pay = payroll.compute_pay(person.terms, units)
        _guard_total(pay.total, f"حقوق {person.name}")
        paid_people.append(PersonPay(person, pay))

    income = _guard_total(sum(paid_in.values()), "جمع دریافتی‌ها")
    staff_total = _guard_total(sum(p.pay.total for p in paid_people), "جمع حقوق نیروها")
    by_kind: dict[ExpenseKind, int] = {}
    for spec in extra:
        by_kind[spec.kind] = by_kind.get(spec.kind, 0) + spec.amount
    expense_total = _guard_total(staff_total + sum(by_kind.values()), "جمع هزینه‌ها")
    return InvoiceComputation(
        lines=tuple(items),
        payments=paid_in,
        expenses=extra,
        people=tuple(paid_people),
        units=units,
        income=income,
        staff_total=staff_total,
        expense_total=expense_total,
        net_profit=income - expense_total,
        expense_by_kind=by_kind,
    )


# --------------------------------------------------------------------------
# database side: who is paid, what is suggested
# --------------------------------------------------------------------------


def payable_assignments(event: Event) -> list[Assignment]:
    """The assignments that are paid for *event*: accepted, confirmed or completed.

    Invited / declined / cancelled people earn nothing.  Ordered like the
    on-site workflow (presenter, photographer, seller, editor), then by id.
    """
    stmt = (
        sa.select(Assignment)
        .where(Assignment.event_id == event.id, Assignment.status.in_(sorted(COMMITTED_ASSIGNMENT_STATUSES)))
        .options(joinedload(Assignment.user), joinedload(Assignment.roster_member))
        .order_by(Assignment.id)
    )
    rows = list(db.session.execute(stmt).unique().scalars())
    order = {role: index for index, role in enumerate(ROLE_DISPLAY_ORDER)}
    rows.sort(key=lambda a: (order.get(a.role, len(order)), a.id))
    return rows


def person_specs(assignments: Iterable[Assignment]) -> list[PersonSpec]:
    """:class:`PersonSpec` values from assignments (the terms snapshot is read, never the live asking terms)."""
    return [
        PersonSpec(
            key=a.id,
            name=a.person_name or "—",
            role=a.role,
            terms=a.pay_terms(),
            user_id=a.person_user_id,
        )
        for a in assignments
    ]


@dataclass(frozen=True)
class EquipmentSuggestion:
    usage_id: int
    label: str
    amount: int


@dataclass(frozen=True)
class ExpenseSuggestions:
    """Defaults the invoice popup pre-fills (all editable except staff pay)."""

    venue_rent: int
    equipment: tuple[EquipmentSuggestion, ...]


def suggest_expenses(event: Event) -> ExpenseSuggestions:
    """Venue rent (``AtelierHall.rent_amount``, else the atelier default) and rented equipment.

    Equipment lines come from the event's :class:`EquipmentUsage` rows (the rent
    was snapshotted when the gear was attached); owned gear costs nothing and is
    not suggested.
    """
    atelier = event.atelier
    rent = atelier.rent_for_hall(event.hall_id) if atelier is not None else 0
    usages = db.session.execute(
        sa.select(EquipmentUsage)
        .where(EquipmentUsage.event_id == event.id)
        .options(joinedload(EquipmentUsage.equipment))
        .order_by(EquipmentUsage.id)
    ).scalars()
    gear = tuple(
        EquipmentSuggestion(u.id, u.equipment.name, u.rent_cost_snapshot)
        for u in usages
        if u.rent_cost_snapshot > 0 or u.equipment.ownership == EquipmentOwnership.RENTED
    )
    return ExpenseSuggestions(venue_rent=rent, equipment=gear)


@dataclass(frozen=True)
class InvoiceData:
    """The user-entered part of an invoice (what the popup posts, already validated)."""

    lines: Sequence[LineSpec]
    payments: Mapping[Any, int]
    venue_rent: Optional[int] = None  # None = use the suggestion
    equipment: Mapping[int, int] = field(default_factory=dict)  # usage id -> amount; missing = suggestion
    extras: Sequence[ExpenseSpec] = ()
    notes: Optional[str] = None


def _expense_specs(event: Event, data: InvoiceData, suggestions: ExpenseSuggestions) -> list[ExpenseSpec]:
    """Venue rent + equipment + extra lines of *data*; flags values equal to the suggestion as automatic."""
    rent = suggestions.venue_rent if data.venue_rent is None else _as_int(data.venue_rent, "اجارهٔ باغ/تالار", maximum=MAX_AMOUNT)
    specs = [ExpenseSpec(ExpenseKind.VENUE_RENT, ExpenseKind.VENUE_RENT.label, rent, rent == suggestions.venue_rent)]
    known = {item.usage_id: item for item in suggestions.equipment}
    for usage_id in data.equipment:
        if usage_id not in known:
            raise LedgerError("تجهیزات انتخاب‌شده برای این مراسم ثبت نشده است.", code="bad_equipment")
    for item in suggestions.equipment:
        amount = data.equipment.get(item.usage_id, item.amount)
        amount = _as_int(amount, f"اجارهٔ {item.label}", maximum=MAX_AMOUNT)
        specs.append(
            ExpenseSpec(ExpenseKind.EQUIPMENT_RENTAL, f"اجاره: {item.label}", amount, amount == item.amount, item.usage_id)
        )
    for extra in data.extras:
        if extra.kind not in EXTRA_EXPENSE_KINDS:
            raise LedgerError("نوع هزینه نامعتبر است.", code="bad_expense")
        label = (extra.label or "").strip()
        if not label:
            raise LedgerError("برای هر هزینهٔ اضافی عنوان بنویسید.", code="bad_expense")
        specs.append(ExpenseSpec(extra.kind, label[:200], _as_int(extra.amount, label, maximum=MAX_AMOUNT)))
    return specs


def evaluate(event: Event, data: InvoiceData) -> InvoiceComputation:
    """Compute *data* for *event* with the event's real payable assignments (the preview and save path)."""
    suggestions = suggest_expenses(event)
    return compute_invoice(
        build_lines(data.lines),
        data.payments,
        _expense_specs(event, data, suggestions),
        person_specs(payable_assignments(event)),
    )


# --------------------------------------------------------------------------
# audit helpers
# --------------------------------------------------------------------------


def invoice_snapshot(invoice: Invoice) -> dict[str, Any]:
    """Small JSON-able picture of an invoice for the audit trail (old / new values)."""
    return {
        "status": invoice.status.value,
        "revision": invoice.revision,
        "units": invoice.units_total,
        "income": invoice.income_total,
        "expenses": invoice.expense_total,
        "net": invoice.net_profit,
        "lines": [
            f"{line.category.value}:{line.size_label or '-'}x{line.qty}w{line.commission_weight}{'' if line.counts_for_commission else '!'}"
            for line in invoice.lines
        ],
        "payments": {p.method.value: p.amount for p in invoice.payments},
        "expense_lines": [f"{e.kind.value}:{e.amount}" for e in invoice.expenses],
        "payroll": {str(pl.assignment_id): pl.total for pl in invoice.payroll_lines},
    }


def was_saved_with_nonce(invoice: Invoice, nonce: Optional[str]) -> bool:
    """Did a previous save of *invoice* carry this one-time form token? (double-submit detection)"""
    if not nonce or not _NONCE_RE.match(nonce):
        return False
    rows = db.session.execute(
        sa.select(AuditLog.data)
        .where(
            AuditLog.entity_type == Invoice.__tablename__,
            AuditLog.entity_id == invoice.id,
            AuditLog.action.in_(("invoice.submitted", "invoice.updated")),
        )
        .order_by(AuditLog.id.desc())
        .limit(20)
    ).scalars()
    return any(isinstance(data, dict) and data.get("nonce") == nonce for data in rows)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PayChange:
    """What a (re)computation did to one person's payroll line."""

    assignment_id: int
    person_name: str
    role: Skill
    old_total: Optional[int]  # None: the line is new
    new_total: int
    paid: bool  # the line was already paid
    user_id: Optional[int] = None

    @property
    def changed(self) -> bool:
        return self.old_total != self.new_total


@dataclass
class RecomputeResult:
    computation: InvoiceComputation
    changes: list[PayChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SaveResult(RecomputeResult):
    invoice: Optional[Invoice] = None
    created: bool = False
    resubmitted: bool = False  # a reopened (draft) invoice was submitted again
    event_status_changed: bool = False
    notified: int = 0


def _staff_label(person_role: Skill, name: str) -> str:
    return f"{person_role.label} - {name}"[:200]


def _paid_warning(name: str, old: int, new: int) -> str:
    return (
        f"حقوق {name} پیش‌تر پرداخت شده بود؛ مبلغ جدید {_money_text(new)} تومان با مبلغ قبلی "
        f"{_money_text(old)} تومان تفاوت دارد و باید در برگهٔ حقوق بررسی شود."
    )


def _apply_payroll(invoice: Invoice, comp: InvoiceComputation) -> tuple[list[PayChange], list[str]]:
    """Create / update the payroll lines and the automatic staff-pay expenses of *invoice*.

    Existing lines are updated **in place** so their paid markers survive; lines
    whose person is no longer payable are removed unless they are already paid.
    """
    existing = {line.assignment_id: line for line in invoice.payroll_lines}
    staff_expenses = {
        e.assignment_id: e for e in invoice.expenses if e.kind == ExpenseKind.STAFF_PAY and e.assignment_id is not None
    }
    changes: list[PayChange] = []
    warnings: list[str] = []
    live: set[int] = set()
    for item in comp.people:
        person, pay = item.person, item.pay
        live.add(person.key)
        line = existing.get(person.key)
        old_total = line.total if line is not None else None
        is_new = line is None
        if is_new:
            line = PayrollLine()
            line.assignment = db.session.get(Assignment, person.key)  # load before the line enters the session
        line.role = person.role
        line.person_name = person.name[:120]
        line.units = pay.units
        line.base = pay.base
        line.commission = pay.commission
        line.bonus = pay.bonus
        line.total = pay.total
        line.applied_tier = (
            {"threshold": pay.applied_tier.threshold, "amount": pay.applied_tier.amount}
            if pay.applied_tier is not None
            else None
        )
        if is_new:
            invoice.payroll_lines.append(line)  # complete row first: an autoflush must never see a half-built one
        was_paid = line.is_paid
        if was_paid and old_total is not None and old_total != pay.total:
            warnings.append(_paid_warning(person.name, old_total, pay.total))
        expense = staff_expenses.get(person.key)
        if expense is None:
            invoice.expenses.append(
                ExpenseLine(
                    kind=ExpenseKind.STAFF_PAY,
                    label=_staff_label(person.role, person.name),
                    amount=pay.total,
                    is_auto=True,
                    assignment_id=person.key,
                )
            )
        else:
            expense.amount = pay.total
            expense.label = _staff_label(person.role, person.name)
            expense.is_auto = True
        changes.append(
            PayChange(person.key, person.name, person.role, old_total, pay.total, was_paid, person.user_id)
        )
    for assignment_id, line in existing.items():
        if assignment_id in live:
            continue
        if line.is_paid:
            warnings.append(
                f"{line.person_name} دیگر در فهرست حقوق‌بگیران این مراسم نیست، اما حقوق او پرداخت شده است؛ "
                "ردیف او نگه داشته شد."
            )
            continue
        orphan_expense = staff_expenses.get(assignment_id)
        if orphan_expense is not None:
            invoice.expenses.remove(orphan_expense)
        invoice.payroll_lines.remove(line)
    return changes, warnings


def paid_differences(invoice: Invoice, comp: InvoiceComputation) -> list[str]:
    """Warnings for people whose payroll line is already paid but whose total would change under *comp*."""
    paid = {line.assignment_id: line for line in invoice.payroll_lines if line.is_paid}
    return [
        _paid_warning(item.person.name, paid[item.person.key].total, item.pay.total)
        for item in comp.people
        if item.person.key in paid and paid[item.person.key].total != item.pay.total
    ]


def _sync_settlement(invoice: Invoice) -> None:
    """``submitted <-> settled``: settled exactly when every payroll line is paid."""
    if invoice.status not in FINAL_STATUSES:
        return
    lines = list(invoice.payroll_lines)
    everyone_paid = bool(lines) and all(line.is_paid for line in lines)
    invoice.status = InvoiceStatus.SETTLED if everyone_paid else InvoiceStatus.SUBMITTED


def _line_specs_from_rows(invoice: Invoice) -> list[LineSpec]:
    return [
        LineSpec(l.category, l.size_label, l.qty, l.commission_weight, l.counts_for_commission, l.is_custom_size)
        for l in invoice.lines
    ]


def recompute(invoice: Invoice) -> RecomputeResult:
    """Recompute payroll, staff-pay expenses and the totals of *invoice* from its stored rows.

    Sold lines, payments and the non-staff expenses are taken as stored; the paid
    people and their terms snapshots are read fresh from the event's assignments.
    Paid payroll lines keep their paid markers (a changed total is reported in
    ``warnings``).  Use it after anything that can change the formula inputs.
    """
    if invoice.status == InvoiceStatus.VOID:
        raise LedgerError("فاکتور باطل‌شده قابل ویرایش نیست.", code="void")
    event = invoice.event
    other = tuple(
        ExpenseSpec(e.kind, e.label, e.amount, e.is_auto, e.equipment_usage_id)
        for e in invoice.expenses
        if e.kind != ExpenseKind.STAFF_PAY
    )
    comp = compute_invoice(
        _line_specs_from_rows(invoice),
        {p.method: p.amount for p in invoice.payments},
        other,
        person_specs(payable_assignments(event)),
    )
    changes, warnings = _apply_payroll(invoice, comp)
    db.session.flush()
    invoice.refresh_totals()
    _sync_settlement(invoice)
    db.session.flush()
    return RecomputeResult(comp, changes, warnings)


def _write_children(invoice: Invoice, comp: InvoiceComputation) -> None:
    """Replace the sold lines / payments / non-staff expenses of *invoice* with *comp*'s."""
    persisted = invoice.id is not None
    invoice.lines.clear()
    for e in [e for e in invoice.expenses if e.kind != ExpenseKind.STAFF_PAY]:
        invoice.expenses.remove(e)
    if persisted:
        db.session.flush()  # delete first: expense lines are unique per equipment usage
    for index, spec in enumerate(comp.lines):
        invoice.lines.append(
            InvoiceLine(
                category=spec.category,
                size_label=spec.size_label,
                is_custom_size=spec.is_custom,
                qty=spec.qty,
                commission_weight=spec.weight,
                counts_for_commission=spec.counts,
                sort_order=index,
            )
        )
    by_method = {p.method: p for p in invoice.payments}
    for method, amount in comp.payments.items():
        payment = by_method.get(method)
        if payment is None:
            invoice.payments.append(InvoicePayment(method=method, amount=amount))
        else:
            payment.amount = amount
    for spec in comp.expenses:
        invoice.expenses.append(
            ExpenseLine(
                kind=spec.kind,
                label=spec.label[:200],
                amount=spec.amount,
                is_auto=spec.is_auto,
                equipment_usage_id=spec.usage_id,
            )
        )


def _event_text(event: Event) -> str:
    hall = event.hall.name if event.hall is not None else ""
    return f"{hall} - {jalali.to_jalali_str(event.date, 'long', persian_digits=True)}".strip(" -")


def _notify_pay(event: Event, changes: Sequence[PayChange]) -> int:
    sent = 0
    for change in changes:
        if change.user_id is None or not change.changed:
            continue
        created = change.old_total is None
        row = notifications.notify(
            change.user_id,
            NotificationKind.INVOICE_SUBMITTED,
            "حقوق شما ثبت شد" if created else "حقوق شما به‌روز شد",
            f"{change.role.label} - مراسم {_event_text(event)}: {_money_text(change.new_total)} تومان",
            entity=event,
        )
        sent += 1 if row is not None else 0
    return sent


def save_invoice(
    event: Event,
    data: InvoiceData,
    actor: Any,
    *,
    expected_revision: Optional[int] = None,
    nonce: Optional[str] = None,
) -> SaveResult:
    """Create or update the invoice of *event* in the caller's transaction.

    * only ``confirmed`` / ``done`` / ``invoiced`` events can be invoiced; a
      ``confirmed`` event is moved ``done -> invoiced`` first;
    * ``expected_revision`` is the revision the form was built from.  A different
      current revision raises :class:`StaleInvoice` (``replay=True`` when this
      submission's ``nonce`` was already saved, i.e. a double submit);
    * sold lines / payments / non-staff expenses are replaced, payroll and staff
      pay are recomputed in place (paid lines keep their paid markers);
    * the pay terms of every committed assignment are locked, the change is
      audited (``invoice.submitted`` / ``invoice.updated`` with old and new
      values) and each person with an account is notified;
    * a concurrent create of the same event's invoice surfaces as
      ``sqlalchemy.exc.IntegrityError`` from the flush - roll back and treat it
      like :class:`StaleInvoice`.

    Raises :class:`LedgerError` for invalid data or an event that cannot be
    invoiced.  ``invoice.saved`` is published by the caller after the commit.
    """
    if event.atelier_id is None:
        raise LedgerError("برای این مراسم اتلیه‌ای تعیین نشده است.", code="event_state")
    if event.status not in INVOICEABLE_EVENT_STATUSES:
        raise LedgerError(
            f"برای مراسمی که «{event.status.label}» است نمی‌توان فاکتور ثبت کرد.", code="event_state"
        )
    invoice = db.session.execute(sa.select(Invoice).where(Invoice.event_id == event.id)).scalar_one_or_none()
    if invoice is not None:
        if invoice.status == InvoiceStatus.VOID:
            raise LedgerError("فاکتور این مراسم باطل شده است و دیگر قابل ویرایش نیست.", code="void")
        if expected_revision is not None and expected_revision != invoice.revision:
            raise StaleInvoice(invoice.revision, replay=was_saved_with_nonce(invoice, nonce))

    comp = evaluate(event, data)  # validates everything before any write
    before = invoice_snapshot(invoice) if invoice is not None else None
    created = invoice is None
    resubmitted = invoice is not None and invoice.status == InvoiceStatus.DRAFT
    status_changed = False

    if created:
        invoice = Invoice(
            event=event,
            atelier_id=event.atelier_id,
            status=InvoiceStatus.SUBMITTED,
            created_by_id=audit.actor_id(actor),
            revision=1,
        )
        db.session.add(invoice)
        db.session.flush()  # a concurrent create fails here (unique event_id)
    else:
        invoice.revision += 1
    invoice.notes = (data.notes or "").strip() or None
    invoice.submitted_by_id = audit.actor_id(actor)
    invoice.submitted_at = utcnow()
    if invoice.status == InvoiceStatus.DRAFT:
        invoice.status = InvoiceStatus.SUBMITTED

    try:
        if event.status == EventStatus.CONFIRMED:
            staffing.change_event_status(event, EventStatus.DONE, actor, reason="ثبت فاکتور")
            status_changed = True
        if event.status == EventStatus.DONE:
            staffing.change_event_status(event, EventStatus.INVOICED, actor, reason="ثبت فاکتور")
            status_changed = True
    except staffing.StaffingError as exc:
        logger.warning("event %s could not be moved to invoiced: %s", event.id, exc)
        raise LedgerError("وضعیت مراسم اجازهٔ ثبت فاکتور را نمی‌دهد؛ وضعیت مراسم را بررسی کنید.", code="event_state") from exc

    _write_children(invoice, comp)
    db.session.flush()
    result = recompute(invoice)  # payroll + staff-pay expenses + totals (+ settled/submitted)
    staffing.lock_event_terms(event, actor)

    audit.log(
        actor,
        "invoice.submitted" if (created or resubmitted) else "invoice.updated",
        invoice,
        {
            "event_id": event.id,
            "nonce": nonce if (nonce and _NONCE_RE.match(nonce)) else None,
            "before": before,
            "after": invoice_snapshot(invoice),
            "warnings": result.warnings or None,
        },
    )
    line_ids = {line.assignment_id: line.id for line in invoice.payroll_lines}
    for change in result.changes:
        if change.paid and change.changed and change.assignment_id in line_ids:
            audit.log(
                actor,
                "payroll.recalculated",
                (PayrollLine.__tablename__, line_ids[change.assignment_id]),
                {"old_total": change.old_total, "new_total": change.new_total, "invoice_id": invoice.id},
            )
    notified = _notify_pay(event, result.changes)
    return SaveResult(
        computation=result.computation,
        changes=result.changes,
        warnings=result.warnings,
        invoice=invoice,
        created=created,
        resubmitted=resubmitted,
        event_status_changed=status_changed,
        notified=notified,
    )


def _reason(reason: Optional[str]) -> str:
    text = " ".join((reason or "").split())
    if len(text) < 3:
        raise LedgerError("دلیل را بنویسید (دست‌کم سه نویسه).", code="reason_required")
    if len(text) > 300:
        raise LedgerError("دلیل نباید بیش از ۳۰۰ نویسه باشد.", code="reason_required")
    return text


def _release_event(invoice: Invoice, actor: Any, reason: str) -> None:
    """Back to ``done`` and unlock the pay terms (reopen / void)."""
    event = invoice.event
    try:
        if event.status == EventStatus.INVOICED:
            staffing.change_event_status(event, EventStatus.DONE, actor, reason=reason)
        for assignment in list(event.assignments):
            if assignment.is_terms_locked:
                staffing.unlock_terms(assignment, actor, reason=reason)
    except staffing.StaffingError as exc:
        logger.warning("event %s could not be released: %s", event.id, exc)
        raise LedgerError("وضعیت مراسم اجازهٔ این کار را نمی‌دهد؛ وضعیت مراسم را بررسی کنید.", code="event_state") from exc


def reopen_invoice(invoice: Invoice, actor: Any, reason: Optional[str]) -> Invoice:
    """``submitted -> draft``: the event returns to ``done`` and pay terms are unlocked.

    Needed when an assignment's terms must be corrected after the invoice was
    submitted.  A reason is mandatory and audited; saving the invoice again
    submits it once more.
    """
    text = _reason(reason)
    if invoice.status != InvoiceStatus.SUBMITTED:
        raise LedgerError(
            "فقط فاکتور «ثبت شده» را می‌توان بازگشایی کرد؛ اگر تسویه شده است ابتدا پرداخت‌ها را برگردانید.",
            code="bad_state",
        )
    before = invoice_snapshot(invoice)
    invoice.status = InvoiceStatus.DRAFT
    invoice.revision += 1
    db.session.flush()
    _release_event(invoice, actor, text)
    audit.log(actor, "invoice.reopened", invoice, {"reason": text, "before": before, "revision": invoice.revision})
    return invoice


def void_invoice(invoice: Invoice, actor: Any, reason: Optional[str]) -> Invoice:
    """Cancel an invoice for good (kept for the audit trail, excluded from payroll and reports)."""
    text = _reason(reason)
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.SUBMITTED):
        raise LedgerError(
            "این فاکتور تسویه شده است؛ ابتدا پرداخت‌ها را برگردانید و سپس آن را باطل کنید.", code="bad_state"
        )
    if any(line.is_paid for line in invoice.payroll_lines):
        raise LedgerError(
            "برای این فاکتور حقوق پرداخت‌شده ثبت شده است؛ ابتدا پرداخت‌ها را برگردانید.", code="has_payments"
        )
    before = invoice_snapshot(invoice)
    invoice.status = InvoiceStatus.VOID
    invoice.revision += 1
    db.session.flush()
    _release_event(invoice, actor, text)
    audit.log(actor, "invoice.voided", invoice, {"reason": text, "before": before, "revision": invoice.revision})
    return invoice


# --------------------------------------------------------------------------
# paying staff
# --------------------------------------------------------------------------


def paid_at_from_date(day: dt.date) -> dt.datetime:
    """Naive-UTC timestamp for "paid on *day*" (noon Iran time, so the Jalali day never shifts)."""
    return dt.datetime.combine(day, dt.time(12, 0)) - _IRAN_OFFSET


@dataclass
class PayResult:
    paid: list[PayrollLine] = field(default_factory=list)
    skipped: list[PayrollLine] = field(default_factory=list)  # already paid

    @property
    def total(self) -> int:
        return sum(line.total for line in self.paid)


def mark_paid(
    lines: Iterable[PayrollLine],
    actor: Any,
    *,
    paid_on: dt.date,
    method: Any,
    note: Optional[str] = None,
) -> PayResult:
    """Record that payroll *lines* were paid (date, method, note); already-paid lines are skipped.

    Audits ``payroll.paid`` per line (with the amount, so a later recalculation
    can be flagged), notifies each person once and settles invoices whose lines
    are all paid.  Lines of void or draft invoices are refused.
    """
    try:
        payout = PayoutMethod.coerce(method)
    except ValueError:
        raise LedgerError("روش پرداخت نامعتبر است.", code="bad_method") from None
    if not isinstance(paid_on, dt.date):
        raise LedgerError("تاریخ پرداخت نامعتبر است.", code="bad_date")
    note_text = " ".join((note or "").split())[:300] or None
    stamp = paid_at_from_date(paid_on)
    result = PayResult()
    invoices: dict[int, Invoice] = {}
    per_user: dict[int, list[PayrollLine]] = {}
    for line in lines:
        if line.invoice.status not in FINAL_STATUSES:
            raise LedgerError("حقوق فاکتور باطل‌شده یا بازگشایی‌شده پرداخت نمی‌شود.", code="bad_state")
        if line.is_paid:
            result.skipped.append(line)
            continue
        line.paid_at = stamp
        line.paid_method = payout
        line.paid_note = note_text
        result.paid.append(line)
        invoices[line.invoice_id] = line.invoice
        user_id = line.assignment.person_user_id
        if user_id is not None:
            per_user.setdefault(user_id, []).append(line)
    db.session.flush()
    for line in result.paid:
        audit.log(
            actor,
            "payroll.paid",
            line,
            {
                "amount": line.total,
                "paid_on": paid_on.isoformat(),
                "method": payout.value,
                "invoice_id": line.invoice_id,
                "assignment_id": line.assignment_id,
            },
        )
    for invoice in invoices.values():
        _sync_settlement(invoice)
    for user_id, owned in per_user.items():
        total = sum(line.total for line in owned)
        notifications.notify(
            user_id,
            NotificationKind.PAYROLL_PAID,
            "حقوق شما پرداخت شد",
            f"{to_persian_digits(len(owned))} مورد، جمعاً {_money_text(total)} تومان ({payout.label})",
            entity=owned[0],
        )
    db.session.flush()
    return result


def unmark_paid(line: PayrollLine, actor: Any) -> PayrollLine:
    """Undo a payment record (a mistake); a settled invoice goes back to ``submitted``."""
    if not line.is_paid:
        raise LedgerError("این حقوق هنوز پرداخت نشده است.", code="bad_state")
    old = {"paid_at": line.paid_at.isoformat() if line.paid_at else None, "method": line.paid_method, "amount": line.total}
    line.paid_at = None
    line.paid_method = None
    line.paid_note = None
    db.session.flush()
    audit.log(actor, "payroll.unpaid", line, {**old, "invoice_id": line.invoice_id})
    _sync_settlement(line.invoice)
    db.session.flush()
    return line


def paid_amounts(line_ids: Iterable[int]) -> dict[int, int]:
    """``{payroll line id: amount recorded when it was marked paid}`` (latest record wins).

    Compare with the line's current ``total``: a difference means the invoice was
    recalculated after the payment (shown as a warning on the payroll sheet).
    """
    ids = sorted({int(i) for i in line_ids})
    if not ids:
        return {}
    rows = db.session.execute(
        sa.select(AuditLog.entity_id, AuditLog.data)
        .where(
            AuditLog.entity_type == PayrollLine.__tablename__,
            AuditLog.action == "payroll.paid",
            AuditLog.entity_id.in_(ids),
        )
        .order_by(AuditLog.id)
    ).all()
    out: dict[int, int] = {}
    for entity_id, data in rows:
        if isinstance(data, dict) and isinstance(data.get("amount"), int):
            out[int(entity_id)] = data["amount"]
    return out
