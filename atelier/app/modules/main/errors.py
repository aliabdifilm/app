"""Error handlers: friendly Persian pages for browsers, ``{ok: false, error}`` for JSON callers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from flask import Flask, flash, g, redirect, render_template
from flask_wtf.csrf import CSRFError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.exceptions import HTTPException

from app.extensions import db
from app.services import nav
from app.services.http import json_error, wants_json
from app.services.permissions import OnboardingRequired, get_user

logger = logging.getLogger(__name__)

MESSAGES: dict[int, str] = {
    400: "درخواست نامعتبر است.",
    401: "برای ادامه وارد حساب خود شوید.",
    403: "دسترسی شما به این بخش مجاز نیست.",
    404: "مورد درخواستی پیدا نشد.",
    405: "این عملیات مجاز نیست.",
    408: "زمان درخواست به پایان رسید؛ دوباره تلاش کنید.",
    409: "این مورد هم‌زمان تغییر کرده است؛ صفحه را تازه کنید و دوباره تلاش کنید.",
    413: "حجم فایل یا اطلاعات ارسالی زیاد است.",
    415: "نوع داده ارسالی پشتیبانی نمی‌شود.",
    422: "اطلاعات واردشده معتبر نیست.",
    429: "تعداد درخواست‌ها زیاد است؛ کمی صبر کنید و دوباره تلاش کنید.",
    500: "خطایی در سرور رخ داد؛ کمی بعد دوباره تلاش کنید.",
    503: "سرویس موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید.",
}
TITLES: dict[int, tuple[str, str]] = {
    403: ("دسترسی ندارید", "lock"),
    404: ("صفحه پیدا نشد", "search"),
    400: ("درخواست نامعتبر", "alert"),
    401: ("ورود لازم است", "lock"),
    405: ("این عملیات مجاز نیست", "alert"),
    408: ("زمان درخواست تمام شد", "clock"),
    409: ("تغییر هم‌زمان", "alert"),
    413: ("فایل یا اطلاعات بیش از حد بزرگ است", "upload"),
    415: ("نوع داده پشتیبانی نمی‌شود", "alert"),
    422: ("اطلاعات نامعتبر", "alert"),
    429: ("درخواست‌های زیاد", "clock"),
    503: ("سرویس در دسترس نیست", "alert"),
}
CSRF_MESSAGE = "اعتبار این فرم تمام شده است. صفحه را دوباره باز کنید و فرم را دوباره بفرستید."


def _custom_message(exc: Optional[HTTPException]) -> Optional[str]:
    """The text of ``abort(403, "پیام")``, or None for the stock werkzeug description."""
    description = getattr(exc, "description", None) if exc is not None else None
    if description and description != type(exc).description:
        return str(description)
    return None


def _render(code: int, message: str, retry_after: Optional[int] = None, custom: bool = False):
    """Render the error as JSON or as one of the error pages, with status *code*.

    *custom* marks a message chosen by the view (``abort(404, "تالار پیدا نشد")``): it is shown
    on the generic page instead of the stock 403 / 404 wording.
    """
    if wants_json():
        extra: dict[str, Any] = {"retry_after": retry_after} if retry_after else {}
        response = json_error(message, code, **extra)
    else:
        if code == 403 and not custom:
            body = render_template("errors/403.html")
        elif code == 404 and not custom:
            body = render_template("errors/404.html")
        elif code >= 500 and code != 503:
            g.minimal_shell = True  # no menu, no unread count, no database
            body = render_template("errors/500.html")
        else:
            title, icon = TITLES.get(code, ("خطا", "alert"))
            body = render_template(
                "errors/generic.html", code=code, title=title, text=message, icon=icon, retry_after=retry_after
            )
        from flask import make_response

        response = make_response(body, code)
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _rollback() -> None:
    try:
        db.session.rollback()
    except Exception:  # pragma: no cover - never let cleanup hide the original error
        logger.exception("rollback failed in error handler")


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CSRFError)
    def csrf_error(exc: CSRFError):
        return _render(400, CSRF_MESSAGE)

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        code = exc.code or 500
        if code < 400:  # redirects raised as exceptions pass through untouched
            return exc
        _rollback()
        retry_after = None
        retry = exc.get_headers() if hasattr(exc, "get_headers") else []
        for name, value in retry:
            if name.lower() == "retry-after" and str(value).isdigit():
                retry_after = int(value)
        custom = _custom_message(exc)
        return _render(code, custom or MESSAGES.get(code, "خطایی رخ داد."), retry_after, custom=custom is not None)

    @app.errorhandler(StaleDataError)
    def stale_data(exc: StaleDataError):
        _rollback()
        return _render(409, MESSAGES[409])

    @app.errorhandler(OnboardingRequired)
    def onboarding_required(exc: OnboardingRequired):
        what = {
            "atelier": "ابتدا اطلاعات اتلیهٔ خود را ثبت کنید.",
            "hall": "ابتدا اطلاعات تالار خود را ثبت کنید.",
        }.get(exc.what, "ابتدا پروفایل خود را تکمیل کنید.")
        url = nav.onboarding_url(get_user())
        if wants_json():
            return json_error(what, 409, "onboarding_required", url=url)
        if url:
            flash(what, "warning")
            return redirect(url)
        return _render(404, what, custom=True)

    @app.errorhandler(Exception)
    def unhandled(exc: Exception):
        _rollback()
        # Flask has already logged the traceback for unhandled exceptions.
        return _render(500, MESSAGES[500])
