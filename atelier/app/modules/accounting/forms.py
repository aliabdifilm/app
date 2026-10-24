"""The invoice popup as a plain HTML form: field names, parsing, and the state the template renders.

Field names (all plain ``name=value`` pairs, so the form works without JavaScript and tests can post
dicts; money / stepper widgets submit raw integers, Persian or Arabic digits are accepted too)::

    qty__print__16x21  qty__raw_file  qty__frame__30x40 ...   sold count of a standard row
    nocomm__<row key>                                          checkbox: this row does not count for commission
    custom__<print|frame|chassis>__<n>__size|qty|weight|nocomm  a "+ other size" row (n = 0, 1, 2 ...)
    pay__card_reader  pay__card_to_card  pay__cash              customer payments (Toman)
    venue_rent                                                  venue rent; absent = the default, blank = 0
    equip__<usage id>__amount                                   suggested equipment rental (absent = the suggestion)
    extra__<n>__label|amount|kind                               free-form expense lines
    notes  base_revision  save_token                            memo, form revision, one-time submit token

:func:`parse` turns a posted mapping into a validated :class:`~app.services.ledger.InvoiceData`
(or a dict of ``{field name: Persian message}``); the same function serves the real save and the
live preview, so what the panel shows is what gets saved.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.models import Invoice
from app.services import ledger
from app.services.constants import (
    PRODUCT_CATALOG,
    ExpenseKind,
    PaymentMethod,
    ProductCategory,
    standard_invoice_rows,
)
from app.services.forms import Form
from app.services.money import to_persian_digits

__all__ = [
    "FixedRow",
    "SECTIONS",
    "GROUPS",
    "CUSTOM_CATEGORIES",
    "MAX_CUSTOM_ROWS",
    "MAX_EXTRA_ROWS",
    "Parsed",
    "fixed_rows",
    "new_nonce",
    "blank_state",
    "state_from_invoice",
    "invoice_to_data",
    "parse",
]

MAX_CUSTOM_ROWS = 15  # per category
MAX_EXTRA_ROWS = 30
CUSTOM_CATEGORIES: tuple[ProductCategory, ...] = tuple(
    category for category, meta in PRODUCT_CATALOG.items() if meta["allows_custom_sizes"]
)
_CUSTOM_RE = re.compile(r"^custom__(print|frame|chassis)__(\d{1,3})__(size|qty|weight|nocomm)$")
_EXTRA_RE = re.compile(r"^extra__(\d{1,3})__(label|amount|kind)$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class FixedRow:
    """One of the 11 fixed rows of the popup."""

    key: str  # "print__16x21", "raw_file"
    category: ProductCategory
    size: str  # "16x21" or ""
    label: str  # "چاپ 16×21" (pipe through |fa for Persian digits)

    @property
    def qty_name(self) -> str:
        return f"qty__{self.key}"

    @property
    def nocomm_name(self) -> str:
        return f"nocomm__{self.key}"


def fixed_rows() -> list[FixedRow]:
    rows = []
    for row in standard_invoice_rows():
        category = ProductCategory(row["category"])
        size = row["size_label"]
        rows.append(FixedRow(f"{category.value}__{size}" if size else category.value, category, size, row["label"]))
    return rows


_FIXED = fixed_rows()
_FIXED_BY_KEY = {row.key: row for row in _FIXED}

#: Sections of the popup in the AGENTS.md order: prints, raw file, edited file, frames, chassis.
SECTIONS: list[dict[str, Any]] = []
for _category, _meta in PRODUCT_CATALOG.items():
    SECTIONS.append(
        {
            "category": _category,
            "title": _meta["label"],
            "rows": [row for row in _FIXED if row.category == _category],
            "custom_label": _meta["custom_label"],
            "allows_custom": bool(_meta["allows_custom_sizes"]),
        }
    )

_BY_CATEGORY = {section["category"]: section for section in SECTIONS}
#: What the popup shows as cards: prints, files (raw + edited), frames, chassis.
GROUPS: list[dict[str, Any]] = [
    {"key": "print", "title": "چاپ", "sections": [_BY_CATEGORY[ProductCategory.PRINT]]},
    {
        "key": "files",
        "title": "فایل",
        "sections": [_BY_CATEGORY[ProductCategory.RAW_FILE], _BY_CATEGORY[ProductCategory.EDITED_FILE]],
    },
    {"key": "frame", "title": "قاب", "sections": [_BY_CATEGORY[ProductCategory.FRAME]]},
    {"key": "chassis", "title": "شاسی", "sections": [_BY_CATEGORY[ProductCategory.CHASSIS]]},
]


def new_nonce() -> str:
    """A fresh one-time token for a form render (double-submit protection)."""
    return secrets.token_urlsafe(12)


# --------------------------------------------------------------------------
# state: what the form template renders
# --------------------------------------------------------------------------


def _empty_custom() -> dict[str, list[dict[str, Any]]]:
    return {category.value: [] for category in CUSTOM_CATEGORIES}


def blank_state(suggestions: ledger.ExpenseSuggestions) -> dict[str, Any]:
    """State of a brand-new invoice: nothing sold, suggested rent and equipment."""
    return {
        "qty": {},
        "nocomm": {},
        "custom": _empty_custom(),
        "pay": {method.value: "" for method in PaymentMethod},
        "venue_rent": suggestions.venue_rent,
        "equipment": {item.usage_id: item.amount for item in suggestions.equipment},
        "extras": [],
        "notes": "",
        "base_revision": 0,
        "nonce": new_nonce(),
    }


def _line_key(line: Any) -> str:
    category = line.category.value
    return f"{category}__{line.size_label}" if line.size_label else category


def state_from_invoice(invoice: Invoice, suggestions: ledger.ExpenseSuggestions) -> dict[str, Any]:
    """State of an existing invoice (the saved values win over today's suggestions)."""
    state = blank_state(suggestions)
    qty: dict[str, int] = {}
    for line in invoice.lines:
        key = _line_key(line)
        if not line.is_custom_size and key in _FIXED_BY_KEY:
            qty[key] = qty.get(key, 0) + line.qty
            if not line.counts_for_commission:
                state["nocomm"][key] = True
        else:
            rows = state["custom"][line.category.value]
            rows.append(
                {
                    "idx": len(rows),
                    "size": line.size_label,
                    "qty": line.qty,
                    "weight": line.commission_weight,
                    "nocomm": not line.counts_for_commission,
                }
            )
    state["qty"] = qty
    state["pay"] = {p.method.value: p.amount for p in invoice.payments}
    for method in PaymentMethod:
        state["pay"].setdefault(method.value, 0)
    venue = [e for e in invoice.expenses if e.kind == ExpenseKind.VENUE_RENT]
    if venue:
        state["venue_rent"] = sum(e.amount for e in venue)
    saved_usage = {e.equipment_usage_id: e.amount for e in invoice.expenses if e.equipment_usage_id}
    state["equipment"] = {
        item.usage_id: saved_usage.get(item.usage_id, item.amount) for item in suggestions.equipment
    }
    for expense in invoice.expenses:
        if expense.kind in (ExpenseKind.MATERIAL, ExpenseKind.OTHER) or (
            expense.kind == ExpenseKind.EQUIPMENT_RENTAL and expense.equipment_usage_id is None
        ):
            state["extras"].append(
                {"idx": len(state["extras"]), "label": expense.label, "amount": expense.amount, "kind": expense.kind.value}
            )
    state["notes"] = invoice.notes or ""
    state["base_revision"] = invoice.revision
    return state


def invoice_to_data(invoice: Optional[Invoice], suggestions: ledger.ExpenseSuggestions) -> ledger.InvoiceData:
    """The stored invoice (or an empty one) as ledger input - what the first preview computes."""
    if invoice is None:
        return ledger.InvoiceData(lines=[], payments={}, venue_rent=None, equipment={})
    state = state_from_invoice(invoice, suggestions)
    return ledger.InvoiceData(
        lines=ledger.build_lines(
            {
                "category": l.category,
                "size": l.size_label,
                "qty": l.qty,
                "weight": l.commission_weight,
                "counts": l.counts_for_commission,
                "custom": l.is_custom_size,
            }
            for l in invoice.lines
        ),
        payments={p.method: p.amount for p in invoice.payments},
        venue_rent=state["venue_rent"],
        equipment=dict(state["equipment"]),
        extras=[
            ledger.ExpenseSpec(ExpenseKind(x["kind"]), x["label"], x["amount"]) for x in state["extras"]
        ],
        notes=invoice.notes,
    )


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@dataclass
class Parsed:
    """Outcome of :func:`parse`: validated data or Persian errors, plus the state to redisplay."""

    state: dict[str, Any]
    errors: dict[str, str] = field(default_factory=dict)
    data: Optional[ledger.InvoiceData] = None
    base_revision: Optional[int] = None
    nonce: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and self.data is not None


def _label_fa(text: str) -> str:
    return to_persian_digits(text)


def _custom_indexes(data: Mapping[str, Any]) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for name in data.keys():
        match = _CUSTOM_RE.match(str(name))
        if match:
            found.setdefault(match.group(1), set()).add(int(match.group(2)))
    return found


def _extra_indexes(data: Mapping[str, Any]) -> list[int]:
    found = set()
    for name in data.keys():
        match = _EXTRA_RE.match(str(name))
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def parse(data: Mapping[str, Any], suggestions: ledger.ExpenseSuggestions) -> Parsed:
    """Validate a posted invoice form (``request.form``) against the event's *suggestions*.

    Never raises: problems are collected as ``{field name: Persian message}`` so the template can show them
    next to the offending input, and ``state`` always holds what the user typed.
    """
    form = Form(data)
    errors = form.errors
    max_qty, max_weight, max_amount = ledger.MAX_QTY, ledger.MAX_WEIGHT, ledger.MAX_AMOUNT
    state = blank_state(suggestions)
    rows: list[dict[str, Any]] = []

    # -- fixed rows ------------------------------------------------------------
    state["qty"], state["nocomm"] = {}, {}
    for row in _FIXED:
        qty = form.integer(row.qty_name, f"تعداد {_label_fa(row.label)}", min=0, max=max_qty, default=0)
        state["qty"][row.key] = form.raw(row.qty_name)
        nocomm = form.boolean(row.nocomm_name)
        state["nocomm"][row.key] = nocomm
        if qty:
            rows.append({"category": row.category, "size": row.size, "qty": qty, "weight": 1, "counts": not nocomm})

    # -- "+ other size" rows -----------------------------------------------------
    state["custom"] = _empty_custom()
    indexes = _custom_indexes(data)
    for category in CUSTOM_CATEGORIES:
        meta = PRODUCT_CATALOG[category]
        wanted = sorted(indexes.get(category.value, ()))
        if len(wanted) > MAX_CUSTOM_ROWS:
            errors.setdefault(f"custom__{category.value}__{wanted[0]}__size", f"حداکثر {_label_fa(str(MAX_CUSTOM_ROWS))} ردیف «{meta['custom_label']}» مجاز است.")
            wanted = wanted[:MAX_CUSTOM_ROWS]
        for idx in wanted:
            prefix = f"custom__{category.value}__{idx}__"
            raw_size = form.raw(prefix + "size")
            qty = form.integer(prefix + "qty", f"تعداد {meta['custom_label']}", min=0, max=max_qty, default=0)
            weight = form.integer(prefix + "weight", "تعداد محاسبه برای حقوق نیرو", min=0, max=max_weight, default=1)
            nocomm = form.boolean(prefix + "nocomm")
            state["custom"][category.value].append(
                {
                    "idx": idx,
                    "size": raw_size,
                    "qty": form.raw(prefix + "qty"),
                    "weight": form.raw(prefix + "weight") or "1",
                    "nocomm": nocomm,
                }
            )
            if not raw_size and not qty:
                continue  # an untouched blank row
            built = {"category": category, "size": raw_size, "qty": qty or 0, "weight": 1 if weight is None else weight,
                     "counts": not nocomm, "custom": True}
            try:
                ledger.build_lines([built])
            except ledger.LedgerError as exc:
                errors.setdefault(prefix + "size", str(exc))
                continue
            rows.append(built)

    # -- payments ----------------------------------------------------------------
    payments: dict[str, int] = {}
    for method in PaymentMethod:
        name = f"pay__{method.value}"
        amount = form.money(name, f"جمع {method.label}", max=max_amount, default=0)
        state["pay"][method.value] = form.raw(name)
        payments[method.value] = amount or 0

    # -- expenses ----------------------------------------------------------------
    venue: Optional[int] = None
    if "venue_rent" in data:
        venue = form.money("venue_rent", "اجارهٔ باغ/تالار", max=max_amount, default=0) or 0
        state["venue_rent"] = form.raw("venue_rent")
    equipment: dict[int, int] = {}
    for item in suggestions.equipment:
        name = f"equip__{item.usage_id}__amount"
        if name in data:
            equipment[item.usage_id] = form.money(name, f"اجارهٔ {item.label}", max=max_amount, default=0) or 0
            state["equipment"][item.usage_id] = form.raw(name)
    extras: list[ledger.ExpenseSpec] = []
    state["extras"] = []
    extra_indexes = _extra_indexes(data)
    if len(extra_indexes) > MAX_EXTRA_ROWS:
        errors.setdefault(f"extra__{extra_indexes[0]}__label", f"حداکثر {_label_fa(str(MAX_EXTRA_ROWS))} هزینهٔ اضافی مجاز است.")
        extra_indexes = extra_indexes[:MAX_EXTRA_ROWS]
    kinds = [kind.value for kind in ledger.EXTRA_EXPENSE_KINDS]
    for idx in extra_indexes:
        prefix = f"extra__{idx}__"
        label = form.text(prefix + "label", "عنوان هزینه", max_len=200) or ""
        amount = form.money(prefix + "amount", "مبلغ هزینه", max=max_amount, default=0) or 0
        kind = form.choice(prefix + "kind", "نوع هزینه", kinds, default=ExpenseKind.OTHER.value)
        state["extras"].append(
            {"idx": idx, "label": form.raw(prefix + "label"), "amount": form.raw(prefix + "amount"), "kind": kind}
        )
        if not label and not amount:
            continue
        if not label:
            errors.setdefault(prefix + "label", "برای هزینهٔ اضافی عنوان بنویسید.")
            continue
        extras.append(ledger.ExpenseSpec(ExpenseKind(kind), label, amount))

    notes = form.text("notes", "یادداشت", max_len=1000, multiline=True)
    state["notes"] = form.raw("notes")

    base_revision = form.integer("base_revision", "نسخهٔ فاکتور", min=0, max=10**9, default=None)
    state["base_revision"] = base_revision if base_revision is not None else 0
    nonce = str(data.get("save_token") or "").strip()
    nonce = nonce if _NONCE_RE.match(nonce) else ""
    state["nonce"] = nonce or new_nonce()

    parsed = Parsed(state=state, errors=errors, base_revision=base_revision, nonce=nonce)
    if errors:
        return parsed
    try:
        invoice_data = ledger.InvoiceData(
            lines=ledger.build_lines(rows),
            payments=payments,
            venue_rent=venue,
            equipment=equipment,
            extras=extras,
            notes=notes,
        )
        ledger.normalize_payments(invoice_data.payments)
    except ledger.LedgerError as exc:
        errors["__all__"] = str(exc)
        return parsed
    parsed.data = invoice_data
    return parsed
