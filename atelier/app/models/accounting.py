"""Event accounting: invoice, sold lines, payments, expenses, payroll.

Formulas (AGENTS.md, assumptions A2/A5):

* ``units``    = sum(qty * commission_weight) over lines with ``counts_for_commission``
* ``income``   = card_reader + card_to_card + cash payments
* ``expenses`` = venue rent + staff pay + equipment rental + materials + other
* ``net``      = income - expenses (may be negative)

The ``Invoice`` totals columns are a **snapshot** of these sums, written by
:meth:`Invoice.refresh_totals` whenever the invoice is saved, so lists and
reports never have to re-aggregate child rows.  Per-person payroll arithmetic
(base + commission + bonus) lives in ``app.services.payroll``.
"""

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    check_int,
    check_money,
    choice,
    fk,
    json_col,
    money,
    money_checks,
)
from app.models.event import Event
from app.models.terms import normalize_bonus_tiers
from app.services.constants import (
    PRODUCT_CATALOG,
    ExpenseKind,
    InvoiceStatus,
    PaymentMethod,
    PayoutMethod,
    ProductCategory,
    Skill,
    normalize_size_label,
)

_SIZED = sorted(c.value for c, meta in PRODUCT_CATALOG.items() if meta["requires_size"])
_UNSIZED = sorted(c.value for c, meta in PRODUCT_CATALOG.items() if not meta["requires_size"])
_SIZE_RULE = (
    "(category IN ({sized}) AND size_label <> '') OR (category IN ({unsized}) AND size_label = '')"
).format(
    sized=", ".join(f"'{c}'" for c in _SIZED),
    unsized=", ".join(f"'{c}'" for c in _UNSIZED),
)


class Invoice(BaseModel, TimestampMixin):
    """The result sheet of one event (فاکتور مراسم) - at most one per event."""

    __tablename__ = "invoices"
    __table_args__ = (
        sa.CheckConstraint("units_total >= 0", name="units_nonneg"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        *money_checks("income_total", "expense_total"),
        sa.Index("ix_invoices_atelier_status", "atelier_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = fk("events.id", ondelete="RESTRICT", unique=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="RESTRICT", index=False)
    status: Mapped[InvoiceStatus] = mapped_column(
        choice(InvoiceStatus, "status"), nullable=False, default=InvoiceStatus.DRAFT
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text)

    # totals snapshot (see refresh_totals)
    income_total: Mapped[int] = money()
    expense_total: Mapped[int] = money()
    net_profit: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    units_total: Mapped[int] = mapped_column(nullable=False, default=0)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)

    created_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    submitted_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    submitted_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)

    event: Mapped[Event] = relationship(back_populates="invoice")
    atelier: Mapped["Atelier"] = relationship()  # noqa: F821
    created_by: Mapped[Optional["User"]] = relationship(  # noqa: F821
        foreign_keys=[created_by_id]
    )
    submitted_by: Mapped[Optional["User"]] = relationship(  # noqa: F821
        foreign_keys=[submitted_by_id]
    )
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="(InvoiceLine.sort_order, InvoiceLine.id)",
    )
    payments: Mapped[list["InvoicePayment"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="InvoicePayment.id",
    )
    expenses: Mapped[list["ExpenseLine"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExpenseLine.id",
    )
    payroll_lines: Mapped[list["PayrollLine"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PayrollLine.id",
    )

    # -- derived sums (pure; do not touch the snapshot columns) ---------------

    def compute_units(self) -> int:
        """Sum of ``qty * commission_weight`` over commission-counting lines."""
        return sum(
            line.qty * line.commission_weight
            for line in self.lines
            if line.counts_for_commission
        )

    def compute_income(self) -> int:
        return sum(payment.amount for payment in self.payments)

    def compute_expenses(self) -> int:
        return sum(expense.amount for expense in self.expenses)

    def refresh_totals(self) -> None:
        """Write the totals snapshot from the child rows (call before commit)."""
        income = self.compute_income()
        expenses = self.compute_expenses()
        self.income_total = income
        self.expense_total = expenses
        self.net_profit = income - expenses
        self.units_total = self.compute_units()

    @property
    def is_locked(self) -> bool:
        """True once submitted: figures should only change through a reopen."""
        return self.status in (InvoiceStatus.SUBMITTED, InvoiceStatus.SETTLED)


class InvoiceLine(BaseModel):
    """Quantity sold of one product in one size ("+ other size" rows allowed).

    ``size_label`` is the canonical ``'16x21'`` form, or ``''`` for products
    without a size (raw/edited file).  ``commission_weight`` is the integer
    "تعداد محاسبه برای حقوق نیرو" (default 1) and ``counts_for_commission``
    implements the per-product opt-out of assumption A2.  Rows may repeat
    (e.g. two custom rows of the same size); totals simply add up.
    """

    __tablename__ = "invoice_lines"
    __table_args__ = (
        sa.CheckConstraint("qty >= 0", name="qty_nonneg"),
        sa.CheckConstraint("commission_weight >= 0", name="weight_nonneg"),
        sa.CheckConstraint(_SIZE_RULE, name="size_matches_category"),
        sa.Index("ix_invoice_lines_invoice_category", "invoice_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE", index=False)
    category: Mapped[ProductCategory] = mapped_column(
        choice(ProductCategory, "category"), nullable=False
    )
    size_label: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="")
    is_custom_size: Mapped[bool] = mapped_column(nullable=False, default=False)
    qty: Mapped[int] = mapped_column(nullable=False, default=0)
    commission_weight: Mapped[int] = mapped_column(nullable=False, default=1)
    counts_for_commission: Mapped[bool] = mapped_column(nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")

    @validates("size_label")
    def _validate_size(self, key: str, value: Any) -> str:
        if value is None or not str(value).strip():
            return ""
        return normalize_size_label(value)

    @validates("qty", "commission_weight")
    def _validate_counts(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=0)

    @property
    def units(self) -> int:
        """Units this line contributes to commission (0 when opted out)."""
        return self.qty * self.commission_weight if self.counts_for_commission else 0

    # Read-only aliases so a line can be fed straight to
    # ``payroll.units_from_lines`` (which looks for ``weight`` / ``counts``).
    @property
    def weight(self) -> int:
        return self.commission_weight

    @property
    def counts(self) -> bool:
        return bool(self.counts_for_commission)


class InvoicePayment(BaseModel):
    """Customer money received, one total per method (کارتخوان / کارت‌به‌کارت / نقد)."""

    __tablename__ = "invoice_payments"
    __table_args__ = (
        sa.UniqueConstraint("invoice_id", "method", name="uq_invoice_payments_method"),
        *money_checks("amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE", index=False)
    method: Mapped[PaymentMethod] = mapped_column(
        choice(PaymentMethod, "method"), nullable=False
    )
    amount: Mapped[int] = money()

    invoice: Mapped[Invoice] = relationship(back_populates="payments")

    @validates("amount")
    def _validate_amount(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)


class ExpenseLine(BaseModel):
    """One expense of the event.

    ``is_auto`` marks lines generated by the system (venue rent default, staff
    pay from payroll, equipment rental from usages); users may still edit the
    amount.  ``assignment_id`` is only allowed on ``staff_pay`` lines and
    ``equipment_usage_id`` only on ``equipment_rental`` lines, one line per
    assignment / usage and invoice.
    """

    __tablename__ = "expense_lines"
    __table_args__ = (
        sa.UniqueConstraint("invoice_id", "assignment_id", name="uq_expense_lines_assignment"),
        sa.UniqueConstraint(
            "invoice_id", "equipment_usage_id", name="uq_expense_lines_usage"
        ),
        sa.CheckConstraint(
            "assignment_id IS NULL OR kind = 'staff_pay'", name="assignment_only_staff_pay"
        ),
        sa.CheckConstraint(
            "equipment_usage_id IS NULL OR kind = 'equipment_rental'",
            name="usage_only_equipment",
        ),
        *money_checks("amount"),
        sa.Index("ix_expense_lines_assignment", "assignment_id"),
        sa.Index("ix_expense_lines_usage", "equipment_usage_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE", index=False)
    kind: Mapped[ExpenseKind] = mapped_column(choice(ExpenseKind, "kind"), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    amount: Mapped[int] = money()
    is_auto: Mapped[bool] = mapped_column(nullable=False, default=False)
    assignment_id: Mapped[Optional[int]] = fk(
        "assignments.id", ondelete="SET NULL", nullable=True, index=False
    )
    equipment_usage_id: Mapped[Optional[int]] = fk(
        "equipment_usages.id", ondelete="SET NULL", nullable=True, index=False
    )

    invoice: Mapped[Invoice] = relationship(back_populates="expenses")
    assignment: Mapped[Optional["Assignment"]] = relationship()  # noqa: F821
    equipment_usage: Mapped[Optional["EquipmentUsage"]] = relationship()  # noqa: F821

    @validates("amount")
    def _validate_amount(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)


class PayrollLine(BaseModel, TimestampMixin):
    """What one assignment earned on this invoice (a frozen calculation).

    ``base``, ``commission`` and ``bonus`` come from the assignment's terms
    snapshot and the invoice ``units``; ``total = base + commission + bonus``
    is enforced by a CHECK.  ``applied_tier`` records the bonus tier that was
    reached (``{"threshold", "amount"}`` or NULL).  ``role`` and
    ``person_name`` are copied so the sheet stays readable if the roster entry
    is renamed.  ``paid_*`` track settlement.
    """

    __tablename__ = "payroll_lines"
    __table_args__ = (
        sa.UniqueConstraint("invoice_id", "assignment_id", name="uq_payroll_lines_assignment"),
        sa.CheckConstraint("units >= 0", name="units_nonneg"),
        sa.CheckConstraint("total = base + commission + bonus", name="total_is_sum"),
        sa.CheckConstraint(
            "paid_method IS NULL OR paid_at IS NOT NULL", name="paid_method_needs_date"
        ),
        *money_checks("base", "commission", "bonus", "total"),
        sa.Index("ix_payroll_lines_assignment", "assignment_id"),
        sa.Index("ix_payroll_lines_paid", "paid_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE", index=False)
    assignment_id: Mapped[int] = fk("assignments.id", ondelete="RESTRICT", index=False)
    role: Mapped[Optional[Skill]] = mapped_column(choice(Skill, "role"))
    person_name: Mapped[str] = mapped_column(sa.String(120), nullable=False, default="")
    units: Mapped[int] = mapped_column(nullable=False, default=0)
    base: Mapped[int] = money()
    commission: Mapped[int] = money()
    bonus: Mapped[int] = money()
    total: Mapped[int] = money()
    applied_tier: Mapped[Optional[dict]] = json_col(None, nullable=True)
    paid_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    paid_method: Mapped[Optional[PayoutMethod]] = mapped_column(
        choice(PayoutMethod, "paid_method")
    )
    paid_note: Mapped[Optional[str]] = mapped_column(sa.String(300))

    invoice: Mapped[Invoice] = relationship(back_populates="payroll_lines")
    assignment: Mapped["Assignment"] = relationship(  # noqa: F821
        back_populates="payroll_lines"
    )

    @validates("base", "commission", "bonus", "total")
    def _validate_money(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("units")
    def _validate_units(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=0)

    @validates("applied_tier")
    def _validate_tier(self, key: str, value: Any) -> Optional[dict]:
        if value is None:
            return None
        return normalize_bonus_tiers([value])[0]

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None

    def snapshot_person(self, assignment: "Assignment") -> None:  # noqa: F821
        """Copy role and display name from *assignment* (call when creating the line)."""
        self.assignment = assignment
        self.role = assignment.role
        self.person_name = assignment.person_name
