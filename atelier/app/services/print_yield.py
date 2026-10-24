"""Print sheet / paper-pack planning (pure Python, integer math only).

AGENTS.md ("محاسبهٔ چاپ") and assumption A4: one sheet of photo paper yields a
fixed number of prints of a given size.  Defaults (:data:`DEFAULT_YIELD`):

========  ==========  ==========  ==========
paper     16x21       20x30       30x40
========  ==========  ==========  ==========
A4        2           1           (does not fit)
A3        4           2           1
========  ==========  ==========  ==========

Different sizes **never share a sheet**, so the sheets needed are the sum of
``ceil(prints / yield)`` per size, and packs are
``ceil(sheets / sheets_per_pack)`` (packs hold 20 or 50 sheets depending on the
brand).  The yield table is configurable: pass ``table=`` to every function; a
custom table **replaces** the default (it is not merged) and an entry of ``0``
marks a size that does not fit on that paper.

:func:`plan_for_sales` never raises for sizes it cannot print - it lists them,
with a Persian human-readable reason, in ``PrintPlan.unsupported`` so the UI can
warn the user.  Invalid *counts* (negative, non-integer) are programming/data
errors and raise ``ValueError`` everywhere.
"""
from __future__ import annotations

import copy
import numbers
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Union

from .money import normalize_digits

__all__ = [
    "DEFAULT_YIELD",
    "PrintRow",
    "PaperTotals",
    "PrintPlan",
    "default_yield_table",
    "normalize_size",
    "normalize_paper",
    "sheet_yield",
    "sheets_needed",
    "packs_needed",
    "plan_for_sales",
]

#: Default prints per sheet, ``{paper: {size: prints_per_sheet}}`` (assumption A4).
DEFAULT_YIELD: dict[str, dict[str, int]] = {
    "A4": {"16x21": 2, "20x30": 1},
    "A3": {"16x21": 4, "20x30": 2, "30x40": 1},
}

_MAX_SIDE_CM = Decimal(1000)
_INVISIBLE_RE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_WHITESPACE_RE = re.compile(r"\s+")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)[xX×*✕✖⨯](\d+(?:\.\d+)?)$")


def default_yield_table() -> dict[str, dict[str, int]]:
    """A fresh, independent copy of :data:`DEFAULT_YIELD` that is safe to edit."""
    return copy.deepcopy(DEFAULT_YIELD)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub("", _INVISIBLE_RE.sub("", normalize_digits(text)))


def _side(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex guarantees a number
        raise ValueError(f"Invalid size number {text!r}") from None
    if value <= 0 or value > _MAX_SIDE_CM:
        raise ValueError(f"Print side must be between 0 and {_MAX_SIDE_CM} cm, got {text}")
    return value


def _fmt_side(value: Decimal) -> str:
    return format(value.normalize(), "f")


def normalize_size(text) -> str:
    """Canonical ``'WxH'`` (centimetres, short side first) for a size text.

    Accepts Persian / Arabic / Latin digits and the separators ``x X × * ✕``
    with optional spaces: ``'۱۶×۲۱'``, ``'16*21'``, ``'16 x 21'`` and
    ``'21x16'`` all give ``'16x21'``.  Decimal sides such as ``'12.5x18'`` are
    kept (``'12.5x18'``).  Raises ``ValueError`` for anything else.
    """
    if not isinstance(text, str):
        raise ValueError(f"Print size must be text like '16x21', got {text!r}")
    match = _SIZE_RE.match(_clean(text))
    if not match:
        raise ValueError(f"Not a print size (expected e.g. '16x21'): {text!r}")
    first, second = _side(match.group(1)), _side(match.group(2))
    short, long_ = sorted((first, second))
    return f"{_fmt_side(short)}x{_fmt_side(long_)}"


def normalize_paper(paper) -> str:
    """Canonical paper name: trimmed and upper-case (``' a4 '`` -> ``'A4'``)."""
    if not isinstance(paper, str):
        raise ValueError(f"Paper must be text like 'A4', got {paper!r}")
    name = _clean(paper).upper()
    if not name:
        raise ValueError("Paper name is empty")
    return name


def _size_sort_key(size: str) -> tuple[Decimal, Decimal]:
    first, second = size.split("x")
    return Decimal(first), Decimal(second)


def _count(name: str, value: Any, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    return number


def _prepare_table(table: Optional[Mapping[str, Mapping[str, int]]]) -> dict[str, dict[str, int]]:
    """Validate a yield table and canonicalise its paper and size keys."""
    source = DEFAULT_YIELD if table is None else table
    if not isinstance(source, Mapping):
        raise ValueError("Yield table must be a mapping {paper: {size: prints_per_sheet}}")
    prepared: dict[str, dict[str, int]] = {}
    for paper, sizes in source.items():
        name = normalize_paper(paper)
        if not isinstance(sizes, Mapping):
            raise ValueError(f"Yield table entry for {name} must be a mapping of sizes")
        entry = prepared.setdefault(name, {})
        for size, per_sheet in sizes.items():
            key = normalize_size(size)
            value = _count(f"yield of {key} on {name}", per_sheet)
            if key in entry and entry[key] != value:
                raise ValueError(f"Conflicting yields for {key} on {name}")
            entry[key] = value
    return prepared


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PrintRow:
    """One printable size: *prints* of *size* need *sheets* sheets of *paper*."""

    size: str
    prints: int
    per_sheet: int
    sheets: int
    paper: str = "A4"

    def as_dict(self) -> dict:
        return {
            "size": self.size,
            "prints": self.prints,
            "per_sheet": self.per_sheet,
            "sheets": self.sheets,
            "paper": self.paper,
        }


@dataclass(frozen=True)
class PaperTotals:
    """Sheets and packs needed of one paper type."""

    paper: str
    sheets: int
    sheets_per_pack: int
    packs: int
    leftover_sheets: int

    def as_dict(self) -> dict:
        return {
            "paper": self.paper,
            "sheets": self.sheets,
            "sheets_per_pack": self.sheets_per_pack,
            "packs": self.packs,
            "leftover_sheets": self.leftover_sheets,
        }


@dataclass(frozen=True)
class PrintPlan:
    """What an event's print sales need.

    ``total_sheets``, ``packs`` and ``leftover_sheets_in_last_pack`` add up over
    all paper types (each paper type is bought in its own packs - see
    ``by_paper`` for the per-paper numbers; with a single paper they are the
    plain figures).  ``unsupported`` holds Persian reasons for sizes that could
    not be planned and ``unsupported_sizes`` the matching size keys.
    """

    paper: str
    rows: list[PrintRow] = field(default_factory=list)
    total_sheets: int = 0
    packs: int = 0
    leftover_sheets_in_last_pack: int = 0
    unsupported: list[str] = field(default_factory=list)
    unsupported_sizes: list[str] = field(default_factory=list)
    by_paper: dict[str, PaperTotals] = field(default_factory=dict)

    @property
    def total_prints(self) -> int:
        """Prints that were planned (unsupported sizes are not included)."""
        return sum(row.prints for row in self.rows)

    @property
    def ok(self) -> bool:
        """True when every requested size could be planned."""
        return not self.unsupported

    def as_dict(self) -> dict:
        return {
            "paper": self.paper,
            "rows": [row.as_dict() for row in self.rows],
            "total_sheets": self.total_sheets,
            "packs": self.packs,
            "leftover_sheets_in_last_pack": self.leftover_sheets_in_last_pack,
            "unsupported": list(self.unsupported),
            "unsupported_sizes": list(self.unsupported_sizes),
            "by_paper": {name: totals.as_dict() for name, totals in self.by_paper.items()},
        }


# --------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------

def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def sheet_yield(size: str, paper: str = "A4", table: Optional[Mapping] = None) -> int:
    """Prints of *size* per sheet of *paper*; ``0`` when it does not fit or is unknown."""
    return _prepare_table(table).get(normalize_paper(paper), {}).get(normalize_size(size), 0)


def _merge_counts(prints_by_size: Mapping[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Canonicalise size keys (summing duplicates) and split off unreadable keys."""
    if not isinstance(prints_by_size, Mapping):
        raise ValueError("prints_by_size must be a mapping {size: count}")
    good: dict[str, int] = {}
    bad: dict[str, int] = {}
    for raw_size, raw_count in prints_by_size.items():
        count = _count(f"prints of {raw_size!r}", raw_count)
        if count == 0:
            continue
        try:
            key = normalize_size(raw_size)
        except ValueError:
            bad[str(raw_size)] = bad.get(str(raw_size), 0) + count
            continue
        good[key] = good.get(key, 0) + count
    return good, bad


def _plan_rows(
    counts: Mapping[str, int],
    default_paper: str,
    paper_by_size: Mapping[str, str],
    table: Mapping[str, Mapping[str, int]],
) -> tuple[list[PrintRow], list[tuple[str, str]]]:
    rows: list[PrintRow] = []
    problems: list[tuple[str, str]] = []
    for size in sorted(counts, key=_size_sort_key):
        prints = counts[size]
        paper = paper_by_size.get(size, default_paper)
        sizes = table.get(paper)
        per_sheet = 0 if sizes is None else sizes.get(size, 0)
        if per_sheet > 0:
            rows.append(PrintRow(size, prints, per_sheet, _ceil_div(prints, per_sheet), paper))
            continue
        fits_on = sorted(p for p, s in table.items() if p != paper and s.get(size, 0) > 0)
        if sizes is None:
            reason = f"کاغذ {paper} در جدول بازدهی چاپ تعریف نشده است"
        elif size in sizes or fits_on:
            # A known size that this paper cannot hold (e.g. 30x40 on A4).
            reason = f"اندازهٔ {size} روی کاغذ {paper} جا نمی‌شود"
            if fits_on:
                reason += f" (روی {'، '.join(fits_on)} قابل چاپ است)"
        else:
            reason = f"برای اندازهٔ سفارشی {size} روی کاغذ {paper} بازدهی تعریف نشده است"
        problems.append((size, reason))
    rows.sort(key=lambda r: (r.paper, _size_sort_key(r.size)))
    return rows, problems


def sheets_needed(
    prints_by_size: Mapping[str, int],
    paper: str = "A4",
    table: Optional[Mapping[str, Mapping[str, int]]] = None,
) -> int:
    """Total sheets of *paper*: the sum over sizes of ``ceil(count / yield)``.

    Sizes never share a sheet.  Size keys may be written in any notation
    (``'۱۶×۲۱'``, ``'21x16'`` ...) and equivalent keys are added together.
    Raises ``ValueError`` if a requested size cannot be printed on *paper*;
    use :func:`plan_for_sales` for a non-raising report.
    """
    counts, bad = _merge_counts(prints_by_size)
    if bad:
        raise ValueError(f"Unreadable print sizes: {', '.join(sorted(bad))}")
    rows, problems = _plan_rows(counts, normalize_paper(paper), {}, _prepare_table(table))
    if problems:
        raise ValueError("; ".join(reason for _, reason in problems))
    return sum(row.sheets for row in rows)


def packs_needed(sheets: int, sheets_per_pack: int) -> int:
    """Packs to buy for *sheets* sheets: ``ceil(sheets / sheets_per_pack)``."""
    sheets = _count("sheets", sheets)
    sheets_per_pack = _count("sheets_per_pack", sheets_per_pack, minimum=1)
    return _ceil_div(sheets, sheets_per_pack)


def plan_for_sales(
    prints_by_size: Mapping[str, int],
    paper: str = "A4",
    sheets_per_pack: Union[int, Mapping[str, int]] = 20,
    table: Optional[Mapping[str, Mapping[str, int]]] = None,
    paper_by_size: Optional[Mapping[str, str]] = None,
) -> PrintPlan:
    """Plan sheets and packs for an event's print sales.

    * ``paper`` is the default paper; ``paper_by_size`` overrides it per size
      (e.g. ``{'30x40': 'A3'}``).
    * ``sheets_per_pack`` is an int, or a mapping ``{paper: int}`` when paper
      types are packed differently (missing papers fall back to 20).
    * Sizes that cannot be printed (do not fit, no table entry, unknown paper,
      unreadable size text) do not raise - they are reported in ``unsupported``.
    """
    default_paper = normalize_paper(paper)
    prepared = _prepare_table(table)
    overrides = {
        normalize_size(size): normalize_paper(name)
        for size, name in (paper_by_size or {}).items()
    }
    pack_sizes: dict[str, int] = {}
    if isinstance(sheets_per_pack, Mapping):
        for name, value in sheets_per_pack.items():
            pack_sizes[normalize_paper(name)] = _count(f"sheets_per_pack of {name}", value, 1)
        fallback_pack = 20
    else:
        fallback_pack = _count("sheets_per_pack", sheets_per_pack, minimum=1)

    counts, bad = _merge_counts(prints_by_size)
    rows, problems = _plan_rows(counts, default_paper, overrides, prepared)
    unsupported = [reason for _, reason in problems]
    unsupported_sizes = [size for size, _ in problems]
    for raw in sorted(bad):
        unsupported.append(f"اندازهٔ «{raw}» نامعتبر است (نمونهٔ درست: 16x21)")
        unsupported_sizes.append(raw)

    sheets_by_paper: dict[str, int] = {}
    for row in rows:
        sheets_by_paper[row.paper] = sheets_by_paper.get(row.paper, 0) + row.sheets
    by_paper: dict[str, PaperTotals] = {}
    for name in sorted(sheets_by_paper):
        per_pack = pack_sizes.get(name, fallback_pack)
        sheets = sheets_by_paper[name]
        packs = _ceil_div(sheets, per_pack)
        by_paper[name] = PaperTotals(name, sheets, per_pack, packs, packs * per_pack - sheets)

    return PrintPlan(
        paper=default_paper,
        rows=rows,
        total_sheets=sum(t.sheets for t in by_paper.values()),
        packs=sum(t.packs for t in by_paper.values()),
        leftover_sheets_in_last_pack=sum(t.leftover_sheets for t in by_paper.values()),
        unsupported=unsupported,
        unsupported_sizes=unsupported_sizes,
        by_paper=by_paper,
    )
