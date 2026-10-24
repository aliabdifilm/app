"""Inventory: consumables with a movement ledger, and event equipment."""

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
    coerce_int,
    fk,
    money,
    money_checks,
    utcnow,
)
from app.models.event import Event
from app.services.constants import (
    EquipmentCondition,
    EquipmentKind,
    EquipmentOwnership,
    MovementReason,
    PaperSize,
    StockKind,
)


SHEET_UNIT = "برگ"
"""``StockItem.unit`` of paper that is counted in single sheets (packs are derived from it)."""


class StockItem(BaseModel, TimestampMixin):
    """A consumable the atelier keeps in stock (paper, ink, frames, ...).

    ``quantity`` is expressed in ``unit`` (e.g. sheets of paper, pieces of
    frame).  For paper, ``sheets_per_pack`` is 20 or 50 depending on the brand
    and ``paper_size`` is A4 or A3; the inventory module counts paper in sheets
    (``unit`` = :data:`SHEET_UNIT`), and ``unit_cost`` of such paper is the price
    of one *pack*.  Quantity may go negative (oversold) so a
    late data entry never blocks the invoice; ``is_low`` flags it.

    Change quantity only through :meth:`record_movement` so the ledger
    (:class:`StockMovement`) always explains the number.  ``version`` guards
    against two requests overwriting each other's updates
    (``StaleDataError`` on conflict).
    """

    __tablename__ = "stock_items"
    __table_args__ = (
        sa.CheckConstraint("sheets_per_pack IS NULL OR sheets_per_pack > 0", name="pack_positive"),
        sa.CheckConstraint("low_stock_threshold >= 0", name="threshold_nonneg"),
        sa.Index("ix_stock_items_atelier_kind", "atelier_id", "kind"),
        *money_checks("unit_cost"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE", index=False)
    kind: Mapped[StockKind] = mapped_column(choice(StockKind, "kind"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(sa.String(100))
    size_label: Mapped[Optional[str]] = mapped_column(sa.String(20))
    paper_size: Mapped[Optional[PaperSize]] = mapped_column(choice(PaperSize, "paper_size"))
    sheets_per_pack: Mapped[Optional[int]] = mapped_column()
    unit: Mapped[str] = mapped_column(sa.String(30), nullable=False, default="عدد")
    quantity: Mapped[int] = mapped_column(nullable=False, default=0)
    unit_cost: Mapped[int] = money()
    low_stock_threshold: Mapped[int] = mapped_column(nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}

    atelier: Mapped["Atelier"] = relationship()  # noqa: F821
    movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="item", order_by="StockMovement.id"
    )

    @validates("unit_cost")
    def _validate_cost(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("sheets_per_pack", "low_stock_threshold")
    def _validate_positive(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=1 if key == "sheets_per_pack" else 0)

    @property
    def is_low(self) -> bool:
        return (self.quantity or 0) <= (self.low_stock_threshold or 0)

    @property
    def counts_in_sheets(self) -> bool:
        """True for paper whose ``quantity`` is a number of *sheets* (``unit == "برگ"``).

        The inventory module keeps paper in sheets so a half-used pack is exact; ``packs_available``
        shows the whole packs.  Older rows may still count whole packs (``unit`` is something else).
        """
        return self.kind == StockKind.PAPER and bool(self.sheets_per_pack) and self.unit == SHEET_UNIT

    @property
    def sheets_available(self) -> Optional[int]:
        """Loose sheets in stock for paper (None for other kinds).

        ``quantity`` itself when the item is counted in sheets, else ``quantity * sheets_per_pack``
        (rows that count whole packs).
        """
        if self.kind != StockKind.PAPER or not self.sheets_per_pack:
            return None
        if self.counts_in_sheets:
            return self.quantity or 0
        return (self.quantity or 0) * self.sheets_per_pack

    @property
    def packs_available(self) -> Optional[int]:
        """Whole packs in stock for paper (None for other kinds); a negative stock gives 0."""
        sheets = self.sheets_available
        if sheets is None:
            return None
        return max(sheets, 0) // self.sheets_per_pack

    def record_movement(
        self,
        delta: int,
        reason: MovementReason | str,
        *,
        actor: Any = None,
        event: Optional[Event] = None,
        invoice: Any = None,
        is_auto: bool = False,
        note: Optional[str] = None,
        unit_cost: Optional[int] = None,
    ) -> "StockMovement":
        """Append a ledger row and apply it to ``quantity``.

        *delta* is signed (purchase +, consumption -).  The movement is added
        to the session by cascade through ``item.movements``; the caller owns
        the transaction.  *actor* may be a User or id.
        """
        delta = coerce_int(delta, "delta")
        if delta == 0:
            raise ValueError("stock movement delta must not be zero")
        movement = StockMovement(
            delta=delta,
            reason=MovementReason.coerce(reason),
            event=event,
            invoice=invoice,
            is_auto=is_auto,
            note=note,
            unit_cost=unit_cost,
            actor_id=getattr(actor, "id", actor),
        )
        self.movements.append(movement)
        self.quantity = (self.quantity or 0) + delta
        return movement


class StockMovement(BaseModel):
    """Append-only ledger row: why a stock quantity changed."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        sa.CheckConstraint("delta <> 0", name="delta_nonzero"),
        sa.Index("ix_stock_movements_item_created", "item_id", "created_at"),
        sa.Index("ix_stock_movements_reason", "reason"),
        *money_checks("unit_cost"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = fk("stock_items.id", ondelete="RESTRICT", index=False)
    delta: Mapped[int] = mapped_column(nullable=False)
    reason: Mapped[MovementReason] = mapped_column(
        choice(MovementReason, "reason"), nullable=False
    )
    event_id: Mapped[Optional[int]] = fk("events.id", ondelete="SET NULL", nullable=True)
    invoice_id: Mapped[Optional[int]] = fk("invoices.id", ondelete="SET NULL", nullable=True)
    is_auto: Mapped[bool] = mapped_column(nullable=False, default=False)
    note: Mapped[Optional[str]] = mapped_column(sa.String(300))
    unit_cost: Mapped[Optional[int]] = money(nullable=True, default=None)
    actor_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    item: Mapped[StockItem] = relationship(back_populates="movements")
    event: Mapped[Optional[Event]] = relationship()
    invoice: Mapped[Optional["Invoice"]] = relationship()  # noqa: F821
    actor: Mapped[Optional["User"]] = relationship()  # noqa: F821

    @validates("delta")
    def _validate_delta(self, key: str, value: Any) -> int:
        return coerce_int(value, key)

    @validates("unit_cost")
    def _validate_cost(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)


class Equipment(BaseModel, TimestampMixin):
    """A piece of event equipment (camera, flash, printer, laptop ...).

    Rented gear has a ``rent_cost_per_event`` charged to the atelier whenever
    it is used at an event; owned gear costs nothing per event.
    """

    __tablename__ = "equipment"
    __table_args__ = (
        sa.Index("ix_equipment_atelier_kind", "atelier_id", "kind"),
        *money_checks("rent_cost_per_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE", index=False)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    kind: Mapped[EquipmentKind] = mapped_column(choice(EquipmentKind, "kind"), nullable=False)
    ownership: Mapped[EquipmentOwnership] = mapped_column(
        choice(EquipmentOwnership, "ownership"),
        nullable=False,
        default=EquipmentOwnership.OWNED,
    )
    rent_cost_per_event: Mapped[int] = money()
    supplier: Mapped[Optional[str]] = mapped_column(sa.String(150))
    serial: Mapped[Optional[str]] = mapped_column(sa.String(100))
    condition: Mapped[EquipmentCondition] = mapped_column(
        choice(EquipmentCondition, "condition"),
        nullable=False,
        default=EquipmentCondition.GOOD,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    atelier: Mapped["Atelier"] = relationship()  # noqa: F821
    usages: Mapped[list["EquipmentUsage"]] = relationship(back_populates="equipment")

    @validates("rent_cost_per_event")
    def _validate_rent(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @property
    def event_cost(self) -> int:
        """Cost charged per event: the rent for rented gear, 0 for owned gear."""
        if self.ownership == EquipmentOwnership.RENTED:
            return self.rent_cost_per_event or 0
        return 0


class EquipmentUsage(BaseModel, TimestampMixin):
    """Equipment used at an event, with the rent *snapshotted* at that time.

    The ``checked_*`` / ``return_*`` columns are the pack-list trail kept by the inventory module:
    who took the gear for the event and when, and in what condition it came back.
    """

    __tablename__ = "equipment_usages"
    __table_args__ = (
        sa.UniqueConstraint("event_id", "equipment_id", name="uq_equipment_usages_pair"),
        sa.CheckConstraint("checked_in_at IS NULL OR checked_out_at IS NOT NULL", name="in_needs_out"),
        sa.Index("ix_equipment_usages_equipment", "equipment_id"),
        *money_checks("rent_cost_snapshot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = fk("events.id", ondelete="CASCADE", index=False)
    equipment_id: Mapped[int] = fk("equipment.id", ondelete="RESTRICT", index=False)
    rent_cost_snapshot: Mapped[int] = money()
    checked_out_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    checked_out_to: Mapped[Optional[str]] = mapped_column(sa.String(120))
    checked_in_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    return_condition: Mapped[Optional[EquipmentCondition]] = mapped_column(
        choice(EquipmentCondition, "return_condition")
    )
    return_note: Mapped[Optional[str]] = mapped_column(sa.String(300))

    event: Mapped[Event] = relationship(back_populates="equipment_usages")
    equipment: Mapped[Equipment] = relationship(back_populates="usages")

    @validates("rent_cost_snapshot")
    def _validate_snapshot(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @classmethod
    def snapshot(cls, event: Event, equipment: Equipment) -> "EquipmentUsage":
        """New usage whose cost is frozen from the equipment's current terms."""
        return cls(event=event, equipment=equipment, rent_cost_snapshot=equipment.event_cost)
