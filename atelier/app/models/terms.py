"""Pay-terms shape shared by assignments, job posts and presets.

A *terms dict* is the single interchange format everywhere in the codebase::

    {"base": 1_000_000,
     "commission_per_unit": 9_000,
     "bonus_tiers": [{"threshold": 70, "amount": 150_000}]}

It matches the field names of ``payroll.PayTerms`` so
``payroll.terms_from_dict(assignment.terms_dict())`` works.  Database columns
use ``base_pay`` instead of ``base`` (``base`` reads badly next to ``Base``).

Bonus-tier semantics (AGENTS.md, assumption A1): the tier with the highest
``threshold <= units`` applies; tiers do not stack.  This module only stores
and validates tiers, the arithmetic lives in ``app.services.payroll``.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Mapped, validates

from app.models.base import check_int, check_money, json_col, money
from app.services.constants import Skill

TERMS_FIELDS: tuple[str, ...] = ("base_pay", "commission_per_unit", "bonus_tiers")


def normalize_bonus_tiers(value: Any) -> list[dict[str, int]]:
    """Validate bonus tiers and return a fresh list sorted by threshold.

    Accepts ``None`` (-> ``[]``), a list of ``{"threshold", "amount"}`` dicts,
    ``(threshold, amount)`` pairs or ``BonusTier``-like objects.  Duplicate
    thresholds are rejected (they would make "the highest tier" ambiguous).
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise TypeError("bonus_tiers must be a list of {threshold, amount}")
    tiers: list[dict[str, int]] = []
    for item in value:
        if isinstance(item, Mapping):
            threshold, amount = item.get("threshold"), item.get("amount")
        elif hasattr(item, "threshold") and hasattr(item, "amount"):
            threshold, amount = item.threshold, item.amount
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            threshold, amount = item
        else:
            raise TypeError(f"invalid bonus tier: {item!r}")
        threshold = check_int(threshold, "bonus threshold", minimum=0)
        amount = check_money(amount, "bonus amount")
        if threshold is None or amount is None:
            raise ValueError("bonus tier needs both threshold and amount")
        tiers.append({"threshold": threshold, "amount": amount})
    tiers.sort(key=lambda tier: tier["threshold"])
    thresholds = [tier["threshold"] for tier in tiers]
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("duplicate bonus tier thresholds")
    return tiers


def _read(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def normalize_terms(terms: Any) -> dict[str, Any]:
    """Return a validated terms dict from a mapping or a ``PayTerms``-like object.

    Missing ``base`` / ``commission_per_unit`` mean 0 and missing tiers mean
    none.  ``base`` may also be spelled ``base_pay``.  ``None`` is an error:
    callers must decide terms explicitly (the snapshot rule).
    """
    if terms is None:
        raise TypeError("terms are required (use a dict or PayTerms)")
    base = check_money(_read(terms, "base", "base_pay"), "base")
    commission = check_money(_read(terms, "commission_per_unit"), "commission_per_unit")
    tiers = normalize_bonus_tiers(_read(terms, "bonus_tiers"))
    return {
        "base": base or 0,
        "commission_per_unit": commission or 0,
        "bonus_tiers": tiers,
    }


def normalize_terms_by_role(value: Any) -> dict[str, dict[str, Any]]:
    """Validate ``{role: terms}`` presets (roles must be known skills)."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping of role -> terms")
    return {Skill.coerce(role).value: normalize_terms(terms) for role, terms in value.items()}


def normalize_skills(value: Any) -> list[str]:
    """Validate a list of skills; returns unique values in canonical order."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = [value]
    wanted = {Skill.coerce(item) for item in value}
    return [member.value for member in Skill if member in wanted]


class TermsMixin:
    """The three snapshot columns: ``base_pay``, ``commission_per_unit``, ``bonus_tiers``."""

    base_pay: Mapped[int] = money()
    commission_per_unit: Mapped[int] = money()
    bonus_tiers: Mapped[list] = json_col(list)

    @validates("base_pay", "commission_per_unit")
    def _validate_terms_money(self, key: str, value: Any) -> int | None:
        return check_money(value, key)

    @validates("bonus_tiers")
    def _validate_bonus_tiers(self, key: str, value: Any) -> list[dict[str, int]]:
        return normalize_bonus_tiers(value)

    def terms_dict(self) -> dict[str, Any]:
        """Current terms as a plain terms dict (a deep copy)."""
        return {
            "base": self.base_pay or 0,
            "commission_per_unit": self.commission_per_unit or 0,
            "bonus_tiers": [dict(tier) for tier in (self.bonus_tiers or [])],
        }

    def apply_terms(self, terms: Any) -> None:
        """Overwrite the three columns from *terms* (dict or ``PayTerms``)."""
        data = normalize_terms(terms)
        self.base_pay = data["base"]
        self.commission_per_unit = data["commission_per_unit"]
        self.bonus_tiers = data["bonus_tiers"]

    def pay_terms(self):
        """The terms as a ``payroll.PayTerms`` value object.

        ``app.services.payroll`` is imported lazily so the models package has no
        import-time dependency on it.
        """
        from app.services.payroll import BonusTier, PayTerms

        return PayTerms(
            base=self.base_pay or 0,
            commission_per_unit=self.commission_per_unit or 0,
            bonus_tiers=tuple(
                BonusTier(threshold=tier["threshold"], amount=tier["amount"])
                for tier in (self.bonus_tiers or [])
            ),
        )
