"""``/me/settings`` - notification preferences, password link and the two-step mobile number change."""

from __future__ import annotations

import math
from typing import Any, Optional

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.modules.auth import otp
from app.modules.staff import account, bp, prefs, web
from app.services import audit, security
from app.services.constants import Role
from app.services.forms import Form
from app.services.money import to_persian_digits
from app.services.permissions import get_user, role_required
from app.services.security import get_rate_limiter

STAFF = Role.STAFF
SEND_FAILED = "ارسال پیامک انجام نشد. چند لحظهٔ دیگر دوباره تلاش کنید."


@bp.get("/settings")
@role_required(STAFF, allow_pending=True)
def settings():
    user = get_user()
    return render_template(
        "staff/settings.html",
        groups=prefs.GROUPS,
        enabled=prefs.enabled_groups(user),
        always_on=prefs.ALWAYS_ON,
        has_password=bool(user.password_hash),
        password_url=web.optional_url("auth.password"),
        account_url=web.optional_url("auth.account"),
    )


@bp.post("/settings/notifications")
@role_required(STAFF, allow_pending=True)
def notification_prefs():
    user = get_user()
    valid = {group["key"] for group in prefs.GROUPS}
    enabled = {key for key in Form(request.form).getlist("groups") if key in valid}
    muted = prefs.save(user, enabled)
    audit.log(user, "user.notification_prefs_changed", user, {"muted": muted})
    db.session.commit()
    flash("تنظیمات اعلان‌ها ذخیره شد.", "success")
    return redirect(url_for("staff.settings"))


# -- mobile number change --------------------------------------------------------------


def _start_context(user, **extra: Any) -> dict[str, Any]:
    return {"has_password": bool(user.password_hash), "form": {}, "errors": {}, "error": None, **extra}


@bp.route("/settings/mobile", methods=["GET", "POST"])
@role_required(STAFF, allow_pending=True)
def mobile_change():
    user = get_user()
    if request.method == "GET":
        return render_template("staff/mobile_change.html", **_start_context(user))

    has_password = bool(user.password_hash)
    form = Form(request.form)
    new_mobile = form.mobile("new_mobile", "شمارهٔ جدید", required=True)
    if new_mobile and new_mobile == user.mobile:
        form.error("new_mobile", "این همان شمارهٔ فعلی شماست.")
    limiter = get_rate_limiter()
    key = f"mobile-change:{user.id}"
    window = float(current_app.config["PASSWORD_FAILURE_WINDOW_SECONDS"])
    if has_password and form.ok:
        if not limiter.check(key, int(current_app.config["PASSWORD_MAX_FAILURES"]), window):
            form.error("current_password", "تلاش‌های ناموفق زیاد بود؛ کمی بعد دوباره امتحان کنید.")
        elif not user.check_password(request.form.get("current_password", "")):
            limiter.record(key, window)
            form.error("current_password", "رمز عبور فعلی نادرست است.")
    if not form.ok:
        return render_template("staff/mobile_change.html", **_start_context(user, form=form.values, errors=form.errors)), 422
    limiter.reset(key)

    flow = account.Flow(new=new_mobile, stage=account.STAGE_NEW if has_password else account.STAGE_OLD, at=security.timestamp())
    failure = _send(user, flow)
    if failure is not None:
        return failure
    db.session.commit()
    account.store(flow)
    return redirect(url_for("staff.mobile_verify"))


def _send(user, flow: account.Flow, *, template: Optional[str] = None, extra: Optional[dict[str, Any]] = None):
    """Send the code of the flow's stage; returns an error response, or ``None`` when it went out."""
    try:
        account.issue_code(user, flow)
    except otp.OtpRateLimited as exc:
        db.session.rollback()
        return _send_error(user, flow, exc.message, 429, {"Retry-After": str(exc.retry_after)}, template, extra)
    except otp.OtpSendFailed:
        db.session.commit()  # the failed attempt is not kept (otp.issue removed the row); keep the audit trail clean
        return _send_error(user, flow, SEND_FAILED, 503, {}, template, extra)
    return None


def _send_error(user, flow: account.Flow, message: str, status: int, headers: dict[str, str], template: Optional[str], extra):
    if template == "verify":
        return render_template("staff/mobile_verify.html", **_verify_context(user, flow, error=message)), status, headers
    return render_template("staff/mobile_change.html", **_start_context(user, error=message, **(extra or {}))), status, headers


def _verify_context(user, flow: account.Flow, *, errors: Optional[dict[str, str]] = None, error: Optional[str] = None) -> dict[str, Any]:
    return {
        "flow": flow,
        "target": security.mask_mobile(flow.target(user)),
        "new_masked": security.mask_mobile(flow.new),
        "stage": flow.stage,
        "resend_in": max(0, math.ceil(flow.resend_at - security.timestamp())),
        "otp_length": int(current_app.config["OTP_LENGTH"]),
        "debug_code": flow.debug_code if current_app.config.get("OTP_DEBUG_SHOW") else None,
        "errors": errors or {},
        "error": error,
    }


def _flow_or_restart():
    flow = account.load()
    if flow is None:
        flash("مهلت تغییر شماره تمام شد؛ دوباره شروع کنید.", "warning")
    return flow


@bp.route("/settings/mobile/verify", methods=["GET", "POST"])
@role_required(STAFF, allow_pending=True)
def mobile_verify():
    user = get_user()
    flow = _flow_or_restart()
    if flow is None:
        return redirect(url_for("staff.mobile_change"))
    if request.method == "GET":
        return render_template("staff/mobile_verify.html", **_verify_context(user, flow))

    result = otp.check(flow.target(user), request.form.get("code"), flow.purpose)
    db.session.commit()  # keeps the attempt counter / consumption even when the answer is "no"
    if result is not otp.CheckResult.OK:
        message = (
            "تعداد تلاش‌های ناموفق زیاد بود. کد جدید دریافت کنید."
            if result is otp.CheckResult.EXHAUSTED
            else "کد واردشده نادرست یا منقضی شده است."
        )
        audit.log(user, "user.mobile_change_code_failed", user, {"stage": flow.stage, "result": result.value})
        db.session.commit()
        return render_template("staff/mobile_verify.html", **_verify_context(user, flow, errors={"code": message})), 422

    if flow.stage == account.STAGE_OLD:  # the current number is proven; now the new one
        flow.stage = account.STAGE_NEW
        failure = _send(user, flow, template="verify")
        account.store(flow)
        if failure is not None:
            return failure
        db.session.commit()
        flash("شمارهٔ فعلی تأیید شد؛ کد دوم به شمارهٔ جدید فرستاده شد.", "success")
        return redirect(url_for("staff.mobile_verify"))

    # the new number is proven: it must not belong to somebody else
    if account.number_is_taken(user, flow.new):
        account.clear()
        flash("این شماره برای حساب دیگری ثبت شده است و نمی‌توان آن را به حساب شما منتقل کرد.", "error")
        return redirect(url_for("staff.mobile_change"))
    try:
        account.apply_change(user, flow.new)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        account.clear()
        flash("این شماره هم‌زمان برای حساب دیگری ثبت شد؛ شمارهٔ دیگری را امتحان کنید.", "error")
        return redirect(url_for("staff.mobile_change"))
    account.clear()
    flash(f"شمارهٔ موبایل شما به {to_persian_digits(security.mask_mobile(flow.new))} تغییر کرد.", "success")
    return redirect(url_for("staff.settings"))


@bp.post("/settings/mobile/resend")
@role_required(STAFF, allow_pending=True)
def mobile_resend():
    user = get_user()
    flow = _flow_or_restart()
    if flow is None:
        return redirect(url_for("staff.mobile_change"))
    failure = _send(user, flow, template="verify")
    if failure is not None:
        return failure
    db.session.commit()
    account.store(flow)
    flash("کد جدید ارسال شد.", "success")
    return redirect(url_for("staff.mobile_verify"))


@bp.post("/settings/mobile/cancel")
@role_required(STAFF, allow_pending=True)
def mobile_cancel():
    account.clear()
    flash("تغییر شمارهٔ موبایل لغو شد.", "info")
    return redirect(url_for("staff.settings"))
