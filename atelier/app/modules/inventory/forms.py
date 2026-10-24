"""Reading and validating the plain HTML forms of the inventory module.

Everything goes through :class:`app.services.forms.Form`, so Persian / Arabic / Latin digits, Toman
amounts and Persian error messages behave as in every other module.  The functions return clean Python
values and record the Persian messages on the form (``form.ok`` / ``form.errors``); routes re-render
the page with status 422 when ``form.ok`` is false.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models import StockItem
from app.services.constants import (
    STANDARD_SIZES,
    EquipmentCondition,
    EquipmentKind,
    EquipmentOwnership,
    MovementReason,
    PaperSize,
    StockKind,
    format_size,
)
from app.services.consumption import SHEET_UNIT, Sales, canonical_size
from app.services.forms import Form
from app.services.money import MAX_TOMAN, normalize_digits, to_persian_digits

MAX_QTY = 1_000_000  # more than any atelier stocks; keeps every product far from integer limits
MAX_SALES_QTY = 100_000
MAX_EXTRA_ROWS = 30
PACK_SIZES = (20, 50)  # AGENTS.md: a pack holds 20 or 50 sheets depending on the brand
SIZED_KINDS = (StockKind.FRAME, StockKind.CHASSIS, StockKind.CELLOPHANE)
SALES_CATEGORIES = (("print", "چاپ"), ("frame", "قاب"), ("chassis", "شاسی"))
SIZE_HINT = "اندازه نامعتبر است؛ مثلاً ۱۶×۲۱ یا ۲۰*۳۰ وارد کنید."


# --------------------------------------------------------------------------
# sizes
# --------------------------------------------------------------------------


def size_choice_value(size: Optional[str]) -> tuple[str, str]:
    """``(select value, custom text)`` for showing a stored size in the item form."""
    if not size:
        return "", ""
    if size in STANDARD_SIZES:
        return size, ""
    return "custom", size


def read_size(form: Form, *, choice_name: str = "size_choice", custom_name: str = "size_custom") -> Optional[str]:
    """A canonical size from the select (``16x21`` / ``custom``) and the custom text field."""
    chosen = form.choice(choice_name, "اندازه", (*STANDARD_SIZES, "custom"), required=True)
    if chosen is None:
        return None
    if chosen != "custom":
        return str(chosen)
    custom = form.text(custom_name, "اندازهٔ دلخواه", required=True, max_len=20)
    if custom is None:
        return None
    size = canonical_size(custom)
    if size is None:
        form.error(custom_name, SIZE_HINT)
    return size


# --------------------------------------------------------------------------
# stock items
# --------------------------------------------------------------------------


@dataclass
class ItemInput:
    kind: StockKind
    name: str
    brand: Optional[str]
    paper_size: Optional[PaperSize]
    sheets_per_pack: Optional[int]
    size_label: Optional[str]
    unit: str
    unit_cost: int
    low_stock_threshold: int
    is_active: bool = True
    use_as_default: bool = False
    initial_quantity: int = 0  # create only: the stock the item starts with

    def columns(self) -> dict[str, Any]:
        """Keyword arguments for ``StockItem`` (everything but the atelier and the initial stock)."""
        return {
            "kind": self.kind,
            "name": self.name,
            "brand": self.brand,
            "paper_size": self.paper_size,
            "sheets_per_pack": self.sheets_per_pack,
            "size_label": self.size_label,
            "unit": self.unit,
            "unit_cost": self.unit_cost,
            "low_stock_threshold": self.low_stock_threshold,
            "is_active": self.is_active,
        }


def read_units(
    form: Form,
    *,
    counts_in_sheets: bool,
    sheets_per_pack: Optional[int],
    label: str,
    packs_name: str = "packs",
    sheets_name: str = "sheets",
    quantity_name: str = "quantity",
    allow_zero: bool = False,
    required: bool = True,
) -> Optional[int]:
    """A stock quantity in item units.

    Paper counted in sheets is typed as whole packs plus loose sheets; every other kind as one number.
    Returns ``None`` (with an error recorded) when invalid.
    """
    if counts_in_sheets and sheets_per_pack:
        packs = form.integer(packs_name, "تعداد بسته", min=0, max=MAX_QTY, default=0) or 0
        sheets = form.integer(sheets_name, "تعداد برگ", min=0, max=MAX_QTY, default=0) or 0
        if packs_name in form.errors or sheets_name in form.errors:
            return None
        total = packs * sheets_per_pack + sheets
        if total == 0 and required and not allow_zero:
            form.error(packs_name, f"{label} باید بیشتر از صفر باشد.")
            return None
        if total > MAX_QTY:
            form.error(packs_name, f"{label} بیش از حد بزرگ است.")
            return None
        return total
    quantity = form.integer(quantity_name, label, required=required, min=0, max=MAX_QTY, default=0)
    if quantity_name in form.errors:
        return None
    if quantity == 0 and required and not allow_zero:
        form.error(quantity_name, f"{label} باید بیشتر از صفر باشد.")
        return None
    return quantity or 0


def read_item(form: Form, *, existing: Optional[StockItem] = None, kind_locked: bool = False) -> Optional[ItemInput]:
    """The item form (create when *existing* is ``None``).

    *kind_locked*: the item already has movements, so kind, paper size and size are taken from *existing*
    (changing them would silently re-target past consumption).
    """
    if kind_locked and existing is not None:
        kind = existing.kind
        form.values["kind"] = kind.value
    else:
        kind = form.enum("kind", "نوع قلم", StockKind, required=True)
    name = form.text("name", "نام قلم", required=True, max_len=150)
    brand = form.text("brand", "برند", max_len=100)
    unit_cost = form.money("unit_cost", "قیمت", default=0, max=MAX_TOMAN) or 0
    is_active = form.boolean("is_active") if existing is not None else True
    use_as_default = form.boolean("use_as_default")
    if kind is None:
        return None

    paper_size: Optional[PaperSize] = None
    spp: Optional[int] = None
    size_label: Optional[str] = None
    unit = form.text("unit", "واحد", max_len=30) or "عدد"
    if kind == StockKind.PAPER:
        unit = SHEET_UNIT
        if kind_locked and existing is not None and existing.paper_size is not None:
            paper_size = existing.paper_size
        else:
            paper_size = form.enum("paper_size", "اندازهٔ کاغذ", PaperSize, required=True)
        spp = form.integer("sheets_per_pack", "تعداد برگ در هر بسته", required=True, min=1, max=1000)
    elif kind in SIZED_KINDS:
        if kind_locked and existing is not None and existing.size_label:
            size_label = existing.size_label
        else:
            size_label = read_size(form)
    threshold = form.integer("low_stock_threshold", "آستانهٔ هشدار کمبود", min=0, max=MAX_QTY, default=0) or 0
    if kind == StockKind.PAPER and spp:
        threshold *= spp  # typed in packs, stored in sheets like the stock itself

    initial = 0
    if existing is None:
        initial = (
            read_units(
                form,
                counts_in_sheets=kind == StockKind.PAPER,
                sheets_per_pack=spp,
                label="موجودی اولیه",
                packs_name="initial_packs",
                sheets_name="initial_sheets",
                quantity_name="initial_quantity",
                allow_zero=True,
                required=False,
            )
            or 0
        )
    if not form.ok:
        return None
    return ItemInput(
        kind=kind,
        name=name,
        brand=brand,
        paper_size=paper_size,
        sheets_per_pack=spp,
        size_label=size_label,
        unit=unit,
        unit_cost=unit_cost,
        low_stock_threshold=threshold,
        is_active=is_active,
        use_as_default=use_as_default,
        initial_quantity=initial,
    )


def item_form_values(item: StockItem) -> dict[str, Any]:
    """The values of an existing item as the item form posts them (for the edit page)."""
    choice, custom = size_choice_value(item.size_label)
    spp = item.sheets_per_pack or 0
    threshold = item.low_stock_threshold or 0
    if item.counts_in_sheets and spp:
        threshold = -(-threshold // spp)  # packs, rounded up
    return {
        "kind": item.kind.value,
        "name": item.name,
        "brand": item.brand or "",
        "paper_size": item.paper_size.value if item.paper_size else "",
        "sheets_per_pack": item.sheets_per_pack or "",
        "size_choice": choice,
        "size_custom": custom,
        "unit": item.unit,
        "unit_cost": item.unit_cost,
        "low_stock_threshold": threshold,
        "is_active": item.is_active,
    }


# --------------------------------------------------------------------------
# movements
# --------------------------------------------------------------------------

#: The manual movement types of the item page: label, ledger reason and what the form asks for.
MOVEMENT_TYPES: dict[str, dict[str, Any]] = {
    "purchase": {
        "label": "ورود به انبار",
        "title": "ثبت ورود به انبار (خرید)",
        "reason": MovementReason.PURCHASE,
        "icon": "download",
        "help": "تعداد خریداری‌شده را وارد کنید؛ به موجودی اضافه می‌شود.",
    },
    "consume": {
        "label": "مصرف",
        "title": "ثبت مصرف",
        "reason": MovementReason.CONSUMPTION,
        "icon": "minus",
        "help": "مصرف دستی (مثلاً جوهر یک مراسم)؛ از موجودی کم می‌شود.",
    },
    "waste": {
        "label": "ضایعات",
        "title": "ثبت ضایعات",
        "reason": MovementReason.WASTE,
        "icon": "trash",
        "help": "مواد خراب‌شده یا ازبین‌رفته؛ از موجودی کم می‌شود.",
    },
    "return": {
        "label": "مرجوعی",
        "title": "ثبت مرجوعی",
        "reason": MovementReason.RETURN,
        "icon": "refresh",
        "help": "برگشت به انبار (مثلاً قاب برگشتی مشتری) یا برگشت به فروشنده.",
    },
    "adjust": {
        "label": "شمارش موجودی",
        "title": "شمارش و اصلاح موجودی",
        "reason": MovementReason.ADJUSTMENT,
        "icon": "scale",
        "help": "تعداد واقعی شمارش‌شده را وارد کنید؛ اختلاف با موجودی دفتری به‌عنوان اصلاح ثبت می‌شود.",
    },
}


@dataclass
class MovementInput:
    type: str
    reason: MovementReason
    delta: Optional[int] = None  # signed change (every type but "adjust")
    counted: Optional[int] = None  # "adjust": the counted quantity
    note: Optional[str] = None
    unit_cost: Optional[int] = None
    update_cost: bool = False
    event_id: Optional[int] = None


def read_movement(form: Form, item: StockItem, move_type: str) -> Optional[MovementInput]:
    """Validate one manual movement of *item*; the stock itself is checked by the service."""
    spec = MOVEMENT_TYPES[move_type]
    result = MovementInput(type=move_type, reason=spec["reason"])
    result.note = form.text("note", "توضیحات", max_len=300, multiline=False)

    if move_type == "adjust":
        counted = read_units(
            form,
            counts_in_sheets=item.counts_in_sheets,
            sheets_per_pack=item.sheets_per_pack,
            label="تعداد شمارش‌شده",
            allow_zero=True,
            required=True,
        )
        if not result.note and "note" not in form.errors:
            form.error("note", "دلیل اصلاح را بنویسید (مثلاً «شمارش پایان ماه»).")
        result.counted = counted
    else:
        qty = read_units(
            form,
            counts_in_sheets=item.counts_in_sheets,
            sheets_per_pack=item.sheets_per_pack,
            label="مقدار",
        )
        if qty is not None:
            if move_type == "purchase":
                result.delta = qty
            elif move_type in ("consume", "waste"):
                result.delta = -qty
            else:  # return
                direction = form.choice("direction", "جهت مرجوعی", ("in", "out"), required=True, default="in")
                result.delta = qty if direction == "in" else -qty
        if move_type == "purchase":
            cost = form.money("unit_cost", "قیمت خرید", max=MAX_TOMAN)
            result.unit_cost = cost
            result.update_cost = form.boolean("update_cost") and cost is not None
        if move_type == "consume":
            result.event_id = form.integer("event_id", "مراسم", min=1)
    return result if form.ok else None


# --------------------------------------------------------------------------
# equipment
# --------------------------------------------------------------------------


@dataclass
class EquipmentInput:
    name: str
    kind: EquipmentKind
    ownership: EquipmentOwnership
    rent_cost_per_event: int
    supplier: Optional[str]
    serial: Optional[str]
    condition: EquipmentCondition
    is_active: bool = True


def read_equipment(form: Form, *, editing: bool = False) -> Optional[EquipmentInput]:
    name = form.text("name", "نام تجهیز", required=True, max_len=150)
    kind = form.enum("kind", "نوع تجهیز", EquipmentKind, required=True)
    ownership = form.enum("ownership", "مالکیت", EquipmentOwnership, required=True, default=EquipmentOwnership.OWNED)
    rent = form.money("rent_cost_per_event", "اجارهٔ هر مراسم", max=MAX_TOMAN)
    if ownership == EquipmentOwnership.RENTED and rent is None and "rent_cost_per_event" not in form.errors:
        form.error("rent_cost_per_event", "برای تجهیز اجاره‌ای، اجارهٔ هر مراسم را وارد کنید.")
    supplier = form.text("supplier", "اجاره‌دهنده", max_len=150)
    serial = form.text("serial", "شمارهٔ سریال", max_len=100, digits=False)
    condition = form.enum("condition", "وضعیت", EquipmentCondition, default=EquipmentCondition.GOOD)
    is_active = form.boolean("is_active") if editing else True
    if not form.ok or kind is None or ownership is None:
        return None
    return EquipmentInput(
        name=name or "",
        kind=kind,
        ownership=ownership,
        rent_cost_per_event=(rent or 0) if ownership == EquipmentOwnership.RENTED else 0,
        supplier=supplier,
        serial=serial,
        condition=condition or EquipmentCondition.GOOD,
        is_active=is_active,
    )


# --------------------------------------------------------------------------
# expected sales (calculator and "prepare for event")
# --------------------------------------------------------------------------


@dataclass
class SalesInput:
    """Expected sales read from a query string / form, plus what the page needs to redisplay it."""

    sales: Sales = field(default_factory=Sales)
    extras: list[dict[str, str]] = field(default_factory=list)  # rows the user added with "+ ابعاد دیگر"
    present: bool = False  # the request carried any sales field at all


def sales_name(category: str, size: str) -> str:
    return f"{category}_{size}"


def _raw_list(form: Form, name: str) -> list[str]:
    getter = getattr(form.data, "getlist", None)
    values = getter(name) if getter else (form.data.get(name) or [])
    if isinstance(values, str):
        values = [values]
    return [str(value).strip() for value in values]


def read_sales(form: Form, *, categories: tuple[str, ...] = ("print", "frame", "chassis")) -> SalesInput:
    """Expected quantities: a field per standard size of each category plus "other size" rows."""
    result = SalesInput()
    targets = {"print": result.sales.prints, "frame": result.sales.frames, "chassis": result.sales.chassis}
    labels = dict(SALES_CATEGORIES)
    for category in categories:
        for size in STANDARD_SIZES:
            name = sales_name(category, size)
            if name in form.data:
                result.present = True
            qty = form.integer(name, f"{labels[category]} {format_size(size)}", min=0, max=MAX_SALES_QTY, default=0)
            if qty:
                targets[category][size] = qty
    cats = _raw_list(form, "extra_category")
    sizes = _raw_list(form, "extra_size")
    qtys = _raw_list(form, "extra_qty")
    count = max(len(cats), len(sizes), len(qtys))
    if count:
        result.present = True
    if count > MAX_EXTRA_ROWS:
        form.error("extra_qty", f"حداکثر {to_persian_digits(MAX_EXTRA_ROWS)} ردیف «ابعاد دیگر» مجاز است.")
        count = MAX_EXTRA_ROWS
    for index, (cat, size_text, qty_text) in enumerate(
        itertools.islice(itertools.zip_longest(cats, sizes, qtys, fillvalue=""), count)
    ):
        if not size_text and not qty_text:
            continue
        key = f"extra_{index}"
        row = {"category": cat or categories[0], "size": size_text, "qty": qty_text}
        result.extras.append(row)
        category = cat if cat in categories else None
        size = canonical_size(size_text)
        digits_only = normalize_digits(qty_text).replace(",", "")
        if category is None:
            form.error(key, "نوع ردیف نامعتبر است.")
            continue
        if size is None:
            form.error(key, SIZE_HINT)
            continue
        if not digits_only.isdigit() or int(digits_only) > MAX_SALES_QTY:
            form.error(key, "تعداد باید یک عدد صحیح (حداکثر ۱۰۰٬۰۰۰) باشد.")
            continue
        qty = int(digits_only)
        if qty:
            targets[category][size] = targets[category].get(size, 0) + qty
    return result


def sales_form_values(sales: Sales) -> dict[str, Any]:
    """Field values for a sales form pre-filled from *sales* (standard sizes; others become extra rows)."""
    values: dict[str, Any] = {}
    for category, counts in (("print", sales.prints), ("frame", sales.frames), ("chassis", sales.chassis)):
        for size in STANDARD_SIZES:
            values[sales_name(category, size)] = counts.get(size, 0)
    return values


def extra_rows(sales: Sales) -> list[dict[str, str]]:
    """The sales entries whose size is not a standard one, as "other size" rows."""
    rows: list[dict[str, str]] = []
    for category, counts in (("print", sales.prints), ("frame", sales.frames), ("chassis", sales.chassis)):
        for size, qty in counts.items():
            if size not in STANDARD_SIZES:
                rows.append({"category": category, "size": size, "qty": str(qty)})
    return rows


# --------------------------------------------------------------------------
# preferences and the yield table
# --------------------------------------------------------------------------


def read_prefs(form: Form) -> Optional[dict[str, Any]]:
    paper = form.enum("preferred_paper", "کاغذ ترجیحی", PaperSize, required=True)
    history = form.integer("history_invoices", "تعداد فاکتورهای میانگین", required=True, min=1, max=50)
    margin = form.integer("margin_percent", "حاشیهٔ اطمینان", required=True, min=0, max=100)
    auto = form.boolean("auto_consume")
    if not form.ok:
        return None
    return {
        "preferred_paper": paper.value,
        "history_invoices": history,
        "margin_percent": margin,
        "auto_consume": auto,
    }


MAX_YIELD = 64  # prints on one sheet; far above any real layout, rejects typos like 400


def yield_field(paper: str, size: str) -> str:
    return f"yield_{paper}_{size}"


def read_yield(form: Form, papers: list[str], sizes: list[str]) -> Optional[dict[str, dict[str, int]]]:
    """The yield grid: a number of prints per sheet for each (paper, size); ``0`` / blank = does not fit.

    *sizes* are the rows shown; the "other size" row posts ``new_size`` plus ``yield_<paper>_new``.
    """
    table: dict[str, dict[str, int]] = {paper: {} for paper in papers}
    for size in sizes:
        for paper in papers:
            label = f"بازدهی {format_size(size)} روی {paper}"
            value = form.integer(yield_field(paper, size), label, min=0, max=MAX_YIELD, default=0) or 0
            if value:
                table[paper][size] = value
    new_text = form.raw("new_size")
    if new_text:
        new_size = canonical_size(new_text)
        form.values["new_size"] = new_text
        if new_size is None:
            form.error("new_size", SIZE_HINT)
        else:
            for paper in papers:
                value = form.integer(yield_field(paper, "new"), f"بازدهی اندازهٔ جدید روی {paper}", min=0, max=MAX_YIELD, default=0) or 0
                if value:
                    table[paper][new_size] = value
    if not form.ok:
        return None
    if not any(table[paper] for paper in papers):
        form.error("new_size", "دست‌کم یک اندازه باید روی یک کاغذ جا شود.")
        return None
    return table
