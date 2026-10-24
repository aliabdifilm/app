"""Small helpers shared by the route modules of the atelier panel."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from flask import flash, redirect, url_for
from flask_sqlalchemy.pagination import Pagination
from werkzeug.exceptions import TooManyRequests
from werkzeug.wrappers import Response

from app.models import Atelier, Event
from app.modules.ateliers import board as boards
from app.services import security, staffing
from app.services.constants import AssignmentStatus, EventStatus, Role
from app.services.money import format_toman, to_persian_digits
from app.services.permissions import event_or_404, get_my_atelier

MANAGER = Role.ATELIER_MANAGER


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def current_atelier() -> Atelier:
    """The atelier the manager is working on (OnboardingRequired -> the setup page)."""
    return get_my_atelier()


def my_event_or_404(event_id: Any) -> Event:
    """An event worked by one of my ateliers (404 for anybody else's, same answer as for a missing id)."""
    return event_or_404(event_id, as_=("atelier",))


def event_label(event: Event) -> str:
    hall = event.hall.name if event.hall is not None else ""
    return hall or (event.title or "مراسم")


# --------------------------------------------------------------------------
# text for templates
# --------------------------------------------------------------------------


def terms_text(terms: Optional[dict]) -> str:
    """One Persian line describing pay terms: base + commission per photo + bonus tiers."""
    if not terms:
        return "بدون مبلغ تعیین‌شده"
    parts: list[str] = []
    base = terms.get("base") or 0
    commission = terms.get("commission_per_unit") or 0
    if base:
        parts.append(f"پایه {format_toman(base, persian_digits=True)} تومان")
    if commission:
        parts.append(f"پورسانت هر عکس {format_toman(commission, persian_digits=True)} تومان")
    for tier in terms.get("bonus_tiers") or []:
        parts.append(
            f"پاداش {to_persian_digits(tier['threshold'])} عکس به بالا "
            f"{format_toman(tier['amount'], persian_digits=True)} تومان"
        )
    return " · ".join(parts) if parts else "بدون مبلغ تعیین‌شده"


def hhmm(value: Optional[dt.time]) -> str:
    """``dt.time(18, 30)`` -> ``'۱۸:۳۰'`` (empty for None)."""
    return to_persian_digits(value.strftime("%H:%M")) if value else ""


def input_time(value: Optional[dt.time]) -> str:
    """Value for a text time field (Latin digits, ``HH:MM``)."""
    return value.strftime("%H:%M") if value else ""


# status -> badge tone, used by every template
ASSIGNMENT_TONES = {
    AssignmentStatus.INVITED: "warning",
    AssignmentStatus.ACCEPTED: "info",
    AssignmentStatus.CONFIRMED: "success",
    AssignmentStatus.COMPLETED: "neutral",
    AssignmentStatus.DECLINED: "danger",
    AssignmentStatus.CANCELLED: "neutral",
}
EVENT_TONES = {
    EventStatus.DRAFT: "neutral",
    EventStatus.OPEN: "info",
    EventStatus.OFFERED: "info",
    EventStatus.CONFIRMED: "success",
    EventStatus.DONE: "accent",
    EventStatus.INVOICED: "neutral",
    EventStatus.CANCELLED: "danger",
}


def assignment_tone(status: Any) -> str:
    return ASSIGNMENT_TONES.get(AssignmentStatus.coerce(status), "neutral")


def event_tone(status: Any) -> str:
    return EVENT_TONES.get(EventStatus.coerce(status), "neutral")


# --------------------------------------------------------------------------
# errors and redirects
# --------------------------------------------------------------------------


def describe_staffing_error(exc: Exception) -> str:
    """Persian message for an exception raised by ``app.services.staffing``."""
    if isinstance(exc, staffing.TermsLocked):
        return "شرایط این افیش پس از ثبت فاکتور قفل شده است؛ ابتدا فاکتور را بازگشایی کنید."
    if isinstance(exc, staffing.DuplicateAssignment):
        return "این نفر همین نقش را از قبل در این مراسم دارد."
    if isinstance(exc, staffing.AssignmentConflict):
        return "این نفر در همان روز در مراسم دیگری افیش شده است."
    if isinstance(exc, staffing.EventNotStaffable):
        return "این مراسم دیگر قابل افیش‌کردن نیست."
    if isinstance(exc, staffing.InvalidTransition):
        return "این تغییر وضعیت در حالت فعلی مجاز نیست."
    if isinstance(exc, staffing.StaffingPermissionError):
        return "اجازهٔ انجام این کار را ندارید."
    if isinstance(exc, staffing.StaffingError):
        return "عملیات افیش انجام نشد؛ اطلاعات را بررسی کنید."
    return "عملیات انجام نشد."


LOOKUP_LIMIT = 40  # exact-mobile lookups per manager and hour
LOOKUP_WINDOW = 3600


def spend_lookup_budget() -> None:
    """Count one lookup of a person by mobile number (directory search, roster linking).

    Finding or linking an account by number reveals that the number is registered, so the number of
    probes per manager is limited (in-process limiter, like password sign-in).  429 when exhausted.
    """
    from app.services.permissions import get_user

    manager = get_user()
    key = f"ateliers:lookup:{manager.id if manager is not None else security.client_ip()}"
    decision = security.get_rate_limiter().hit(key, LOOKUP_LIMIT, LOOKUP_WINDOW)
    if not decision:
        raise TooManyRequests(
            "تعداد جستجوی شمارهٔ موبایل در این ساعت به حد مجاز رسیده است؛ کمی بعد دوباره تلاش کنید.",
            retry_after=decision.retry_after,
        )


def to_event(event_id: int, anchor: Optional[str] = "afish", **values: Any) -> Response:
    return redirect(url_for("ateliers.event_detail", event_id=event_id, _anchor=anchor, **values))


def fail(message: str, endpoint: str, **values: Any) -> Response:
    """Flash an error and redirect (for POST actions without a form of their own)."""
    flash(message, "error")
    return redirect(url_for(endpoint, **values))


class ListPagination(Pagination):
    """Pagination over a Python list, for filters that cannot run in SQL (JSON list columns).

    Same attributes as the Flask-SQLAlchemy object, so the ``pagination`` macro renders it.
    """

    def _query_items(self) -> list:
        rows = self._query_args["rows"]
        start = (self.page - 1) * self.per_page
        return rows[start : start + self.per_page]

    def _query_count(self) -> int:
        return len(self._query_args["rows"])


def paginate_list(rows: list, *, per_page: int = 20) -> ListPagination:
    from app.services.http import MAX_PER_PAGE, page_args

    page, size = page_args(per_page, MAX_PER_PAGE)
    return ListPagination(page=page, per_page=size, max_per_page=MAX_PER_PAGE, error_out=False, count=True, rows=rows)


# Re-exported for the route modules.
load_board = boards.load_board
