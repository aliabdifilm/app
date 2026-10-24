"""Stock service: the movement ledger, low-stock alerts and automatic consumption.

This is the one place that changes ``StockItem.quantity`` for the inventory module and for the
event bus.  Everything here uses ``db.session`` only, calls ``add`` + ``flush`` and **never commits**
(the route, or the publisher of the event, owns the transaction).

Contents
========

* **Movements** - :func:`record_movement` appends a ledger row (who / when / why), refuses a negative
  stock unless the caller says it is deliberate (``allow_negative=True``: automatic consumption and
  explicit stocktake adjustments) and raises a *low-stock* notification when an item crosses its
  threshold (only on the crossing, never on every movement).
* **Requirements** - :func:`build_requirements` turns expected sales (prints / frames / chassis per
  size) into stock needs: paper sheets through :mod:`app.services.print_yield` (a size that does not
  fit the preferred paper moves to the first paper it fits), frames and chassis one for one, cellophane
  one per delivered print.  The calculator and the "prepare for event" view use it directly.
* **Automatic consumption** - :func:`sync_invoice` brings the ledger of one invoice in line with what
  the invoice theoretically consumed.  It is **idempotent**: it compares the target with the net
  consumption already recorded for that invoice and writes only the difference, so re-saving an edited
  invoice adjusts by the difference and saving it twice changes nothing.  The manager can override a
  row (:func:`set_overrides`); an overridden row keeps the manager's number until it is reset.
  :func:`on_invoice_saved` / :func:`on_event_status_changed` are the event-bus handlers (registered by
  ``app.modules.inventory``); they never raise, so a failure cannot break saving an invoice.

Conventions
===========

* Paper is counted in **sheets** (``StockItem.unit == "برگ"``) and shown also as packs; the ``unit_cost``
  of such paper is the price of one **pack**.  Every other kind is counted in ``unit`` with ``unit_cost``
  per unit.  Money is integer Toman.
* A *need key* names a thing the stock must provide: ``paper:A4``, ``frame:16x21``, ``chassis:30x40``,
  ``cellophane:20x30``.  :func:`item_key` gives the key a stock item serves.
* Preferences of one atelier live in the ``settings`` table (``inventory.prefs.<atelier id>``) and the
  manager's per-invoice overrides in ``inventory.override.<invoice id>``: no schema of their own.
"""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models import Atelier, Invoice, Setting, StockItem, StockMovement
from app.models.inventory import SHEET_UNIT
from app.services import audit, jalali, notifications, print_yield
from app.services.constants import (
    DEFAULT_PRINT_YIELD,
    InvoiceStatus,
    MovementReason,
    NotificationKind,
    PaperSize,
    ProductCategory,
    StockKind,
)
from app.services.money import to_persian_digits

logger = logging.getLogger(__name__)

__all__ = [
    "SHEET_UNIT",
    "StockError",
    "InsufficientStock",
    "DEFAULT_PREFS",
    "Need",
    "Requirements",
    "Sales",
    "Row",
    "State",
    "SyncResult",
    "canonical_size",
    "item_key",
    "format_quantity",
    "stock_value",
    "get_prefs",
    "save_prefs",
    "yield_table_for",
    "record_movement",
    "stocktake",
    "lines_to_sales",
    "apply_margin",
    "build_requirements",
    "candidates_for",
    "choose_item",
    "load_state",
    "load_states",
    "sync_invoice",
    "set_overrides",
    "reset_overrides",
    "on_invoice_saved",
    "on_event_status_changed",
]

PREFS_KEY = "inventory.prefs.{atelier_id}"
OVERRIDE_KEY = "inventory.override.{invoice_id}"

DEFAULT_PREFS: dict[str, Any] = {
    "preferred_paper": PaperSize.A4.value,  # paper tried first for every print size
    "auto_consume": True,  # deduct stock when an invoice is saved
    "history_invoices": 10,  # how many past invoices the "prepare for event" averages use
    "margin_percent": 10,  # safety margin the "prepare for event" plan adds
    "default_items": {},  # {need key: stock item id} used first when several items could serve a need
}

_KIND_ORDER = {
    StockKind.PAPER: 0,
    StockKind.FRAME: 1,
    StockKind.CHASSIS: 2,
    StockKind.CELLOPHANE: 3,
    StockKind.INK: 4,
    StockKind.OTHER: 5,
}
_SIZED_KINDS = (StockKind.FRAME, StockKind.CHASSIS, StockKind.CELLOPHANE)
_MAX_NOTE = 300


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class StockError(ValueError):
    """A stock rule was broken; the message is Persian and safe to show."""


class InsufficientStock(StockError):
    """A decrease would take the stock below zero."""

    def __init__(self, item: StockItem, wanted: int) -> None:
        self.item = item
        self.wanted = wanted
        super().__init__(
            f"موجودی «{item.name}» کافی نیست؛ موجودی فعلی {format_quantity(item)} است "
            f"و {format_quantity(item, wanted)} خواسته شده."
        )


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------


def canonical_size(value: Any) -> Optional[str]:
    """``'۱۶ × ۲۱'`` / ``'21x16'`` -> ``'16x21'``; ``None`` for anything that is not a size."""
    if value is None or not str(value).strip():
        return None
    try:
        return print_yield.normalize_size(str(value))
    except ValueError:
        return None


def item_key(item: StockItem) -> Optional[str]:
    """The need key an item serves (``paper:A4``, ``frame:16x21`` ...), ``None`` for ink and others."""
    if item.kind == StockKind.PAPER and item.paper_size is not None:
        return f"paper:{item.paper_size.value}"
    if item.kind in _SIZED_KINDS:
        size = canonical_size(item.size_label)
        if size:
            return f"{item.kind.value}:{size}"
    return None


def _text(value: Any) -> str:
    return to_persian_digits(str(value))


def format_quantity(item: StockItem, quantity: Optional[int] = None) -> str:
    """Human text of a quantity: paper as packs and sheets (``۶ بسته و ۱۰ برگ``), others with their unit."""
    qty = int(item.quantity or 0) if quantity is None else int(quantity)
    sign = "−" if qty < 0 else ""
    amount = abs(qty)
    if item.counts_in_sheets:
        packs, loose = divmod(amount, int(item.sheets_per_pack))
        parts = []
        if packs:
            parts.append(f"{_text(packs)} بسته")
        if loose or not packs:
            parts.append(f"{_text(loose)} برگ")
        return sign + " و ".join(parts)
    return f"{sign}{_text(amount)} {item.unit or 'عدد'}"


def stock_value(item: StockItem, quantity: Optional[int] = None) -> int:
    """Value in Toman of *quantity* (default: the stock) at the item's ``unit_cost``; never negative.

    Paper counted in sheets is priced per pack, so its value is ``sheets * pack price / sheets per pack``
    rounded half up (one integer division, no float).
    """
    qty = max(int(item.quantity or 0) if quantity is None else int(quantity), 0)
    cost = int(item.unit_cost or 0)
    if item.counts_in_sheets:
        per_pack = int(item.sheets_per_pack)
        return (qty * cost * 2 + per_pack) // (2 * per_pack)
    return qty * cost


def _note(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text[:_MAX_NOTE] or None


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


# --------------------------------------------------------------------------
# preferences (settings table)
# --------------------------------------------------------------------------


def _bounded(value: Any, low: int, high: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def clean_prefs(raw: Any) -> dict[str, Any]:
    """*raw* merged over :data:`DEFAULT_PREFS`; anything invalid falls back to the default."""
    prefs = copy.deepcopy(DEFAULT_PREFS)
    if not isinstance(raw, Mapping):
        return prefs
    paper = str(raw.get("preferred_paper") or "").strip().upper()
    if paper in PaperSize.values():
        prefs["preferred_paper"] = paper
    if isinstance(raw.get("auto_consume"), bool):
        prefs["auto_consume"] = raw["auto_consume"]
    prefs["history_invoices"] = _bounded(raw.get("history_invoices"), 1, 50, prefs["history_invoices"])
    prefs["margin_percent"] = _bounded(raw.get("margin_percent"), 0, 100, prefs["margin_percent"])
    defaults = raw.get("default_items")
    if isinstance(defaults, Mapping):
        prefs["default_items"] = {
            str(key): int(value)
            for key, value in defaults.items()
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    return prefs


def get_prefs(atelier_id: int) -> dict[str, Any]:
    """The inventory preferences of an atelier (defaults for the keys it never set)."""
    row = db.session.execute(
        sa.select(Setting.value).where(Setting.key == PREFS_KEY.format(atelier_id=atelier_id))
    ).scalar_one_or_none()
    return clean_prefs(row)


def save_prefs(atelier_id: int, prefs: Mapping[str, Any], actor: Any = None) -> dict[str, Any]:
    """Validate and store *prefs*; returns what was stored (the caller commits)."""
    clean = clean_prefs(prefs)
    Setting.set(PREFS_KEY.format(atelier_id=atelier_id), clean, actor=audit.actor_id(actor))
    return clean


def _valid_table(table: Any) -> bool:
    if not isinstance(table, Mapping) or not table:
        return False
    try:
        print_yield.plan_for_sales({}, "A4", 20, table)
    except (ValueError, TypeError):
        return False
    return True


def yield_table_for(atelier: Atelier) -> dict[str, dict[str, int]]:
    """Prints per sheet in use: the atelier's own table, else the site setting, else the A4 default."""
    own = getattr(atelier, "print_yield", None)
    if own and _valid_table(own):
        return copy.deepcopy(dict(own))
    site = Setting.get("print_yield")
    if site and _valid_table(site):
        return copy.deepcopy(dict(site))
    return copy.deepcopy(DEFAULT_PRINT_YIELD)


# --------------------------------------------------------------------------
# movements
# --------------------------------------------------------------------------


def _notify_if_low(item: StockItem, was_low: bool) -> None:
    """Tell the atelier manager when *item* has just crossed into "low" (not on every movement)."""
    if was_low or not item.is_low or not item.is_active:
        return
    atelier = item.atelier
    manager_id = getattr(atelier, "manager_id", None)
    if manager_id is None:
        return
    empty = (item.quantity or 0) <= 0
    title = f"موجودی «{item.name}» {'تمام شد' if empty else 'کم است'}"
    body = f"موجودی فعلی: {format_quantity(item)}"
    if item.low_stock_threshold:
        body += f" (آستانهٔ هشدار: {format_quantity(item, item.low_stock_threshold)})"
    url: Optional[str] = None
    try:
        from flask import url_for

        url = url_for("inventory.item_detail", item_id=item.id)
    except Exception:  # no request / module disabled: the notification still works without a link
        url = None
    notifications.notify(manager_id, NotificationKind.LOW_STOCK, title, body, url, entity=item)


def record_movement(
    item: StockItem,
    delta: int,
    reason: MovementReason | str,
    *,
    actor: Any = None,
    event: Any = None,
    invoice: Any = None,
    is_auto: bool = False,
    note: Optional[str] = None,
    unit_cost: Optional[int] = None,
    allow_negative: bool = False,
    notify: bool = True,
) -> StockMovement:
    """Append a ledger row and apply it to ``item.quantity``.

    *delta* is signed and non-zero.  A decrease that would leave the stock below zero raises
    :class:`InsufficientStock` unless *allow_negative* is true (automatic consumption never blocks an
    invoice; an explicit stocktake adjustment is the manager's deliberate number).  The row is flushed
    here, so a concurrent change of the same item surfaces as ``StaleDataError`` now and the caller can
    retry.
    """
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise StockError("مقدار باید یک عدد صحیح باشد.")
    if delta == 0:
        raise StockError("مقدار نمی‌تواند صفر باشد.")
    reason = MovementReason.coerce(reason)
    before = int(item.quantity or 0)
    if delta < 0 and before + delta < 0 and not allow_negative:
        raise InsufficientStock(item, -delta)
    was_low = item.is_low
    movement = item.record_movement(
        delta,
        reason,
        actor=audit.actor_id(actor),
        event=event,
        invoice=invoice,
        is_auto=is_auto,
        note=_note(note),
        unit_cost=unit_cost,
    )
    db.session.flush()
    if notify:
        _notify_if_low(item, was_low)
    return movement


def stocktake(
    item: StockItem,
    counted: int,
    *,
    actor: Any = None,
    note: Optional[str] = None,
    allow_negative: bool = False,
) -> Optional[StockMovement]:
    """Set the stock to the *counted* quantity with an ``adjustment`` movement (``None`` if unchanged).

    A negative count is refused unless *allow_negative* is passed explicitly.
    """
    if isinstance(counted, bool) or not isinstance(counted, int):
        raise StockError("تعداد شمارش‌شده باید یک عدد صحیح باشد.")
    if counted < 0 and not allow_negative:
        raise StockError("تعداد شمارش‌شده نمی‌تواند منفی باشد.")
    delta = counted - int(item.quantity or 0)
    if delta == 0:
        return None
    return record_movement(
        item, delta, MovementReason.ADJUSTMENT, actor=actor, note=note, allow_negative=True
    )


# --------------------------------------------------------------------------
# requirements: expected sales -> what the stock must provide
# --------------------------------------------------------------------------


@dataclass
class Sales:
    """Quantities sold (or expected) per size, canonical sizes as keys."""

    prints: dict[str, int] = field(default_factory=dict)
    frames: dict[str, int] = field(default_factory=dict)
    chassis: dict[str, int] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.prints or self.frames or self.chassis)


@dataclass(frozen=True)
class Need:
    """Something the stock has to provide: *qty* in the item unit (sheets for paper)."""

    key: str
    kind: StockKind
    label: str
    qty: int
    detail: str = ""


@dataclass
class Requirements:
    needs: list[Need]
    plan: Optional[print_yield.PrintPlan]
    unsupported: list[str]

    def by_key(self) -> dict[str, Need]:
        return {need.key: need for need in self.needs}


def _merge_counts(counts: Optional[Mapping[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for raw_size, raw_qty in (counts or {}).items():
        if isinstance(raw_qty, bool) or not isinstance(raw_qty, int) or raw_qty < 0:
            raise StockError("تعداد فروش باید عدد صحیحِ نامنفی باشد.")
        if raw_qty == 0:
            continue
        size = canonical_size(raw_size)
        if size is None:
            raise StockError(f"اندازهٔ «{raw_size}» نامعتبر است؛ نمونهٔ درست: ۱۶×۲۱")
        merged[size] = merged.get(size, 0) + raw_qty
    return merged


def apply_margin(counts: Mapping[str, int], percent: int) -> dict[str, int]:
    """Every count raised by *percent* percent, rounded up (integer arithmetic)."""
    percent = max(0, int(percent))
    return {size: _ceil_div(qty * (100 + percent), 100) for size, qty in counts.items() if qty > 0}


def lines_to_sales(lines: Iterable[Any]) -> Sales:
    """Sold prints / frames / chassis of invoice lines (rows of one size add up; files are ignored)."""
    sales = Sales()
    targets = {
        ProductCategory.PRINT: sales.prints,
        ProductCategory.FRAME: sales.frames,
        ProductCategory.CHASSIS: sales.chassis,
    }
    for line in lines:
        target = targets.get(line.category)
        size = canonical_size(line.size_label)
        qty = int(line.qty or 0)
        if target is None or size is None or qty <= 0:
            continue
        target[size] = target.get(size, 0) + qty
    return sales


def _paper_order(preferred: str, table: Mapping[str, Mapping[str, int]]) -> list[str]:
    order = [preferred]
    for name in [member.value for member in PaperSize] + sorted(table):
        if name not in order:
            order.append(name)
    return order


def build_requirements(
    sales: Sales,
    *,
    preferred_paper: str = "A4",
    table: Optional[Mapping[str, Mapping[str, int]]] = None,
    pack_sizes: Optional[Mapping[str, int] | int] = None,
    margin_percent: int = 0,
    cellophane: bool = True,
) -> Requirements:
    """Needs of *sales*: paper sheets, frames, chassis and one cellophane per print.

    Paper uses :func:`print_yield.plan_for_sales`: every size is printed on *preferred_paper* when it
    fits there, else on the first paper (A4, A3, ...) of the yield table that holds it; sizes that fit
    nowhere are reported in ``unsupported`` (their cellophane is still counted).  *margin_percent*
    raises every sold count first (planning an event); automatic consumption uses 0.
    """
    paper_name = print_yield.normalize_paper(preferred_paper)
    prepared = copy.deepcopy(dict(table)) if table is not None else copy.deepcopy(DEFAULT_PRINT_YIELD)
    prints = apply_margin(_merge_counts(sales.prints), margin_percent)
    frames = apply_margin(_merge_counts(sales.frames), margin_percent)
    chassis = apply_margin(_merge_counts(sales.chassis), margin_percent)

    needs: list[Need] = []
    unsupported: list[str] = []
    plan: Optional[print_yield.PrintPlan] = None
    if prints:
        order = _paper_order(paper_name, prepared)
        chosen: dict[str, str] = {}
        for size in prints:
            for candidate in order:
                if print_yield.sheet_yield(size, candidate, prepared) > 0:
                    chosen[size] = candidate
                    break
        plan = print_yield.plan_for_sales(
            prints,
            paper=paper_name,
            sheets_per_pack=pack_sizes if pack_sizes is not None else 20,
            table=prepared,
            paper_by_size=chosen,
        )
        unsupported = list(plan.unsupported)
        for name, totals in plan.by_paper.items():
            rows = [r for r in plan.rows if r.paper == name]
            detail = "؛ ".join(
                f"{row.prints} چاپ {row.size.replace('x', '×')} ← {row.sheets} برگ" for row in rows
            )
            needs.append(Need(f"paper:{name}", StockKind.PAPER, f"کاغذ {name}", totals.sheets, detail))
    for kind, label, counts in (
        (StockKind.FRAME, "قاب", frames),
        (StockKind.CHASSIS, "شاسی", chassis),
    ):
        for size in sorted(counts, key=_size_sort):
            needs.append(
                Need(f"{kind.value}:{size}", kind, f"{label} {size.replace('x', '×')}", counts[size])
            )
    if cellophane:
        for size in sorted(prints, key=_size_sort):
            needs.append(
                Need(
                    f"cellophane:{size}",
                    StockKind.CELLOPHANE,
                    f"سلفون {size.replace('x', '×')}",
                    prints[size],
                    "یک سلفون برای هر چاپ تحویلی",
                )
            )
    needs.sort(key=lambda need: (_KIND_ORDER[need.kind], _size_sort(need.key.partition(":")[2]), need.key))
    return Requirements(needs=needs, plan=plan, unsupported=unsupported)


def _size_sort(size: str) -> tuple:
    parts = size.split("x")
    try:
        return tuple(float(part) for part in parts)
    except ValueError:
        return (float("inf"), size)


# --------------------------------------------------------------------------
# matching needs to stock items
# --------------------------------------------------------------------------


def index_items(items: Iterable[StockItem]) -> dict[str, list[StockItem]]:
    """Stock items grouped by the need key they serve (items with no key are left out)."""
    index: dict[str, list[StockItem]] = defaultdict(list)
    for item in items:
        key = item_key(item)
        if key:
            index[key].append(item)
    return index


def candidates_for(key: str, items: Iterable[StockItem]) -> list[StockItem]:
    return index_items(items).get(key, [])


def choose_item(
    candidates: Sequence[StockItem],
    *,
    sticky: Optional[Mapping[int, int]] = None,
    preferred_id: Optional[int] = None,
) -> Optional[StockItem]:
    """The item that serves a need.

    1. an item that already carries consumption of this invoice (so a re-save never moves the stock
       from one brand to another; the one with the largest recorded amount wins),
    2. the item the manager marked as the default,
    3. the oldest active item that has stock, else the oldest active item.

    ``None`` when there is no active item and no sticky one.
    """
    sticky = sticky or {}
    held = [item for item in candidates if item.id in sticky]
    if held:
        return max(held, key=lambda item: (sticky[item.id], -item.id))
    active = [item for item in candidates if item.is_active]
    if preferred_id is not None:
        for item in active:
            if item.id == preferred_id:
                return item
    if not active:
        return None
    stocked = [item for item in active if (item.quantity or 0) > 0]
    return min(stocked or active, key=lambda item: item.id)


# --------------------------------------------------------------------------
# automatic consumption of one invoice
# --------------------------------------------------------------------------


@dataclass
class Row:
    """One stock item of an invoice: what it theoretically consumed, what the ledger says, the override."""

    item: StockItem
    theoretical: int = 0
    recorded: int = 0
    override: Optional[int] = None
    detail: str = ""

    @property
    def target(self) -> int:
        """What the ledger should say: the manager's number when set, else the theory."""
        return self.override if self.override is not None else self.theoretical

    @property
    def diff(self) -> int:
        return self.target - self.recorded

    @property
    def status(self) -> str:
        if self.override is not None:
            return "override" if self.diff == 0 else "differs"
        return "ok" if self.diff == 0 else "differs"


@dataclass
class State:
    invoice: Invoice
    rows: list[Row] = field(default_factory=list)
    unmatched: list[Need] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    auto_enabled: bool = True

    @property
    def in_sync(self) -> bool:
        return all(row.diff == 0 for row in self.rows) and not self.unmatched

    @property
    def has_override(self) -> bool:
        return any(row.override is not None for row in self.rows)

    @property
    def is_void(self) -> bool:
        return self.invoice.status == InvoiceStatus.VOID


@dataclass
class SyncResult:
    invoice_id: int
    skipped: Optional[str] = None  # "disabled" when automatic consumption is switched off
    movements: list[StockMovement] = field(default_factory=list)
    state: Optional[State] = None

    @property
    def changed(self) -> bool:
        return bool(self.movements)


def _invoice_label(invoice: Invoice) -> str:
    event = invoice.event
    hall = event.hall.name if event is not None and event.hall is not None else ""
    when = jalali.to_jalali_str(event.date, "numeric", persian_digits=True) if event is not None else ""
    return " · ".join(part for part in (hall, when) if part) or f"#{invoice.id}"


def _recorded_by_item(invoice_ids: Sequence[int]) -> dict[int, dict[int, int]]:
    """``{invoice id: {item id: net units consumed}}`` from the ledger (consumption rows of invoices)."""
    result: dict[int, dict[int, int]] = defaultdict(dict)
    if not invoice_ids:
        return result
    rows = db.session.execute(
        sa.select(StockMovement.invoice_id, StockMovement.item_id, sa.func.sum(StockMovement.delta))
        .where(
            StockMovement.invoice_id.in_(list(invoice_ids)),
            StockMovement.reason == MovementReason.CONSUMPTION,
        )
        .group_by(StockMovement.invoice_id, StockMovement.item_id)
    ).all()
    for invoice_id, item_id, total in rows:
        net = -int(total or 0)
        if net != 0:
            result[int(invoice_id)][int(item_id)] = net
    return result


def _overrides_by_invoice(invoice_ids: Sequence[int]) -> dict[int, dict[int, int]]:
    result: dict[int, dict[int, int]] = {}
    if not invoice_ids:
        return result
    keys = {OVERRIDE_KEY.format(invoice_id=i): i for i in invoice_ids}
    for key, value in db.session.execute(sa.select(Setting.key, Setting.value).where(Setting.key.in_(list(keys)))).all():
        if isinstance(value, Mapping):
            result[keys[key]] = {
                int(item_id): int(qty)
                for item_id, qty in value.items()
                if str(item_id).isdigit() and isinstance(qty, int) and not isinstance(qty, bool) and qty >= 0
            }
    return result


def compute_state(
    invoice: Invoice,
    *,
    items: Sequence[StockItem],
    recorded: Mapping[int, int],
    overrides: Mapping[int, int],
    prefs: Mapping[str, Any],
    table: Mapping[str, Mapping[str, int]],
) -> State:
    """Pure comparison of an invoice's theoretical consumption with the ledger (writes nothing)."""
    state = State(invoice=invoice, auto_enabled=bool(prefs.get("auto_consume", True)))
    by_id = {item.id: item for item in items}
    index = index_items(items)
    sticky = {item_id: amount for item_id, amount in recorded.items() if item_id in by_id}
    for item_id in overrides:
        sticky.setdefault(item_id, 0)

    needs: list[Need] = []
    if invoice.status != InvoiceStatus.VOID:
        preferred = prefs.get("preferred_paper", "A4")
        pinned = [
            (amount, by_id[item_id].paper_size.value)
            for item_id, amount in sticky.items()
            if item_id in by_id and by_id[item_id].kind == StockKind.PAPER and by_id[item_id].paper_size is not None
        ]
        if pinned:  # keep an invoice on the paper it was first consumed from
            preferred = max(pinned)[1]
        requirements = build_requirements(
            lines_to_sales(invoice.lines), preferred_paper=preferred, table=table, margin_percent=0
        )
        needs = requirements.needs
        state.unsupported = requirements.unsupported

    rows: dict[int, Row] = {}
    defaults = prefs.get("default_items") or {}
    for need in needs:
        item = choose_item(index.get(need.key, []), sticky=sticky, preferred_id=defaults.get(need.key))
        if item is None:
            state.unmatched.append(need)
            continue
        row = rows.setdefault(item.id, Row(item=item))
        row.theoretical += need.qty
        row.detail = need.detail if not row.detail else f"{row.detail}؛ {need.detail}"
    for item_id in set(recorded) | set(overrides):
        item = by_id.get(item_id)
        if item is not None:
            rows.setdefault(item_id, Row(item=item))
    for item_id, row in rows.items():
        row.recorded = int(recorded.get(item_id, 0))
        row.override = overrides.get(item_id)
    state.rows = sorted(rows.values(), key=lambda r: (_KIND_ORDER[r.item.kind], r.item.name, r.item.id))
    return state


def _atelier_items(atelier_id: int) -> list[StockItem]:
    return list(
        db.session.execute(
            sa.select(StockItem).where(StockItem.atelier_id == atelier_id).order_by(StockItem.id)
        ).scalars()
    )


def load_states(invoices: Sequence[Invoice]) -> dict[int, State]:
    """:func:`compute_state` for many invoices of one or more ateliers with a handful of queries.

    Load ``Invoice.lines`` (``selectinload``) and ``Invoice.event`` before calling to avoid per-row queries.
    """
    ids = [invoice.id for invoice in invoices]
    recorded = _recorded_by_item(ids)
    overrides = _overrides_by_invoice(ids)
    context: dict[int, tuple[list[StockItem], dict, dict]] = {}
    states: dict[int, State] = {}
    for invoice in invoices:
        if invoice.atelier_id not in context:
            atelier = db.session.get(Atelier, invoice.atelier_id)
            context[invoice.atelier_id] = (
                _atelier_items(invoice.atelier_id),
                get_prefs(invoice.atelier_id),
                yield_table_for(atelier),
            )
        items, prefs, table = context[invoice.atelier_id]
        states[invoice.id] = compute_state(
            invoice,
            items=items,
            recorded=recorded.get(invoice.id, {}),
            overrides=overrides.get(invoice.id, {}),
            prefs=prefs,
            table=table,
        )
    return states


def load_state(invoice: Invoice) -> State:
    return load_states([invoice])[invoice.id]


def _auto_note(row: Row, invoice: Invoice) -> str:
    label = _invoice_label(invoice)
    if invoice.status == InvoiceStatus.VOID:
        return f"برگشت خودکار: فاکتور {label} باطل شد"
    if row.recorded == 0:
        return f"کسر خودکار بر اساس فاکتور {label}"
    if row.diff > 0:
        return f"افزایش خودکار پس از ویرایش فاکتور {label}"
    return f"کاهش خودکار پس از ویرایش فاکتور {label}"


def sync_invoice(invoice: Invoice, actor: Any = None, *, state: Optional[State] = None) -> SyncResult:
    """Write the movements that bring the ledger of *invoice* to its target (idempotent).

    Only the difference between the target and the net consumption already recorded is written, so
    saving twice writes nothing and an edit writes the delta.  A void invoice has no target, so its
    consumption is handed back.  Does nothing (``skipped="disabled"``) while automatic consumption
    is switched off.  The caller owns the transaction and should wrap the call in a savepoint.
    """
    state = state or load_state(invoice)
    result = SyncResult(invoice_id=invoice.id, state=state)
    if not state.auto_enabled:
        result.skipped = "disabled"
        return result
    event = invoice.event
    for row in state.rows:
        diff = row.diff
        if diff == 0:
            continue
        movement = record_movement(
            row.item,
            -diff,
            MovementReason.CONSUMPTION,
            actor=actor,
            event=event,
            invoice=invoice,
            is_auto=True,
            note=_auto_note(row, invoice),
            allow_negative=True,
        )
        result.movements.append(movement)
        row.recorded += diff
    if result.movements:
        audit.log(
            actor,
            "stock.auto_consumed",
            invoice,
            {
                "event_id": invoice.event_id,
                "items": {str(row.item.id): row.recorded for row in state.rows},
                "movements": len(result.movements),
            },
        )
    return result


def set_overrides(
    invoice: Invoice, quantities: Mapping[int, int], actor: Any = None, *, note: Optional[str] = None
) -> list[StockMovement]:
    """The manager's own numbers for rows of *invoice*: ``{item id: units actually consumed}``.

    Each number replaces the theory for that item until :func:`reset_overrides`; the difference to the
    ledger is written at once as a manual (not automatic) consumption movement.  A number equal to the
    theory just removes the override.  Items that are not a row of this invoice are refused.
    """
    state = load_state(invoice)
    rows = {row.item.id: row for row in state.rows}
    clean: dict[int, int] = {}
    for item_id, qty in quantities.items():
        if item_id not in rows:
            raise StockError("این قلم در مصرف این فاکتور نیست.")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            raise StockError("مقدار مصرف باید عدد صحیحِ نامنفی باشد.")
        clean[int(item_id)] = qty

    stored = dict(_overrides_by_invoice([invoice.id]).get(invoice.id, {}))
    movements: list[StockMovement] = []
    changes: dict[str, Any] = {}
    for item_id, qty in clean.items():
        row = rows[item_id]
        if qty == row.theoretical:
            stored.pop(item_id, None)
        else:
            stored[item_id] = qty
        if qty != row.recorded:
            movements.append(
                record_movement(
                    row.item,
                    row.recorded - qty,
                    MovementReason.CONSUMPTION,
                    actor=actor,
                    event=invoice.event,
                    invoice=invoice,
                    is_auto=False,
                    note=note or f"اصلاح دستی مصرف فاکتور {_invoice_label(invoice)}",
                    allow_negative=True,
                )
            )
            changes[str(item_id)] = [row.recorded, qty]
            row.recorded = qty
    _store_overrides(invoice.id, stored, actor)
    if changes:
        audit.log(actor, "stock.consumption_overridden", invoice, {"changes": changes})
    return movements


def _store_overrides(invoice_id: int, values: Mapping[int, int], actor: Any = None) -> None:
    key = OVERRIDE_KEY.format(invoice_id=invoice_id)
    if values:
        Setting.set(key, {str(item_id): qty for item_id, qty in values.items()}, actor=audit.actor_id(actor))
        return
    row = db.session.execute(sa.select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is not None:
        db.session.delete(row)
        db.session.flush()


def reset_overrides(invoice: Invoice, actor: Any = None) -> SyncResult:
    """Forget the manager's numbers and go back to the automatic calculation (the ledger follows)."""
    had = bool(_overrides_by_invoice([invoice.id]).get(invoice.id))
    _store_overrides(invoice.id, {}, actor)
    result = sync_invoice(invoice, actor)
    if had:
        audit.log(actor, "stock.consumption_reset", invoice, {"movements": len(result.movements)})
    return result


# --------------------------------------------------------------------------
# event-bus handlers (registered by app.modules.inventory)
# --------------------------------------------------------------------------

_RETRIES = 3


def _sync_by_id(invoice_id: int, actor_id: Optional[int]) -> None:
    """Run :func:`sync_invoice` in a savepoint; never raises (a stock problem must not break an invoice)."""
    for attempt in range(1, _RETRIES + 1):
        try:
            with db.session.begin_nested():
                invoice = db.session.get(Invoice, invoice_id)
                if invoice is None:
                    return
                sync_invoice(invoice, actor_id)
            return
        except StaleDataError:  # another request moved the same item at the same moment: recompute
            logger.info("stock changed while syncing invoice %s (attempt %s)", invoice_id, attempt)
        except Exception:  # noqa: BLE001 - isolation is the whole point
            logger.exception("automatic consumption of invoice %s failed", invoice_id)
            return
    logger.warning("automatic consumption of invoice %s gave up after %s attempts", invoice_id, _RETRIES)


def on_invoice_saved(invoice_id: int, actor_id: Optional[int] = None, **_: Any) -> None:
    """``invoice.saved``: deduct (or adjust) the stock the invoice consumed."""
    _sync_by_id(invoice_id, actor_id)


def on_event_status_changed(
    event_id: int, old_status: Any = None, new_status: Any = None, actor_id: Optional[int] = None, **_: Any
) -> None:
    """``event.status_changed``: a voided invoice releases its event (``invoiced -> done``); hand its stock back."""
    if str(getattr(old_status, "value", old_status)) != "invoiced":
        return
    try:
        invoice_id = db.session.execute(
            sa.select(Invoice.id).where(Invoice.event_id == event_id, Invoice.status == InvoiceStatus.VOID)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception("could not look up the invoice of event %s", event_id)
        return
    if invoice_id is not None:
        _sync_by_id(invoice_id, actor_id)
