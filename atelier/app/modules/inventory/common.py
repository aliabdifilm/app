"""Helpers shared by the route modules of the inventory panel."""

from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from flask import request, session, url_for

from app.models import Atelier, StockItem
from app.services.constants import Role, format_size
from app.services.consumption import format_quantity, stock_value
from app.services.permissions import get_my_atelier

MANAGER = Role.ATELIER_MANAGER

# (key, label, endpoint) of the sections shown as tabs on every page of the module
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("overview", "نمای کلی", "inventory.index"),
    ("stock", "موجودی", "inventory.items"),
    ("calculator", "محاسبهٔ چاپ", "inventory.calculator"),
    ("prepare", "آمادگی مراسم", "inventory.prepare_pick"),
    ("equipment", "تجهیزات", "inventory.equipment_list"),
    ("consumption", "مصرف خودکار", "inventory.consumption_list"),
    ("settings", "تنظیمات", "inventory.settings"),
)


def current_atelier() -> Atelier:
    """The atelier the manager is working on (``OnboardingRequired`` -> the setup page)."""
    return get_my_atelier()


def tab_items() -> list[dict[str, Any]]:
    """Items for the ``tabs`` macro: ``{{ tabs(inv_tabs(), active='stock') }}``."""
    return [{"key": key, "label": label, "url": url_for(endpoint)} for key, label, endpoint in SECTIONS]


# --------------------------------------------------------------------------
# one-time form tokens (a double tap or a retry on bad hall Wi-Fi must not book a movement twice)
# --------------------------------------------------------------------------

IDEM_FIELD = "_idem"
_SESSION_KEY = "inv_idem"
_REMEMBER = 24
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


def new_token() -> str:
    """A fresh token for a form (``<input type="hidden" name="_idem" value="{{ inv_idem() }}">``)."""
    return secrets.token_urlsafe(9)


def replayed() -> bool:
    """True when the posted token was already used successfully in this browser session.

    A form without a token (a script, an old tab) is never treated as a replay.  The token is only
    remembered by :func:`remember` after the work was committed, so a rejected (422) form can be
    corrected and sent again.
    """
    token = request.form.get(IDEM_FIELD, "")
    return bool(_TOKEN_RE.match(token)) and token in (session.get(_SESSION_KEY) or [])


def remember() -> None:
    """Record the posted token as used (call after the commit)."""
    token = request.form.get(IDEM_FIELD, "")
    if not _TOKEN_RE.match(token):
        return
    used = list(session.get(_SESSION_KEY) or [])
    if token not in used:
        used.append(token)
    session[_SESSION_KEY] = used[-_REMEMBER:]


REPLAY_MESSAGE = "این فرم قبلاً ثبت شده بود؛ دوباره ثبت نشد."


# --------------------------------------------------------------------------
# text for templates
# --------------------------------------------------------------------------


def qty_text(item: StockItem, quantity: Optional[int] = None) -> str:
    """Persian text of a stock quantity (``۶ بسته و ۱۰ برگ``, ``۱۲ عدد``)."""
    return format_quantity(item, quantity)


def value_of(item: StockItem, quantity: Optional[int] = None) -> int:
    return stock_value(item, quantity)


def size_text(size: Optional[str]) -> str:
    """``'16x21'`` -> ``'16×21'`` (pipe through ``fa`` for Persian digits)."""
    return format_size(size or "")


def item_tone(item: StockItem) -> str:
    """Badge tone of an item: inactive -> neutral, out of stock -> danger, low -> warning, else success."""
    if not item.is_active:
        return "neutral"
    if (item.quantity or 0) <= 0:
        return "danger"
    return "warning" if item.is_low else "success"


def item_state_text(item: StockItem) -> str:
    if not item.is_active:
        return "غیرفعال"
    if (item.quantity or 0) < 0:
        return "منفی (فروش بیش از موجودی)"
    if (item.quantity or 0) == 0:
        return "تمام شده"
    return "کم است" if item.is_low else "کافی"


def packs_of(item: StockItem, quantity: Optional[int] = None) -> Optional[int]:
    """Whole packs in *quantity* sheets of a paper item (``None`` for other kinds)."""
    if not item.counts_in_sheets:
        return None
    qty = item.quantity if quantity is None else quantity
    return max(int(qty or 0), 0) // int(item.sheets_per_pack)
