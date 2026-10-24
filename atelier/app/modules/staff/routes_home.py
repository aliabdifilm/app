"""``/me`` - the dashboard and the read-only list of my marketplace applications."""

from __future__ import annotations

import datetime as dt
from typing import Any

from flask import render_template

from app.modules.staff import availability, bp, common, earnings, paytext, profile as profile_logic, queries
from app.modules.staff.web import optional_url
from app.services import jalali, security
from app.services.constants import Role
from app.services.http import paginate
from app.services.permissions import get_user, role_required

STAFF = Role.STAFF

GENERIC_TIPS = (
    "شب قبل از مراسم باتری‌ها را شارژ و کارت‌های حافظه را خالی کنید.",
    "نیم ساعت زودتر از ساعت حضور در تالار باشید تا تجهیزات با آرامش چیده شود.",
    "پیش از ترک مراسم، فایل‌ها را با فروشنده و ادیتور یک بار بررسی کنید.",
    "اگر اتفاقی برایتان افتاد، هرچه زودتر از طریق صفحهٔ افیش به اتلیه خبر دهید.",
)


def build_tips(
    user: Any, profile: Any, completeness: profile_logic.Completeness, unpaid: int, marked_days: int, today: dt.date
) -> list[dict[str, str]]:
    """Up to three short, contextual suggestions (what would help this person most right now)."""
    tips: list[dict[str, str]] = []
    keys = {step.key for step in completeness.missing}
    if "skills" in keys:
        tips.append({"icon": "user-check", "text": "توانایی‌هایتان را مشخص کنید تا دعوت‌ها با کار شما جور باشد.", "endpoint": "staff.profile", "label": "تکمیل پروفایل"})
    if profile is None or not profile.is_listed:
        tips.append({"icon": "search", "text": "با روشن‌کردن «نمایش در جستجو»، اتلیه‌ها می‌توانند شما را پیدا و دعوت کنند.", "endpoint": "staff.profile", "label": "تنظیم نمایش"})
    if "portfolio" in keys:
        tips.append({"icon": "image", "text": "چند نمونه‌کار اضافه کنید؛ اتلیه‌ها با دیدن کار شما راحت‌تر تصمیم می‌گیرند.", "endpoint": "staff.portfolio", "label": "افزودن نمونه‌کار"})
    if marked_days == 0:
        tips.append({"icon": "calendar-check", "text": "روزهایی را که آزاد نیستید علامت بزنید تا دعوت‌های هم‌زمان نگیرید.", "endpoint": "staff.availability_page", "label": "ثبت روزهای مشغول"})
    if unpaid > 0:
        tips.append({"icon": "coins", "text": f"{paytext.money(unpaid)} تومان از حقوق شما هنوز پرداخت نشده است.", "endpoint": "staff.earnings", "label": "دیدن جزئیات"})
    tips.append({"icon": "sparkles", "text": GENERIC_TIPS[today.toordinal() % len(GENERIC_TIPS)], "endpoint": "", "label": ""})
    return tips[:3]


@bp.get("/")
@role_required(STAFF)
def dashboard():
    user = get_user()
    today = security.today()
    jy, jm, _ = jalali.gregorian_to_jalali(today.year, today.month, today.day)
    first, last = jalali.jalali_month_range(jy, jm)

    next_row = queries.next_assignment(user.id, today)
    upcoming = [row for row in queries.upcoming_committed(user.id, today, limit=4) if next_row is None or row.id != next_row.id][:3]
    pending = queries.pending_invitation_count(user.id, today)
    month_totals = earnings.totals(user.id, first, last)
    unpaid = earnings.outstanding(user.id)
    profile = queries.get_profile(user.id)
    portfolio = queries.portfolio_count(user.id)
    completeness = profile_logic.completeness(user, profile, portfolio)
    upcoming_count, done_count = queries.committed_counts(user.id, today)
    marked = len(availability.marks_between(user.id, today, today + dt.timedelta(days=60)))

    next_example = None
    if next_row is not None:
        next_example = paytext.example(next_row.terms_dict())
    return render_template(
        "staff/dashboard.html",
        next_row=next_row,
        next_terms=paytext.terms_lines(next_row.terms_dict()) if next_row is not None else [],
        next_example=next_example,
        next_venue=common.venue_visible(next_row) if next_row is not None else False,
        upcoming=upcoming,
        pending=pending,
        month_title=common.month_title(jy, jm),
        month_totals=month_totals,
        unpaid=unpaid,
        completeness=completeness,
        upcoming_count=upcoming_count,
        done_count=done_count,
        tips=build_tips(user, profile, completeness, unpaid, marked, today),
        profile=profile,
    )


@bp.get("/applications")
@role_required(STAFF)
def applications():
    """Read-only list of my applications; managing them (withdraw, contact) happens in the marketplace."""
    user = get_user()
    page = paginate(queries.applications_select(user.id), per_page=10)
    links = {}
    for application in page.items:
        links[application.id] = optional_url("marketplace.job_detail", post_id=application.post_id)
    return render_template(
        "staff/applications.html",
        page=page,
        links=links,
        manage_url=optional_url("marketplace.my_applications"),
        jobs_url=optional_url("marketplace.jobs"),
        today=security.today(),
    )
