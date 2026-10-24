"""Small helpers shared by the route modules of the hall panel (no business rules here, see ``rules``)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from flask import abort, flash, redirect, request, url_for
from werkzeug.wrappers import Response

from app.models import Hall
from app.modules.halls import rules
from app.services import event_offers, jalali, staffing
from app.services.constants import EventStatus, OfferMode, OfferStatus, Role
from app.services.money import to_persian_digits
from app.services.permissions import OnboardingRequired, get_my_halls

MANAGER = Role.HALL_MANAGER

#: Jalali years offered by the calendar and the reports.
YEAR_RANGE = (1300, 1600)


# --------------------------------------------------------------------------
# which halls am I working with?
# --------------------------------------------------------------------------


def my_halls() -> list[Hall]:
    """All halls of the signed-in manager (ordered by name); none yet -> the "create your hall" page."""
    halls = get_my_halls()
    if not halls:
        raise OnboardingRequired(MANAGER, "hall")
    return halls


def hall_filter(halls: list[Hall], name: str = "hall") -> tuple[Optional[Hall], list[int]]:
    """``?hall=<id>`` -> the chosen hall (404 when it is not mine) and the hall ids the page covers.

    No / ``all`` / empty parameter means every hall of the manager.
    """
    raw = request.args.get(name, "").strip()
    if raw and raw != "all":
        if not raw.isdigit():
            abort(404)
        for hall in halls:
            if hall.id == int(raw):
                return hall, [hall.id]
        abort(404)
    return None, [hall.id for hall in halls]


def hall_options(halls: list[Hall]) -> list[tuple[int, str]]:
    return [(hall.id, hall.name) for hall in halls]


# --------------------------------------------------------------------------
# text for templates
# --------------------------------------------------------------------------


def hhmm(value: Optional[dt.time]) -> str:
    """``dt.time(18, 30)`` -> ``'۱۸:۳۰'`` (empty for None)."""
    return to_persian_digits(value.strftime("%H:%M")) if value else ""


def input_time(value: Optional[dt.time]) -> str:
    """Value of a ``<input type="time">`` (Latin digits, ``HH:MM``)."""
    return value.strftime("%H:%M") if value else ""


EVENT_TONES = {
    EventStatus.DRAFT: "neutral",
    EventStatus.OPEN: "info",
    EventStatus.OFFERED: "info",
    EventStatus.CONFIRMED: "success",
    EventStatus.DONE: "accent",
    EventStatus.INVOICED: "neutral",
    EventStatus.CANCELLED: "danger",
}
OFFER_TONES = {
    OfferStatus.PENDING: "warning",
    OfferStatus.ACCEPTED: "success",
    OfferStatus.DECLINED: "danger",
    OfferStatus.WITHDRAWN: "neutral",
}


def event_tone(status: Any) -> str:
    return EVENT_TONES.get(EventStatus.coerce(status), "neutral")


def offer_tone(status: Any) -> str:
    return OFFER_TONES.get(OfferStatus.coerce(status), "neutral")


def state_of(event: Any, pending: int = 0) -> tuple[str, str]:
    """``(label, tone)`` of an event from the hall's point of view.

    ``pending`` is the number of direct offers waiting for an answer.
    """
    status = event.status
    if status == EventStatus.OFFERED:
        if pending:
            return f"پیشنهاد به {to_persian_digits(pending)} اتلیه", "info"
        return "بدون پاسخ‌دهنده", "warning"
    if status == EventStatus.OPEN:
        if event.offer_mode == OfferMode.OPEN:
            return "باز برای همهٔ اتلیه‌ها", "info"
        return "بدون اتلیه", "warning"
    if status == EventStatus.CONFIRMED and event.atelier is not None:
        return f"قطعی · {event.atelier.name}", "success"
    return status.label, event_tone(status)


def month_title(jy: int, jm: int) -> str:
    return to_persian_digits(f"{jalali.JALALI_MONTHS[jm - 1]} {jy}")


def jalali_month_arg(today: dt.date) -> tuple[int, int]:
    """``?y=1404&m=5`` -> (year, month); anything missing or out of range means the current month."""
    jy, jm, _ = jalali.to_jalali(today)
    year, month = request.args.get("y", type=int), request.args.get("m", type=int)
    if year and month and YEAR_RANGE[0] <= year <= YEAR_RANGE[1] and 1 <= month <= 12:
        return year, month
    return jy, jm


def year_choices(years) -> list[tuple[int, str]]:
    """``[(1404, '۱۴۰۴'), ...]`` for a <select> of Jalali years."""
    return [(year, to_persian_digits(year)) for year in years]


def shift_month(jy: int, jm: int, delta: int) -> tuple[int, int]:
    index = jy * 12 + (jm - 1) + delta
    return index // 12, index % 12 + 1


# --------------------------------------------------------------------------
# redirects
# --------------------------------------------------------------------------


def fail(message: str, endpoint: str, **values: Any) -> Response:
    """Flash an error and redirect (for POST actions without a form of their own)."""
    flash(message, "error")
    return redirect(url_for(endpoint, **values))


def ok(message: str, endpoint: str, **values: Any) -> Response:
    flash(message, "success")
    return redirect(url_for(endpoint, **values))


# --------------------------------------------------------------------------
# errors of the services
# --------------------------------------------------------------------------

STAFFING_MESSAGES: dict[type, str] = {
    staffing.TermsLocked: "شرایط افیش این مراسم قفل شده است؛ ابتدا فاکتور آن را بازگشایی کنید.",
    staffing.EventNotStaffable: "این مراسم در وضعیت فعلی قابل تغییر نیست.",
    staffing.InvalidTransition: "این تغییر وضعیت در حالت فعلی مجاز نیست.",
    staffing.StaffingPermissionError: "اجازهٔ انجام این کار را ندارید.",
}

#: Exceptions a route turns into a flash message / form error (everything else is a bug and stays a 500).
ERRORS = (rules.RuleError, event_offers.OfferError, staffing.StaffingError)


def describe_error(exc: Exception) -> str:
    """Persian message for the exceptions the services raise (never the exception text of a bug)."""
    if isinstance(exc, (rules.RuleError, event_offers.OfferError)):
        return str(exc)
    for kind, message in STAFFING_MESSAGES.items():
        if isinstance(exc, kind):
            return message
    return "عملیات انجام نشد؛ اطلاعات را بررسی کنید."
