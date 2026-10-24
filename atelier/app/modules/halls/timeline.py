"""The history of an event for its hall manager: audit rows turned into short Persian sentences.

Only facts about the date and its offers are shown (who was asked, who answered); the names are
those of ateliers, never of their staff, and nothing about money.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional, Sequence

from app.models import AuditLog, Event, EventOffer
from app.services import jalali
from app.services.constants import EventStatus
from app.services.money import to_persian_digits

_STATUS_TEXT = {
    EventStatus.CONFIRMED.value: ("مراسم قطعی شد", "success"),
    EventStatus.CANCELLED.value: ("مراسم لغو شد", "danger"),
    EventStatus.DONE.value: ("مراسم برگزار شد", "accent"),
    EventStatus.INVOICED.value: ("برای مراسم فاکتور ثبت شد", "neutral"),
    EventStatus.DRAFT.value: ("مراسم به پیش‌نویس برگشت", "neutral"),
    EventStatus.OPEN.value: ("مراسم دوباره آزاد شد", "info"),
    EventStatus.OFFERED.value: ("مراسم پیشنهاد شد", "info"),
}
_CHANGE_LABELS = {
    "date": "تاریخ",
    "start_time": "ساعت",
    "ceremony_type": "نوع مراسم",
    "guest_count_estimate": "تعداد میهمان",
    "title": "عنوان",
    "notes": "توضیحات",
}


@dataclass(frozen=True)
class Entry:
    when: dt.datetime
    text: str
    tone: str = "neutral"


def describe(rows: Sequence[AuditLog], offers: Sequence[EventOffer], viewer_id: int) -> list[Entry]:
    """Turn audit rows into :class:`Entry` objects (newest first, as given)."""
    names = {offer.id: offer.atelier.name for offer in offers}
    entries: list[Entry] = []
    for row in rows:
        data = row.data or {}
        who = "شما" if row.actor_id == viewer_id else None
        atelier = names.get(row.entity_id) if row.entity_type == EventOffer.__tablename__ else None
        atelier = atelier or "اتلیه"
        action = row.action
        entry: Optional[tuple[str, str]] = None
        if action == "event.created":
            entry = ("مراسم ثبت شد", "neutral")
        elif action == "event.updated":
            changed = "، ".join(_CHANGE_LABELS.get(name, name) for name in (data.get("changes") or {}))
            entry = (f"مشخصات مراسم ویرایش شد ({changed})" if changed else "مشخصات مراسم ویرایش شد", "neutral")
        elif action == "event.opened":
            entry = ("مراسم برای همهٔ اتلیه‌ها باز شد", "info")
        elif action == "event.open_closed":
            entry = ("انتشار عمومی مراسم متوقف شد", "neutral")
        elif action == "offer.created":
            entry = (f"پیشنهاد به «{atelier}» فرستاده شد", "info")
        elif action == "offer.reoffered":
            entry = (f"پیشنهاد دوباره به «{atelier}» فرستاده شد", "info")
        elif action == "offer.withdrawn":
            entry = (f"پیشنهاد «{atelier}» پس گرفته شد", "neutral")
        elif action == "offer.declined":
            entry = (f"«{atelier}» پیشنهاد را رد کرد", "danger")
        elif action == "offer.accepted":
            entry = (f"«{atelier}» پیشنهاد را پذیرفت", "success")
        elif action == "offer.claimed":
            entry = (f"«{atelier}» مراسم را از فهرست باز برداشت", "success")
        elif action == "offer.released":
            entry = (f"«{atelier}» دیگر مراسم را ندارد", "warning")
        elif action.startswith("event."):
            entry = _STATUS_TEXT.get(action.split(".", 1)[1])
        if entry is None:
            continue
        text = entry[0]
        if who:
            text = f"{text} · {who}"
        entries.append(Entry(row.created_at, text, entry[1]))
    return entries


def stamp(value: dt.datetime) -> str:
    """Jalali date and Iran time of an audit timestamp (Persian digits)."""
    return jalali.to_jalali_datetime_str(value, "short", persian_digits=True)


def share_text(event: Event) -> str:
    """Plain text with the facts an atelier team needs (date, hall, address, map, set-up notes) for a messenger."""
    hall = event.hall
    lines = [f"مراسم {jalali.to_jalali_str(event.date, 'weekday_long', persian_digits=True)}"]
    if event.start_time:
        lines[0] += f" · ساعت {to_persian_digits(event.start_time.strftime('%H:%M'))}"
    lines.append(f"{hall.name}{' - ' + hall.city if hall.city else ''}")
    if hall.address:
        lines.append(f"نشانی: {hall.address}")
    if hall.map_url:
        lines.append(f"نقشه: {hall.map_url}")
    labels = {
        "loading": "بارگیری",
        "power": "برق",
        "wifi": "وای‌فای",
        "parking": "پارکینگ",
        "setup_spot": "محل استقرار",
        "contact_name": "هماهنگی",
        "contact_phone": "تلفن هماهنگی",
    }
    for key, label in labels.items():
        value = (hall.logistics or {}).get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
