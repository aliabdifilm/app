"""Small helpers shared by the marketplace routes, services and templates.

Nothing here touches the database.  Pay maths is **never** done here: every number shown to a
person comes from :mod:`app.services.payroll` (``compute_pay``), this module only words it.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from flask import current_app
from flask_sqlalchemy.pagination import Pagination

from app.services import payroll
from app.services.constants import ApplicationStatus, JobPostStatus
from app.services.money import format_toman, normalize_digits, to_persian_digits

#: The "what would this pay" examples assume this many sold photos (AGENTS.md test vector: 80 -> 1,870,000).
REFERENCE_UNITS = 80
#: Candidate units for the scenario table on a job page (plus every bonus threshold).
SCENARIO_UNITS = (40, 80, 120)
MAX_SCENARIOS = 6

#: Spam protection defaults (override in the app config; all are "per 24 hours").
DEFAULT_LIMITS = {
    "MARKETPLACE_MAX_POSTS_PER_DAY": 10,
    "MARKETPLACE_MAX_APPLICATIONS_PER_DAY": 20,
    "MARKETPLACE_MAX_INVITES_PER_DAY": 20,
    "MARKETPLACE_MAX_REPORTS_PER_DAY": 10,
}
#: How many candidate rows the in-Python "minimum pay / sort by pay" path looks at.
PAY_SORT_CANDIDATES = 1000


def limit(name: str) -> int:
    """A spam limit from the app config (``MARKETPLACE_*``), falling back to the default."""
    try:
        value = int(current_app.config.get(name, DEFAULT_LIMITS[name]))
    except (TypeError, ValueError):
        value = DEFAULT_LIMITS[name]
    return max(0, value)


# --------------------------------------------------------------------------
# names (privacy)
# --------------------------------------------------------------------------

_LONG_DIGITS = re.compile(r"\d{8,}")
ANONYMOUS_NAME = "کاربر بی‌نام"


def person_name(user: Any) -> str:
    """The name to show for a user.

    ``User.display_name`` falls back to the **mobile number** for unnamed accounts, which would leak
    it into every card; this never does (a name that is itself a long digit string is hidden too).
    """
    name = (getattr(user, "full_name", "") or "").strip()
    if not name or _LONG_DIGITS.search(normalize_digits(name).replace(" ", "")):
        return ANONYMOUS_NAME
    return name


def clip(text: Optional[str], length: int = 140) -> str:
    """A one-line excerpt for lists (whitespace collapsed, ellipsis added when cut)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= length else flat[: length - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# pay terms in plain Persian
# --------------------------------------------------------------------------


def _money(value: int) -> str:
    return format_toman(value, persian_digits=True)


def terms_lines(terms: Optional[Mapping[str, Any]]) -> list[str]:
    """The terms as short plain sentences: base, commission per photo, one line per bonus tier."""
    if not terms:
        return []
    lines: list[str] = []
    base = int(terms.get("base") or 0)
    commission = int(terms.get("commission_per_unit") or 0)
    if base:
        lines.append(f"پایهٔ ثابت {_money(base)} تومان")
    if commission:
        lines.append(f"به‌ازای هر عکس فروخته‌شده {_money(commission)} تومان پورسانت")
    for tier in terms.get("bonus_tiers") or []:
        lines.append(
            f"پاداش: با فروش {to_persian_digits(tier['threshold'])} عکس به بالا، "
            f"{_money(int(tier['amount']))} تومان"
        )
    return lines


def terms_are_empty(terms: Optional[Mapping[str, Any]]) -> bool:
    """True when the terms promise nothing (no base, no commission, no bonus tier)."""
    if not terms:
        return True
    return not (terms.get("base") or terms.get("commission_per_unit") or terms.get("bonus_tiers"))


def terms_text(terms: Optional[Mapping[str, Any]]) -> str:
    """One line for notifications and tooltips."""
    lines = terms_lines(terms)
    return "؛ ".join(lines) if lines else "مبلغ با توافق تعیین می‌شود"


def has_stacking_note(terms: Optional[Mapping[str, Any]]) -> bool:
    """With two or more tiers the reader must know they do not add up (assumption A1)."""
    return len((terms or {}).get("bonus_tiers") or []) > 1


TIERS_DO_NOT_STACK = "پله‌های پاداش جمع نمی‌شوند؛ فقط بالاترین پله‌ای که به آن برسید پرداخت می‌شود."


def pay_example(terms: Optional[Mapping[str, Any]], units: int = REFERENCE_UNITS) -> Optional[dict[str, Any]]:
    """``payroll.compute_pay`` for *units* sold photos as a dict (``None`` for empty/invalid terms).

    Keys: ``units, base, commission, bonus, total, applied_tier`` plus ``sentence`` (Persian):
    «اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان».
    """
    if terms_are_empty(terms):
        return None
    try:
        result = payroll.compute_pay(payroll.terms_from_dict(terms), units).as_dict()
    except (ValueError, TypeError, KeyError):
        return None
    result["sentence"] = f"اگر {to_persian_digits(units)} عکس فروخته شود: {_money(result['total'])} تومان"
    result["formula"] = " + ".join(_money(result[key]) for key in ("base", "commission", "bonus"))
    return result


def pay_scenarios(terms: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A small table «اگر N عکس فروخته شود» around the usual volumes and the bonus thresholds."""
    if terms_are_empty(terms):
        return []
    thresholds = [int(tier["threshold"]) for tier in (terms.get("bonus_tiers") or [])]
    units = sorted({*SCENARIO_UNITS, *(t for t in thresholds if 0 < t <= 5000)})
    if len(units) > MAX_SCENARIOS:  # keep the reference volume and the extremes
        keep = {REFERENCE_UNITS, units[0], units[-1]}
        spare = [u for u in units if u not in keep]
        units = sorted(keep | set(spare[: MAX_SCENARIOS - len(keep)]))
    rows = [pay_example(terms, value) for value in units]
    return [row for row in rows if row]


def reference_total(terms: Optional[Mapping[str, Any]]) -> int:
    """The number the "minimum pay" filter and the "sort by pay" order use (total at 80 photos)."""
    example = pay_example(terms)
    return int(example["total"]) if example else 0


# --------------------------------------------------------------------------
# tones (badge colours)
# --------------------------------------------------------------------------

APPLICATION_TONES = {
    ApplicationStatus.PENDING: "warning",
    ApplicationStatus.ACCEPTED: "success",
    ApplicationStatus.REJECTED: "danger",
    ApplicationStatus.WITHDRAWN: "neutral",
}
POST_TONES = {
    JobPostStatus.OPEN: "success",
    JobPostStatus.FILLED: "info",
    JobPostStatus.CLOSED: "neutral",
    JobPostStatus.HIDDEN: "warning",
}


def application_tone(status: Any) -> str:
    return APPLICATION_TONES.get(ApplicationStatus.coerce(status), "neutral")


def post_tone(status: Any) -> str:
    return POST_TONES.get(JobPostStatus.coerce(status), "neutral")


# --------------------------------------------------------------------------
# pagination over a Python list (filters that cannot run in SQL)
# --------------------------------------------------------------------------


class ListPagination(Pagination):
    """Same attributes as the Flask-SQLAlchemy object, so the ``pagination`` macro renders it."""

    def _query_items(self) -> list:
        rows = self._query_args["rows"]
        start = (self.page - 1) * self.per_page
        return rows[start : start + self.per_page]

    def _query_count(self) -> int:
        return len(self._query_args["rows"])


def paginate_list(rows: list, *, per_page: int = 12) -> ListPagination:
    from app.services.http import MAX_PER_PAGE, page_args

    page, size = page_args(per_page, MAX_PER_PAGE)
    return ListPagination(page=page, per_page=size, max_per_page=MAX_PER_PAGE, error_out=False, count=True, rows=rows)


# --------------------------------------------------------------------------
# prefill: the contract with the ateliers module ("70:150000,100:300000")
# --------------------------------------------------------------------------


def parse_tiers_query(text: Optional[str]) -> list[dict[str, int]]:
    """``'70:150000,100:300000'`` -> tiers; junk pieces are skipped (this is only a form prefill)."""
    tiers: list[dict[str, int]] = []
    seen: set[int] = set()
    for piece in (text or "").split(","):
        left, _, right = normalize_digits(piece).partition(":")
        left, right = left.strip(), right.strip()
        if left.isdigit() and right.isdigit() and int(left) > 0 and int(left) not in seen:
            seen.add(int(left))
            tiers.append({"threshold": int(left), "amount": int(right)})
    return sorted(tiers, key=lambda tier: tier["threshold"])[:8]
