"""Staff pay for one event (pure Python, integer math only).

Formula (AGENTS.md, "فرمول حقوق نیروها")::

    units      = sum(qty * weight)   over the event's sold lines that count
    commission = units * commission_per_unit
    bonus      = amount of the HIGHEST tier with threshold <= units, else 0
    total_pay  = base + commission + bonus

Assumption A1: bonus tiers do **not** accumulate - only the highest reached
tier applies ("70 and above" means ``>= 70``).  Assumption A2: every sold line
counts unless it is flagged ``counts=False``.  Assumption A3: every person on
the event is paid with their own individually agreed terms, but all of them
share the same ``units``.

Every number must be a real ``int`` (``bool`` and ``float`` are rejected) and
non-negative; violations raise ``ValueError``.  The terms are snapshotted per
person and per event at booking time - use :func:`terms_to_dict` /
:func:`terms_from_dict` to store them as JSON.
"""
from __future__ import annotations

import numbers
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "BonusTier",
    "PayTerms",
    "SaleLine",
    "PayBreakdown",
    "units_from_lines",
    "bonus_for",
    "compute_pay",
    "terms_to_dict",
    "terms_from_dict",
    "compute_event_payroll",
    "total_payroll",
]


def _int(name: str, value: Any, minimum: int = 0) -> int:
    """Return *value* as ``int`` if it is a genuine integer >= *minimum*."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    return number


@dataclass(frozen=True)
class BonusTier:
    """Pay *amount* once the event sold at least *threshold* units."""

    threshold: int
    amount: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "threshold", _int("threshold", self.threshold))
        object.__setattr__(self, "amount", _int("amount", self.amount))


def _coerce_tier(tier: Any) -> BonusTier:
    if isinstance(tier, BonusTier):
        return tier
    if isinstance(tier, Mapping):
        try:
            return BonusTier(tier["threshold"], tier["amount"])
        except KeyError as exc:
            raise ValueError(f"Bonus tier is missing {exc.args[0]!r}") from None
    if isinstance(tier, (tuple, list)) and len(tier) == 2:
        return BonusTier(tier[0], tier[1])
    raise ValueError(f"Cannot read a bonus tier from {tier!r}")


@dataclass(frozen=True)
class PayTerms:
    """The individually agreed pay of one person for one event."""

    base: int
    commission_per_unit: int
    bonus_tiers: tuple[BonusTier, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", _int("base", self.base))
        object.__setattr__(
            self, "commission_per_unit", _int("commission_per_unit", self.commission_per_unit)
        )
        tiers = self.bonus_tiers
        if tiers is None:
            tiers = ()
        if isinstance(tiers, (str, bytes, Mapping)):
            raise ValueError("bonus_tiers must be a sequence of tiers")
        object.__setattr__(self, "bonus_tiers", tuple(_coerce_tier(t) for t in tiers))


@dataclass(frozen=True)
class SaleLine:
    """One sold product row: *qty* items, each counting *weight* units."""

    qty: int
    weight: int = 1
    counts: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "qty", _int("qty", self.qty))
        object.__setattr__(self, "weight", _int("weight", self.weight))
        object.__setattr__(self, "counts", _as_flag(self.counts))


def _as_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral) and int(value) in (0, 1):
        return bool(value)
    raise ValueError(f"counts must be a boolean, got {value!r}")


@dataclass(frozen=True)
class PayBreakdown:
    """The result of :func:`compute_pay` for one person."""

    units: int
    base: int
    commission: int
    bonus: int
    total: int
    applied_tier: Optional[BonusTier] = None

    def as_dict(self) -> dict:
        """JSON-friendly dict; ``applied_tier`` becomes ``{threshold, amount}`` or ``None``."""
        tier = self.applied_tier
        return {
            "units": self.units,
            "base": self.base,
            "commission": self.commission,
            "bonus": self.bonus,
            "total": self.total,
            "applied_tier": None
            if tier is None
            else {"threshold": tier.threshold, "amount": tier.amount},
        }


_MISSING = object()


def _line_field(line: Any, names: tuple[str, ...], default: Any) -> Any:
    """Read the first present, non-None field among *names* from a dict or object."""
    for name in names:
        if isinstance(line, Mapping):
            value = line.get(name, _MISSING)
        else:
            value = getattr(line, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _line_units(line: Any) -> int:
    """Units of one line (0 when it does not count towards commission)."""
    if isinstance(line, SaleLine):
        return line.qty * line.weight if line.counts else 0
    if not isinstance(line, Mapping) and not hasattr(line, "qty"):
        raise ValueError(f"Cannot read a sale line from {line!r}")
    if not _as_flag(_line_field(line, ("counts", "counts_for_commission"), True)):
        return 0
    qty = _line_field(line, ("qty",), _MISSING)
    if qty is _MISSING:
        raise ValueError(f"Sale line has no 'qty': {line!r}")
    return _int("qty", qty) * _int("weight", _line_field(line, ("weight",), 1))


def units_from_lines(lines: Iterable[Any]) -> int:
    """Sum ``qty * weight`` over the lines that count.

    Each line may be a :class:`SaleLine`, a mapping with ``qty`` (and optional
    ``weight`` / ``counts``) or any object with those attributes (the
    attribute ``counts_for_commission`` is accepted as an alias of ``counts``).
    A missing or ``None`` weight means 1 and a missing or ``None`` ``counts``
    means true.  Lines with ``counts=False`` are ignored entirely.
    """
    return sum(_line_units(line) for line in (lines or ()))


def bonus_for(units: int, tiers: Iterable[Any]) -> tuple[int, Optional[BonusTier]]:
    """Return ``(amount, applied_tier)`` for the highest tier with threshold <= units.

    Tiers are not cumulative (A1) and their order is irrelevant.  If several
    tiers share the winning threshold the largest amount wins, so the result is
    deterministic.  ``(0, None)`` when no tier is reached.
    """
    units = _int("units", units)
    best: Optional[BonusTier] = None
    for raw in tiers or ():
        tier = _coerce_tier(raw)
        if tier.threshold <= units and (
            best is None or (tier.threshold, tier.amount) > (best.threshold, best.amount)
        ):
            best = tier
    if best is None:
        return 0, None
    return best.amount, best


def _coerce_terms(terms: Any) -> PayTerms:
    if isinstance(terms, PayTerms):
        return terms
    if isinstance(terms, Mapping):
        return terms_from_dict(terms)
    raise ValueError(f"Cannot read pay terms from {terms!r}")


def compute_pay(terms: PayTerms, units: int) -> PayBreakdown:
    """Apply the pay formula for one person (*terms* may also be a snapshot dict)."""
    terms = _coerce_terms(terms)
    units = _int("units", units)
    commission = units * terms.commission_per_unit
    bonus, tier = bonus_for(units, terms.bonus_tiers)
    return PayBreakdown(
        units=units,
        base=terms.base,
        commission=commission,
        bonus=bonus,
        total=terms.base + commission + bonus,
        applied_tier=tier,
    )


def terms_to_dict(terms: PayTerms) -> dict:
    """JSON-serialisable snapshot of *terms* (tiers keep their given order)."""
    terms = _coerce_terms(terms)
    return {
        "base": terms.base,
        "commission_per_unit": terms.commission_per_unit,
        "bonus_tiers": [
            {"threshold": t.threshold, "amount": t.amount} for t in terms.bonus_tiers
        ],
    }


def terms_from_dict(d: Mapping[str, Any]) -> PayTerms:
    """Inverse of :func:`terms_to_dict`; a missing ``bonus_tiers`` means no tiers."""
    if not isinstance(d, Mapping):
        raise ValueError(f"Pay terms snapshot must be a mapping, got {d!r}")
    for key in ("base", "commission_per_unit"):
        if key not in d:
            raise ValueError(f"Pay terms snapshot is missing {key!r}")
    return PayTerms(
        base=d["base"],
        commission_per_unit=d["commission_per_unit"],
        bonus_tiers=tuple(_coerce_tier(t) for t in (d.get("bonus_tiers") or ())),
    )


def compute_event_payroll(
    people: Mapping[Any, PayTerms], lines: Iterable[Any]
) -> dict[Any, PayBreakdown]:
    """Pay everyone on an event: ``{key: PayBreakdown}`` in the order of *people*.

    The event's units are computed once from *lines* and shared by everybody
    (A3); each person's own terms decide base, commission rate and bonus tiers.
    """
    units = units_from_lines(lines)
    return {key: compute_pay(terms, units) for key, terms in people.items()}


def total_payroll(breakdowns: Mapping[Any, PayBreakdown]) -> int:
    """Sum of ``total`` over the result of :func:`compute_event_payroll`."""
    return sum(b.total for b in breakdowns.values())
