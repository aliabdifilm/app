"""Notification preferences of a staff member.

The bell and the future SMS layer have *essential* kinds that cannot be switched off (invitations, afish changes,
answers to them, account notices: ``notifications.ESSENTIAL_KINDS``).  Everything else belongs to a group below;
the person turns whole groups on or off.  The choice is stored as ``User.notification_prefs["muted"]`` (a list of
kinds) and enforced by ``notifications.notify`` itself, so it applies to every module that notifies.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.models import User
from app.services import notifications

#: (key, title, help, kinds) - the switchable groups, in display order.
GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "reminders",
        "title": "یادآوری روز مراسم",
        "help": "یک روز مانده به مراسم و صبح روز مراسم.",
        "kinds": ("reminder_day_before", "reminder_same_day"),
    },
    {
        "key": "pay",
        "title": "ثبت و پرداخت حقوق",
        "help": "وقتی فاکتور مراسم ثبت یا حقوق شما پرداخت می‌شود.",
        "kinds": ("invoice_submitted", "payroll_paid"),
    },
    {
        "key": "reviews",
        "title": "نظرهای دریافتی",
        "help": "وقتی اتلیه برای همکاری با شما نظر می‌گذارد.",
        "kinds": ("review_received",),
    },
    {
        "key": "jobs",
        "title": "درخواست‌ها در بازار کار",
        "help": "نتیجهٔ درخواست‌هایی که برای آگهی‌ها فرستاده‌اید.",
        "kinds": ("application_decided", "application_received"),
    },
    {
        "key": "system",
        "title": "اطلاعیه‌های سامانه",
        "help": "خبرها و پیام‌های مدیران سامانه.",
        "kinds": ("system",),
    },
)
ALWAYS_ON = "دعوت‌ها، تغییر افیش، تغییر وضعیت مراسم و اعلان‌های حساب کاربری همیشه فعال می‌مانند."


def _muted(user: User) -> set[str]:
    prefs = user.notification_prefs if isinstance(user.notification_prefs, dict) else {}
    muted = prefs.get("muted")
    return {str(kind) for kind in muted} if isinstance(muted, (list, tuple)) else set()


def enabled_groups(user: User) -> set[str]:
    """Keys of the groups the user still receives (a group is off when all its kinds are muted)."""
    muted = _muted(user)
    return {group["key"] for group in GROUPS if not set(group["kinds"]) <= muted}


def save(user: User, enabled: Iterable[str]) -> list[str]:
    """Store the choice; returns the muted kinds.  Essential kinds can never end up muted."""
    wanted = set(enabled)
    muted: list[str] = []
    for group in GROUPS:
        if group["key"] not in wanted:
            muted.extend(kind for kind in group["kinds"] if kind not in notifications.ESSENTIAL_KINDS)
    # keep entries of kinds that belong to no group (set elsewhere) untouched
    known = {kind for group in GROUPS for kind in group["kinds"]}
    muted.extend(sorted(kind for kind in _muted(user) if kind not in known and kind not in notifications.ESSENTIAL_KINDS))
    prefs = dict(user.notification_prefs) if isinstance(user.notification_prefs, dict) else {}
    prefs["muted"] = sorted(set(muted))
    user.notification_prefs = prefs  # assign a new object: JSON columns are not change-tracked
    return prefs["muted"]
