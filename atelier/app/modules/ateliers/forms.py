"""Form helpers of the atelier panel: pay terms, role presets, small parsers.

The pay terms editor (base + commission per photo + bonus tiers) appears on four pages
(atelier presets, roster defaults, the afish form, the terms editor of one assignment), so
parsing and redisplay live here once.  Field names, with ``suffix`` ``""`` or ``"__photographer"``:

* ``base<suffix>``                    money (Toman)
* ``commission<suffix>``              money (Toman per sold photo unit)
* ``tier_threshold<suffix>[]``        repeated, integer "N photos and above"
* ``tier_amount<suffix>[]``           repeated, money

Rows where both tier fields are blank are ignored (the editor always renders a spare row).
Only the *shape* is validated here; the arithmetic of pay is ``app.services.payroll``.
"""

from __future__ import annotations

import re
from itertools import zip_longest
from typing import Any, Mapping, Optional

from app.services.constants import ROLE_DISPLAY_ORDER, Skill
from app.services.forms import Form, clean_text
from app.services.money import MAX_TOMAN, normalize_digits, parse_toman

MAX_TIERS = 8
MAX_TIER_THRESHOLD = 100_000

ROLE_SUFFIX = {role.value: f"__{role.value}" for role in ROLE_DISPLAY_ORDER}


def role_suffix(role: Any) -> str:
    return ROLE_SUFFIX[Skill.coerce(role).value]


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _raw_list(form: Form, name: str) -> list[str]:
    getter = getattr(form.data, "getlist", None)
    if getter is None:
        value = form.data.get(name) or []
        return [value] if isinstance(value, str) else list(value)
    return list(getter(name))


def read_tiers(form: Form, suffix: str = "", prefix: str = "") -> Optional[list[dict[str, int]]]:
    """Bonus tiers from the repeated rows; ``None`` (and an error) when a row is invalid.

    ``form.values['tiers<suffix>']`` receives the raw rows so a failed submit shows what was typed.
    """
    key = f"tiers{suffix}"
    thresholds = _raw_list(form, f"tier_threshold{suffix}[]")
    amounts = _raw_list(form, f"tier_amount{suffix}[]")
    rows: list[dict[str, str]] = []
    tiers: list[dict[str, int]] = []
    problem: Optional[str] = None
    seen: set[int] = set()
    for raw_threshold, raw_amount in zip_longest(thresholds, amounts, fillvalue=""):
        threshold_text = normalize_digits(clean_text(raw_threshold)).replace(",", "")
        amount_text = clean_text(raw_amount)
        if not threshold_text and not amount_text:
            continue
        rows.append({"threshold": threshold_text, "amount": amount_text})
        if not re.fullmatch(r"\d{1,6}", threshold_text) or int(threshold_text) < 1 or int(threshold_text) > MAX_TIER_THRESHOLD:
            problem = problem or f"{prefix}در هر پلهٔ پاداش، تعداد عکس را به‌صورت عدد صحیح (حداقل ۱) وارد کنید."
            continue
        try:
            amount = parse_toman(amount_text, allow_blank=False)
        except ValueError:
            problem = problem or f"{prefix}مبلغ هر پلهٔ پاداش را به تومان و به‌صورت عدد وارد کنید."
            continue
        threshold = int(threshold_text)
        if threshold in seen:
            problem = problem or f"{prefix}دو پلهٔ پاداش نباید تعداد عکس یکسان داشته باشند."
            continue
        seen.add(threshold)
        tiers.append({"threshold": threshold, "amount": amount})
    form.values[key] = rows
    if len(rows) > MAX_TIERS:
        problem = problem or f"{prefix}حداکثر {MAX_TIERS} پلهٔ پاداش مجاز است."
    if problem:
        form.error(key, problem)
        return None
    tiers.sort(key=lambda tier: tier["threshold"])
    return tiers


def read_terms(form: Form, suffix: str = "", label: str = "") -> Optional[dict[str, Any]]:
    """The terms dict ``{base, commission_per_unit, bonus_tiers}`` or ``None`` with errors recorded."""
    prefix = f"{label}: " if label else ""
    base = form.money(f"base{suffix}", f"{prefix}حقوق پایه", default=0)
    commission = form.money(f"commission{suffix}", f"{prefix}پورسانت هر عکس", default=0)
    tiers = read_tiers(form, suffix, prefix)
    # Form.money returns its default (0) after recording an error, so look at the errors, not the values.
    if tiers is None or any(key in form.errors for key in (f"base{suffix}", f"commission{suffix}")):
        return None
    return {"base": base or 0, "commission_per_unit": commission or 0, "bonus_tiers": tiers}


def terms_are_empty(terms: Optional[Mapping[str, Any]]) -> bool:
    """True for ``None`` or terms that promise nothing (no base, no commission, no tiers)."""
    if not terms:
        return True
    return not (terms.get("base") or terms.get("commission_per_unit") or terms.get("bonus_tiers"))


# --------------------------------------------------------------------------
# redisplay
# --------------------------------------------------------------------------


def terms_to_form(terms: Optional[Mapping[str, Any]], suffix: str = "", *, blank_zero: bool = False) -> dict[str, Any]:
    """Form values (the keys the templates read) for a terms dict."""
    terms = terms or {}
    base = terms.get("base") or 0
    commission = terms.get("commission_per_unit") or 0
    return {
        f"base{suffix}": ("" if blank_zero and not base else base),
        f"commission{suffix}": ("" if blank_zero and not commission else commission),
        f"tiers{suffix}": [
            {"threshold": tier["threshold"], "amount": tier["amount"]}
            for tier in (terms.get("bonus_tiers") or [])
        ],
    }


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------


def read_id(form: Form, name: str) -> Optional[int]:
    """A positive integer id from a form field (``None`` when absent or junk)."""
    text = normalize_digits(form.raw(name))
    return int(text) if re.fullmatch(r"\d{1,12}", text) and int(text) > 0 else None


def read_skills(form: Form, name: str = "skills", label: str = "مهارت") -> list[str]:
    """Checked skills (``name`` repeated), validated against the Skill enum, canonical order."""
    chosen: set[str] = set()
    for text in form.getlist(name):
        if not Skill.is_valid(text):
            form.error(name, f"{label} نامعتبر است.")
            continue
        chosen.add(Skill.coerce(text).value)
    return [role.value for role in Skill if role.value in chosen]


def money_or_none(form: Form, name: str, label: str) -> Optional[int]:
    return form.money(name, label, min=0, max=MAX_TOMAN)
