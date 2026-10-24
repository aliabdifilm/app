"""Pay terms in plain Persian, with worked examples.

Nothing here does pay maths of its own: every number comes from :func:`app.services.payroll.compute_pay`
(AGENTS.md formula ``total = base + units x commission_per_unit + highest reached bonus tier``).
This module only words the result for the person who is going to be paid.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.services import payroll
from app.services.money import format_toman, to_persian_digits

#: The reference volume of the examples (the AGENTS.md test vector: 80 photos -> 1,870,000 Toman).
REFERENCE_UNITS = 80
#: Volumes shown in the scenario table besides the bonus thresholds.
SCENARIO_UNITS = (40, 80, 120)
MAX_SCENARIOS = 6

TIERS_DO_NOT_STACK = "پله‌های پاداش جمع نمی‌شوند؛ فقط بالاترین پله‌ای که به آن برسید پرداخت می‌شود."
UNITS_EXPLAINED = (
    "تعداد فروش مراسم، جمع «تعداد × ضریب» ردیف‌های فاکتور است؛ همین عدد برای همهٔ نیروهای مراسم یکسان است."
)
FORMULA = "حقوق = پایه + (تعداد فروش × پورسانت هر عکس) + پاداش پلهٔ رسیده"


def money(value: int) -> str:
    """``1870000`` -> ``۱٬۸۷۰٬۰۰۰`` (Persian digits, no unit)."""
    return format_toman(int(value), persian_digits=True)


def fa(value: Any) -> str:
    return to_persian_digits(value)


def terms_are_empty(terms: Optional[Mapping[str, Any]]) -> bool:
    """True when the terms promise nothing at all (no base, no commission, no bonus tier)."""
    if not terms:
        return True
    return not (terms.get("base") or terms.get("commission_per_unit") or terms.get("bonus_tiers"))


def terms_lines(terms: Optional[Mapping[str, Any]]) -> list[str]:
    """The terms as short sentences: base, commission per photo, one line per bonus tier."""
    if not terms:
        return []
    lines: list[str] = []
    base = int(terms.get("base") or 0)
    commission = int(terms.get("commission_per_unit") or 0)
    if base:
        lines.append(f"پایهٔ ثابت {money(base)} تومان")
    if commission:
        lines.append(f"به‌ازای هر عکس فروخته‌شده {money(commission)} تومان پورسانت")
    for tier in sorted(terms.get("bonus_tiers") or [], key=lambda item: item["threshold"]):
        lines.append(
            f"پاداش: با فروش {fa(tier['threshold'])} عکس به بالا، {money(int(tier['amount']))} تومان"
        )
    return lines


def terms_summary(terms: Optional[Mapping[str, Any]]) -> str:
    """One line for tooltips, share texts and notifications."""
    lines = terms_lines(terms)
    return "؛ ".join(lines) if lines else "مبلغ با توافق تعیین می‌شود"


def example(terms: Optional[Mapping[str, Any]], units: int = REFERENCE_UNITS) -> Optional[dict[str, Any]]:
    """``payroll.compute_pay`` for *units* sold photos as a dict, or ``None`` for empty / invalid terms.

    Keys: ``units, base, commission, bonus, total, applied_tier`` (``{threshold, amount}`` or ``None``) and
    ``sentence`` («اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان»).
    """
    if terms_are_empty(terms):
        return None
    try:
        result = payroll.compute_pay(payroll.terms_from_dict(terms), units).as_dict()
    except (ValueError, TypeError, KeyError):
        return None
    result["sentence"] = f"اگر {fa(units)} عکس فروخته شود: {money(result['total'])} تومان"
    return result


def scenarios(terms: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A small table «اگر N عکس فروخته شود» around the usual volumes and every bonus threshold."""
    if terms_are_empty(terms):
        return []
    thresholds = [int(tier["threshold"]) for tier in (terms.get("bonus_tiers") or [])]
    units = sorted({*SCENARIO_UNITS, *(value for value in thresholds if 0 < value <= 5000)})
    if len(units) > MAX_SCENARIOS:  # keep the reference volume and the extremes
        keep = {REFERENCE_UNITS, units[0], units[-1]}
        spare = [value for value in units if value not in keep]
        units = sorted(keep | set(spare[: MAX_SCENARIOS - len(keep)]))
    rows = [example(terms, value) for value in units]
    return [row for row in rows if row]


def has_several_tiers(terms: Optional[Mapping[str, Any]]) -> bool:
    """With two or more tiers the reader must know they do not add up (assumption A1)."""
    return len((terms or {}).get("bonus_tiers") or []) > 1


def tier_text(tier: Optional[Mapping[str, Any]]) -> str:
    """«پلهٔ ۷۰ عکس به بالا (۱۵۰٬۰۰۰ تومان)» or the no-tier sentence."""
    if not tier:
        return "به هیچ پلهٔ پاداشی نرسیده‌اید"
    return f"پلهٔ {fa(tier['threshold'])} عکس به بالا ({money(int(tier['amount']))} تومان)"


def breakdown_lines(line: Any, terms: Optional[Mapping[str, Any]] = None) -> list[dict[str, str]]:
    """Explain one payroll line (a ``PayrollLine``-like object) in three rows plus the total.

    The numbers are the ones stored on the line - exactly what is paid.  The commission row shows
    ``units x rate`` only when that product really equals the stored commission (the rate comes from the
    terms snapshot of the afish), so the explanation can never contradict the amount.
    """
    units = int(line.units)
    rows = [
        {
            "label": "پایه",
            "formula": "مبلغ ثابت توافقی",
            "amount": money(line.base),
        }
    ]
    rate = int((terms or {}).get("commission_per_unit") or 0)
    if rate and units * rate == line.commission:
        formula = f"{fa(units)} عکس × {money(rate)} تومان"
    elif units:
        formula = f"{fa(units)} عکس فروخته‌شده"
    else:
        formula = "فروشی ثبت نشده"
    rows.append({"label": "پورسانت", "formula": formula, "amount": money(line.commission)})
    tier = getattr(line, "applied_tier", None)
    if line.bonus:
        rows.append({"label": "پاداش", "formula": tier_text(tier), "amount": money(line.bonus)})
    else:
        rows.append({"label": "پاداش", "formula": tier_text(None) if (terms or {}).get("bonus_tiers") else "پاداشی توافق نشده", "amount": money(0)})
    return rows
