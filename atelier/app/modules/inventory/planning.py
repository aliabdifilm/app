"""Comparing what an event needs with what is in stock: shortfalls, what to buy, what to pack.

Pure functions over :class:`app.services.consumption.Requirements` and the atelier's stock items; no
request objects.  The numbers of sheets come from :mod:`app.services.print_yield` (through
``consumption.build_requirements``); nothing here re-implements a yield formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.models import StockItem
from app.services.constants import StockKind
from app.services.consumption import (
    Need,
    Requirements,
    choose_item,
    format_quantity,
    index_items,
    stock_value,
)
from app.services.money import format_toman, to_persian_digits

DEFAULT_PACK_SIZE = 20


def _fa(value: object) -> str:
    return to_persian_digits(str(value))


@dataclass
class PlanLine:
    """One need compared with the stock that can serve it."""

    need: Need
    items: list[StockItem] = field(default_factory=list)  # active items that can serve the need
    chosen: Optional[StockItem] = None  # the item the stock would be taken from
    available: int = 0  # units in stock over all those items (never below zero)
    pack_size: Optional[int] = None  # paper: sheets per pack used for the pack counts

    @property
    def shortfall(self) -> int:
        return max(self.need.qty - self.available, 0)

    @property
    def status(self) -> str:
        if not self.items:
            return "no_item"
        return "short" if self.shortfall else "ok"

    @property
    def is_paper(self) -> bool:
        return self.need.kind == StockKind.PAPER

    @property
    def packs_needed(self) -> Optional[int]:
        """Paper: whole packs that hold the sheets needed (``None`` for other kinds)."""
        if not self.is_paper:
            return None
        return -(-self.need.qty // (self.pack_size or DEFAULT_PACK_SIZE))

    @property
    def buy_units(self) -> int:
        """What to buy: whole packs for paper, units otherwise (0 when the stock suffices)."""
        if not self.shortfall:
            return 0
        if self.is_paper:
            return -(-self.shortfall // (self.pack_size or DEFAULT_PACK_SIZE))
        return self.shortfall

    @property
    def unit(self) -> str:
        if self.is_paper:
            return "بسته"
        return (self.chosen.unit if self.chosen is not None else None) or "عدد"

    @property
    def buy_text(self) -> str:
        if not self.buy_units:
            return ""
        if self.is_paper:
            return f"{_fa(self.buy_units)} بسته {_fa(self.pack_size or DEFAULT_PACK_SIZE)} برگی"
        return f"{_fa(self.buy_units)} {self.unit}"

    @property
    def need_text(self) -> str:
        """The need as people say it: sheets and packs for paper, units otherwise."""
        if self.is_paper:
            return f"{_fa(self.need.qty)} برگ ({_fa(self.packs_needed)} بسته {_fa(self.pack_size or DEFAULT_PACK_SIZE)} برگی)"
        return f"{_fa(self.need.qty)} {self.unit}"

    @property
    def available_text(self) -> str:
        if self.is_paper:
            packs = self.available // (self.pack_size or DEFAULT_PACK_SIZE)
            return f"{_fa(self.available)} برگ ({_fa(packs)} بسته کامل)"
        return f"{_fa(self.available)} {self.unit}"

    @property
    def bring_text(self) -> str:
        """What to put in the van: whole packs for paper (the last pack is usually opened), units otherwise."""
        if self.is_paper:
            return f"{_fa(self.packs_needed)} بسته {_fa(self.pack_size or DEFAULT_PACK_SIZE)} برگی"
        return f"{_fa(self.need.qty)} {self.unit}"

    @property
    def item_text(self) -> str:
        if self.chosen is None:
            return "قلمی در انبار تعریف نشده"
        brand = f" · {self.chosen.brand}" if self.chosen.brand else ""
        return f"{self.chosen.name}{brand}"


def build_plan(
    requirements: Requirements,
    items: Sequence[StockItem],
    *,
    defaults: Optional[dict[str, int]] = None,
    pack_size: Optional[int] = None,
) -> list[PlanLine]:
    """Compare every need of *requirements* with the active stock items that serve it.

    *pack_size* forces the sheets-per-pack used for paper (the calculator's pack-size field); without it
    the pack size of the chosen paper item is used (20 when no item exists).
    """
    index = index_items(item for item in items if item.is_active)
    lines: list[PlanLine] = []
    for need in requirements.needs:
        candidates = index.get(need.key, [])
        chosen = choose_item(candidates, preferred_id=(defaults or {}).get(need.key))
        line = PlanLine(need=need, items=candidates, chosen=chosen)
        line.available = sum(max(int(item.quantity or 0), 0) for item in candidates)
        if need.kind == StockKind.PAPER:
            line.pack_size = pack_size or (chosen.sheets_per_pack if chosen is not None else None) or DEFAULT_PACK_SIZE
        lines.append(line)
    return lines


def ink_lines(items: Sequence[StockItem]) -> list[StockItem]:
    """Active ink items (no formula links ink to prints, so the pack list just shows what is there)."""
    return [item for item in items if item.is_active and item.kind == StockKind.INK]


def shopping_lines(lines: Sequence[PlanLine], ink: Sequence[StockItem] = ()) -> list[str]:
    """Plain-text lines of what to buy (needs with a shortfall, then low ink)."""
    out: list[str] = []
    for line in lines:
        if line.status == "short":
            out.append(f"{line.need.label.replace('x', '×')}: {line.buy_text}")
        elif line.status == "no_item":
            out.append(f"{line.need.label.replace('x', '×')}: {_fa(line.need.qty)} {line.unit} (قلم انبار تعریف نشده)")
    for item in ink:
        if item.is_low:
            out.append(f"{item.name}: تهیه شود (موجودی {format_quantity(item)})")
    return out


def shopping_text(title: str, lines: Sequence[str]) -> str:
    """The shopping list as a message that can be pasted into a messenger."""
    if not lines:
        return f"{title}\nچیزی برای خرید لازم نیست."
    return "\n".join([title, *[f"• {line}" for line in lines]])


def estimated_cost(lines: Sequence[PlanLine]) -> int:
    """Rough cost of buying the shortfalls at the price of the chosen item (0 when unknown)."""
    total = 0
    for line in lines:
        if line.status != "short" or line.chosen is None:
            continue
        if line.is_paper:
            total += line.buy_units * int(line.chosen.unit_cost or 0)
        else:
            total += stock_value(line.chosen, line.buy_units)
    return total


def cost_text(amount: int) -> str:
    return f"{format_toman(amount, persian_digits=True)} تومان"
