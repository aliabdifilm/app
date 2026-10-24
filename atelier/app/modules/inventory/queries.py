"""Query builders and small aggregations of the inventory panel (every one is scoped by atelier id)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Equipment, EquipmentUsage, Event, Invoice, InvoiceLine, StockItem, StockMovement
from app.services.constants import (
    EquipmentCondition,
    EquipmentKind,
    EquipmentOwnership,
    EventStatus,
    InvoiceStatus,
    MovementReason,
    ProductCategory,
    StockKind,
)
from app.services.consumption import Sales, canonical_size, stock_value

#: Kinds in the order a manager thinks of them (paper first); used for sorting and for the filter tabs.
KIND_ORDER = (
    StockKind.PAPER,
    StockKind.INK,
    StockKind.FRAME,
    StockKind.CHASSIS,
    StockKind.CELLOPHANE,
    StockKind.OTHER,
)

UPCOMING_EXCLUDED = (EventStatus.CANCELLED, EventStatus.INVOICED)

#: Expected sales used when an atelier has no invoice yet (a rough wedding evening; the manager edits them).
DEFAULT_EXPECTED = Sales(
    prints={"16x21": 40, "20x30": 25, "30x40": 10},
    frames={"16x21": 10, "20x30": 6, "30x40": 2},
    chassis={"16x21": 4, "20x30": 4, "30x40": 2},
)


# --------------------------------------------------------------------------
# stock items
# --------------------------------------------------------------------------


def _kind_rank():
    return sa.case(*[(StockItem.kind == kind, rank) for rank, kind in enumerate(KIND_ORDER)], else_=len(KIND_ORDER))


def items_stmt(atelier_id: int, *, kind: str = "", q: str = "", state: str = "") -> sa.Select:
    """Stock items of an atelier, filtered by kind, text (name / brand) and state (low / active / inactive)."""
    stmt = sa.select(StockItem).where(StockItem.atelier_id == atelier_id)
    if StockKind.is_valid(kind):
        stmt = stmt.where(StockItem.kind == StockKind.coerce(kind))
    text = (q or "").strip().lower()
    if text:
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(StockItem.name).contains(text, autoescape=True),
                sa.func.lower(sa.func.coalesce(StockItem.brand, "")).contains(text, autoescape=True),
            )
        )
    if state == "low":
        stmt = stmt.where(StockItem.is_active.is_(True), StockItem.quantity <= StockItem.low_stock_threshold)
    elif state == "inactive":
        stmt = stmt.where(StockItem.is_active.is_(False))
    elif state == "active":
        stmt = stmt.where(StockItem.is_active.is_(True))
    return stmt.order_by(_kind_rank(), StockItem.name, StockItem.id)


def low_stock_stmt(atelier_id: int) -> sa.Select:
    return (
        sa.select(StockItem)
        .where(
            StockItem.atelier_id == atelier_id,
            StockItem.is_active.is_(True),
            StockItem.quantity <= StockItem.low_stock_threshold,
        )
        .order_by(StockItem.quantity, StockItem.name, StockItem.id)
    )


def low_stock_count(atelier_id: int) -> int:
    return int(
        db.session.scalar(
            sa.select(sa.func.count(StockItem.id)).where(
                StockItem.atelier_id == atelier_id,
                StockItem.is_active.is_(True),
                StockItem.quantity <= StockItem.low_stock_threshold,
            )
        )
        or 0
    )


def all_items(atelier_id: int, *, active_only: bool = False) -> list[StockItem]:
    stmt = sa.select(StockItem).where(StockItem.atelier_id == atelier_id).order_by(StockItem.id)
    if active_only:
        stmt = stmt.where(StockItem.is_active.is_(True))
    return list(db.session.execute(stmt).scalars())


def stock_totals(atelier_id: int) -> dict[str, int]:
    """Counts and the value of the stock (active items; value of stock below zero counts as 0)."""
    items = all_items(atelier_id, active_only=True)
    return {
        "items": len(items),
        "value": sum(stock_value(item) for item in items),
        "low": sum(1 for item in items if item.is_low),
        "empty": sum(1 for item in items if (item.quantity or 0) <= 0),
    }


def has_movements(item_id: int) -> bool:
    return (
        db.session.scalar(sa.select(sa.func.count(StockMovement.id)).where(StockMovement.item_id == item_id)) or 0
    ) > 0


# --------------------------------------------------------------------------
# movements
# --------------------------------------------------------------------------


def movements_stmt(
    atelier_id: int,
    *,
    item_id: Optional[int] = None,
    reason: str = "",
    auto: str = "",
) -> sa.Select:
    """The ledger of an atelier (newest first) with the item, the actor and the event loaded."""
    stmt = (
        sa.select(StockMovement)
        .join(StockItem, StockItem.id == StockMovement.item_id)
        .where(StockItem.atelier_id == atelier_id)
        .options(
            joinedload(StockMovement.item),
            joinedload(StockMovement.actor),
            joinedload(StockMovement.event).joinedload(Event.hall),
        )
        .order_by(StockMovement.id.desc())
    )
    if item_id is not None:
        stmt = stmt.where(StockMovement.item_id == item_id)
    if MovementReason.is_valid(reason):
        stmt = stmt.where(StockMovement.reason == MovementReason.coerce(reason))
    if auto == "1":
        stmt = stmt.where(StockMovement.is_auto.is_(True))
    elif auto == "0":
        stmt = stmt.where(StockMovement.is_auto.is_(False))
    return stmt


def recent_movements(atelier_id: int, limit: int = 8) -> list[StockMovement]:
    return list(db.session.execute(movements_stmt(atelier_id).limit(limit)).scalars().unique())


# --------------------------------------------------------------------------
# events and equipment
# --------------------------------------------------------------------------


def upcoming_events_stmt(atelier_id: int, today: dt.date, *, days: Optional[int] = None) -> sa.Select:
    """Events of the atelier from *today* on (not cancelled / invoiced), soonest first."""
    stmt = (
        sa.select(Event)
        .where(
            Event.atelier_id == atelier_id,
            Event.date >= today,
            Event.status.not_in(UPCOMING_EXCLUDED),
        )
        .options(joinedload(Event.hall))
        .order_by(Event.date, Event.start_time, Event.id)
    )
    if days is not None:
        stmt = stmt.where(Event.date <= today + dt.timedelta(days=days))
    return stmt


def usages_by_event(event_ids: list[int]) -> dict[int, list[EquipmentUsage]]:
    """Equipment usages of the given events, grouped by event (one query)."""
    grouped: dict[int, list[EquipmentUsage]] = {event_id: [] for event_id in event_ids}
    if not event_ids:
        return grouped
    rows = db.session.execute(
        sa.select(EquipmentUsage)
        .where(EquipmentUsage.event_id.in_(event_ids))
        .options(joinedload(EquipmentUsage.equipment))
        .order_by(EquipmentUsage.id)
    ).scalars()
    for usage in rows:
        grouped[usage.event_id].append(usage)
    return grouped


def equipment_stmt(atelier_id: int, *, kind: str = "", ownership: str = "", state: str = "", q: str = "") -> sa.Select:
    stmt = sa.select(Equipment).where(Equipment.atelier_id == atelier_id)
    if EquipmentKind.is_valid(kind):
        stmt = stmt.where(Equipment.kind == EquipmentKind.coerce(kind))
    if EquipmentOwnership.is_valid(ownership):
        stmt = stmt.where(Equipment.ownership == EquipmentOwnership.coerce(ownership))
    if state == "inactive":
        stmt = stmt.where(Equipment.is_active.is_(False))
    elif state == "attention":
        stmt = stmt.where(
            Equipment.is_active.is_(True),
            Equipment.condition.in_([EquipmentCondition.POOR, EquipmentCondition.IN_REPAIR]),
        )
    elif state != "all":
        stmt = stmt.where(Equipment.is_active.is_(True))
    text = (q or "").strip().lower()
    if text:
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(Equipment.name).contains(text, autoescape=True),
                sa.func.lower(sa.func.coalesce(Equipment.serial, "")).contains(text, autoescape=True),
            )
        )
    return stmt.order_by(Equipment.kind, Equipment.name, Equipment.id)


def equipment_conflicts(event: Event, equipment_ids: list[int]) -> dict[int, list[Event]]:
    """Other events of the same day that already use one of *equipment_ids*: ``{equipment id: [events]}``."""
    result: dict[int, list[Event]] = {}
    if not equipment_ids:
        return result
    rows = db.session.execute(
        sa.select(EquipmentUsage.equipment_id, Event)
        .join(Event, Event.id == EquipmentUsage.event_id)
        .where(
            EquipmentUsage.equipment_id.in_(equipment_ids),
            Event.date == event.date,
            Event.id != event.id,
            Event.status != EventStatus.CANCELLED,
        )
        .options(joinedload(Event.hall))
    ).unique()
    for equipment_id, other in rows:
        result.setdefault(equipment_id, []).append(other)
    return result


def pending_pickups(atelier_id: int, today: dt.date, days: int = 14) -> list[EquipmentUsage]:
    """Rented gear of upcoming events that nobody has collected yet (soonest event first)."""
    return list(
        db.session.execute(
            sa.select(EquipmentUsage)
            .join(Event, Event.id == EquipmentUsage.event_id)
            .join(Equipment, Equipment.id == EquipmentUsage.equipment_id)
            .where(
                Event.atelier_id == atelier_id,
                Event.date >= today,
                Event.date <= today + dt.timedelta(days=days),
                Event.status.not_in(UPCOMING_EXCLUDED),
                Equipment.ownership == EquipmentOwnership.RENTED,
                EquipmentUsage.checked_out_at.is_(None),
            )
            .options(joinedload(EquipmentUsage.equipment), joinedload(EquipmentUsage.event).joinedload(Event.hall))
            .order_by(Event.date, EquipmentUsage.id)
        )
        .scalars()
        .unique()
    )


def equipment_needing_attention(atelier_id: int, limit: int = 6) -> list[Equipment]:
    return list(
        db.session.execute(
            sa.select(Equipment)
            .where(
                Equipment.atelier_id == atelier_id,
                Equipment.is_active.is_(True),
                Equipment.condition.in_([EquipmentCondition.POOR, EquipmentCondition.IN_REPAIR]),
            )
            .order_by(Equipment.name)
            .limit(limit)
        ).scalars()
    )


# --------------------------------------------------------------------------
# history: what this atelier usually sells
# --------------------------------------------------------------------------


@dataclass
class History:
    """Averages over the atelier's last invoices; ``count == 0`` means "no history" (defaults apply)."""

    count: int = 0
    sales: Sales = field(default_factory=Sales)
    totals: Sales = field(default_factory=Sales)


def historical_sales(atelier_id: int, count: int) -> History:
    """Average prints / frames / chassis per invoice over the last *count* submitted or settled invoices."""
    ids = list(
        db.session.execute(
            sa.select(Invoice.id)
            .join(Event, Event.id == Invoice.event_id)
            .where(
                Invoice.atelier_id == atelier_id,
                Invoice.status.in_([InvoiceStatus.SUBMITTED, InvoiceStatus.SETTLED]),
            )
            .order_by(Event.date.desc(), Invoice.id.desc())
            .limit(max(1, count))
        ).scalars()
    )
    history = History(count=len(ids))
    if not ids:
        return history
    rows = db.session.execute(
        sa.select(InvoiceLine.category, InvoiceLine.size_label, sa.func.sum(InvoiceLine.qty))
        .where(
            InvoiceLine.invoice_id.in_(ids),
            InvoiceLine.category.in_([ProductCategory.PRINT, ProductCategory.FRAME, ProductCategory.CHASSIS]),
        )
        .group_by(InvoiceLine.category, InvoiceLine.size_label)
    ).all()
    targets = {
        ProductCategory.PRINT: (history.totals.prints, history.sales.prints),
        ProductCategory.FRAME: (history.totals.frames, history.sales.frames),
        ProductCategory.CHASSIS: (history.totals.chassis, history.sales.chassis),
    }
    n = len(ids)
    for category, size_label, total in rows:
        size = canonical_size(size_label)
        if size is None or not total:
            continue
        totals, averages = targets[category]
        totals[size] = totals.get(size, 0) + int(total)
    for totals, averages in targets.values():
        for size, total in totals.items():
            average = (total * 2 + n) // (2 * n)  # rounded half up, integers only
            if average:
                averages[size] = average
    return history


# --------------------------------------------------------------------------
# invoices (automatic consumption review)
# --------------------------------------------------------------------------


def invoices_stmt(atelier_id: int) -> sa.Select:
    return (
        sa.select(Invoice)
        .join(Event, Event.id == Invoice.event_id)
        .where(Invoice.atelier_id == atelier_id)
        .options(
            selectinload(Invoice.lines),
            joinedload(Invoice.event).joinedload(Event.hall),
        )
        .order_by(Event.date.desc(), Invoice.id.desc())
    )
