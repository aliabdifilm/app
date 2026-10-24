"""Routes of the ``auth`` module: OTP sign-in, registration, password, logout."""

from __future__ import annotations

import math
from typing import Any, Optional

import sqlalchemy as sa
from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_user, logout_user

from app.extensions import db
from app.models import Setting, User
from app.modules.auth import bp, otp
from app.services import audit, nav, notifications, security
from app.services.constants import NotificationKind, Role, UserStatus
from app.services.forms import Form
from app.services.permissions import get_user, login_required
from app.services.security import get_rate_limiter, request_next, safe_next

FLOW_KEY = "auth_flow"  # an OTP has been sent: {m, p, next, remember, name, role, resend_at, dbg}
VERIFIED_KEY = "auth_verified"  # the owner of an unregistered number proved it: {m, next, remember, at}
REAUTH_KEY = "pw_reauth"  # timestamp until which a fresh OTP lets a password-less user set a password
FLOW_TTL = 15 * 60
REAUTH_TTL = 10 * 60

SUSPENDED_MESSAGE = "حساب شما مسدود شده است. برای پیگیری با پشتیبانی سامانه تماس بگیرید."
GENERIC_LOGIN_ERROR = "شمارهٔ موبایل یا رمز عبور نادرست است."

ROLE_CHOICES = (
    {
        "value": Role.STAFF.value,
        "title": "نیرو",
        "desc": "عکاس، ادیتور، پرزنتور یا فروشنده‌ام و دنبال افیش شدن در مراسم‌ها هستم.",
        "icon": "camera",
    },
    {
        "value": Role.ATELIER_MANAGER.value,
        "title": "مدیر اتلیه (کارفرما)",
        "desc": "اتلیه سیار دارم؛ مراسم می‌گیرم، نیرو افیش می‌کنم و حسابداری و انبار را مدیریت می‌کنم.",
        "icon": "briefcase",
    },
    {
        "value": Role.HALL_MANAGER.value,
        "title": "مدیر تالار یا باغ",
        "desc": "تاریخ مراسم‌های تالارم را ثبت می‌کنم و به اتلیه‌ها می‌سپارم.",
        "icon": "building",
    },
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _config(key: str) -> Any:
    return current_app.config[key]


def _registration_roles() -> list[dict[str, Any]]:
    """Self-service roles: the configured ones, never ``admin``."""
    allowed = set(_config("REGISTRATION_ROLES")) - {Role.ADMIN.value}
    needs_approval = set(_config("REQUIRE_APPROVAL_ROLES"))
    return [
        {**choice, "needs_approval": choice["value"] in needs_approval}
        for choice in ROLE_CHOICES
        if choice["value"] in allowed
    ]


def _registration_open() -> bool:
    return bool(Setting.get("registration_open", True))


def _form_next() -> str:
    """The safe ``next`` of this request ('' when there is none worth keeping)."""
    value = safe_next(request.values.get("next"), "")
    return value


def _flow() -> Optional[dict[str, Any]]:
    flow = session.get(FLOW_KEY)
    if not isinstance(flow, dict) or "m" not in flow or "p" not in flow:
        return None
    if security.timestamp() - float(flow.get("at", 0)) > FLOW_TTL:
        session.pop(FLOW_KEY, None)
        return None
    return flow


def _verified() -> Optional[dict[str, Any]]:
    data = session.get(VERIFIED_KEY)
    if not isinstance(data, dict) or "m" not in data:
        return None
    if security.timestamp() - float(data.get("at", 0)) > FLOW_TTL:
        session.pop(VERIFIED_KEY, None)
        return None
    return data


def _begin(
    mobile: str,
    *,
    purpose: str,
    template: str,
    context: dict[str, Any],
    next_url: str = "",
    remember: bool = False,
    name: Optional[str] = None,
    role: Optional[str] = None,
):
    """Send a code and go to the verify page, or re-render *template* with the reason it failed."""
    try:
        issued = otp.issue(mobile, purpose)
    except otp.OtpRateLimited as exc:
        db.session.rollback()
        return render_template(template, error=exc.message, **context), 429, {"Retry-After": str(exc.retry_after)}
    except otp.OtpSendFailed:
        db.session.commit()
        return (
            render_template(
                template,
                error="ارسال پیامک انجام نشد. چند لحظهٔ دیگر دوباره تلاش کنید.",
                **context,
            ),
            503,
        )
    db.session.commit()
    flow: dict[str, Any] = {
        "m": mobile,
        "p": purpose,
        "next": next_url,
        "remember": bool(remember),
        "name": name,
        "role": role,
        "at": security.timestamp(),
        "resend_at": security.to_timestamp(issued.cooldown_until),
    }
    if _config("OTP_DEBUG_SHOW"):
        flow["dbg"] = issued.code  # development banner only; refused in production by the config loader
    session[FLOW_KEY] = flow
    session.pop(VERIFIED_KEY, None)
    return redirect(url_for("auth.verify"))


def _create_user(mobile: str, full_name: str, role: str) -> User:
    """Create the account of a number whose owner proved it with a code."""
    chosen = Role.coerce(role)
    if chosen == Role.ADMIN or chosen.value not in {c["value"] for c in _registration_roles()}:
        raise ValueError("role not allowed for self-registration")
    needs_approval = chosen.value in set(_config("REQUIRE_APPROVAL_ROLES"))
    user = User(
        mobile=mobile,
        full_name=full_name,
        role=chosen,
        status=UserStatus.PENDING if needs_approval else UserStatus.ACTIVE,
    )
    db.session.add(user)
    db.session.flush()
    audit.log(user, "user.registered", user, {"role": chosen.value, "status": user.status.value})
    notifications.notify(
        user,
        NotificationKind.ACCOUNT,
        "به اتلیه سیار خوش آمدید",
        "حساب شما ساخته شد." + (" پس از تأیید مدیر سامانه فعال می‌شود." if needs_approval else ""),
    )
    if needs_approval:
        admins = db.session.execute(
            sa.select(User.id).where(User.role == Role.ADMIN, User.status == UserStatus.ACTIVE)
        ).scalars()
        notifications.notify_many(
            admins,
            NotificationKind.ACCOUNT,
            "درخواست تأیید حساب جدید",
            f"{full_name} ({chosen.label}) ثبت‌نام کرد و منتظر تأیید است.",
        )
    return user


def _finish_login(user: User, method: str, *, remember: bool, next_url: str = "", new: bool = False):
    """Start a fresh session for *user* and redirect to where they were going."""
    if not user.is_active:
        return None
    session.clear()  # no state survives from before the login (session fixation / stale flow data)
    stale_remember_cookie = current_app.config["REMEMBER_COOKIE_NAME"] in request.cookies
    login_user(user, remember=remember, fresh=True)
    if stale_remember_cookie and not remember:
        session["_remember"] = "clear"  # a previous user's "remember me" cookie must not outlive this login
    session.permanent = True
    user.touch_login(security.now())
    audit.log(user, "auth.login", user, {"method": method, "new_account": new})
    db.session.commit()
    if new:
        flash("حساب شما ساخته شد. خوش آمدید!", "success")
        return redirect(nav.onboarding_url(user) or safe_next(next_url) or url_for("main.dashboard"))
    return redirect(safe_next(next_url, url_for("main.dashboard")))


def _refuse_suspended(template: str, context: dict[str, Any]):
    session.pop(FLOW_KEY, None)
    return render_template(template, error=SUSPENDED_MESSAGE, **context), 403


def _already_signed_in():
    return redirect(request_next(url_for("main.dashboard")))


# --------------------------------------------------------------------------
# login (mobile + code)
# --------------------------------------------------------------------------


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _already_signed_in()
    context = {"next": _form_next(), "password_enabled": _config("PASSWORD_LOGIN_ENABLED")}
    if request.method == "POST":
        form = Form(request.form)
        mobile = form.mobile("mobile", required=True)
        remember = form.boolean("remember")
        if form.ok:
            return _begin(
                mobile,
                purpose="login",
                template="auth/login.html",
                context={**context, "form": form.values},
                next_url=context["next"],
                remember=remember,
            )
        return render_template("auth/login.html", form=form.values, errors=form.errors, **context), 422
    return render_template("auth/login.html", form={}, errors={}, **context)


@bp.route("/verify", methods=["GET", "POST"])
def verify():
    flow = _flow()
    if flow is None:
        flash("مهلت ورود تمام شد؛ دوباره شمارهٔ موبایل خود را وارد کنید.", "warning")
        return redirect(url_for("auth.login"))
    mobile, purpose = flow["m"], flow["p"]
    context = {
        "mobile": mobile,
        "debug_code": flow.get("dbg") if _config("OTP_DEBUG_SHOW") else None,
        "resend_in": max(0, math.ceil(float(flow.get("resend_at", 0)) - security.timestamp())),
        "otp_length": int(_config("OTP_LENGTH")),
        "purpose": purpose,
    }
    if request.method == "GET":
        return render_template("auth/verify.html", errors={}, **context)

    result = otp.check(mobile, request.form.get("code"), purpose)
    db.session.commit()  # keeps the attempt counter / consumption even when the answer is "no"
    if result is not otp.CheckResult.OK:
        if result is otp.CheckResult.EXHAUSTED:
            message = "تعداد تلاش‌های ناموفق زیاد بود. کد جدید دریافت کنید."
        else:
            message = "کد واردشده نادرست یا منقضی شده است."
        audit.log(None, "auth.otp_failed", None, {"mobile": security.mask_mobile(mobile), "result": result.value})
        db.session.commit()
        return render_template("auth/verify.html", errors={"code": message}, **context), 422
    return _after_verified(flow)


def _after_verified(flow: dict[str, Any]):
    mobile, purpose = flow["m"], flow["p"]
    remember, next_url = bool(flow.get("remember")), flow.get("next") or ""

    if purpose == "password":  # a signed-in user re-confirming their number to set a password
        user = get_user()
        session.pop(FLOW_KEY, None)
        if user is None or user.mobile != mobile:
            return redirect(url_for("auth.login"))
        session[REAUTH_KEY] = security.timestamp() + REAUTH_TTL
        return redirect(url_for("auth.password"))

    user = db.session.execute(sa.select(User).where(User.mobile == mobile)).scalar_one_or_none()
    if user is not None:
        if not user.is_active:
            audit.log(user, "auth.login_blocked", user, {"reason": "suspended"})
            db.session.commit()
            return _refuse_suspended("auth/login.html", {"form": {}, "errors": {}, "next": next_url,
                                                          "password_enabled": _config("PASSWORD_LOGIN_ENABLED")})
        response = _finish_login(user, "otp", remember=remember, next_url=next_url)
        if flow.get("role"):  # they used the registration form with a number that already has an account
            flash("این شماره قبلاً ثبت‌نام کرده بود؛ وارد حساب خود شدید.", "info")
        return response

    # no account yet: either the registration form was filled in before the code, or not
    if flow.get("role") and flow.get("name") and _registration_open():
        try:
            user = _create_user(mobile, flow["name"], flow["role"])
        except ValueError:
            db.session.rollback()
            session.pop(FLOW_KEY, None)
            flash("این نقش برای ثبت‌نام مجاز نیست.", "error")
            return redirect(url_for("auth.register"))
        return _finish_login(user, "otp", remember=remember, next_url=next_url, new=True)

    session.pop(FLOW_KEY, None)
    session[VERIFIED_KEY] = {"m": mobile, "next": next_url, "remember": remember, "at": security.timestamp()}
    return redirect(url_for("auth.register"))


@bp.post("/verify/resend")
def resend():
    flow = _flow()
    if flow is None:
        return redirect(url_for("auth.login"))
    if flow["p"] == "password" and not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    try:
        issued = otp.issue(flow["m"], flow["p"])
    except otp.OtpRateLimited as exc:
        db.session.rollback()
        flash(exc.message, "warning")
        return redirect(url_for("auth.verify"))
    except otp.OtpSendFailed:
        db.session.commit()
        flash("ارسال پیامک انجام نشد. چند لحظهٔ دیگر دوباره تلاش کنید.", "error")
        return redirect(url_for("auth.verify"))
    db.session.commit()
    flow = dict(flow)
    flow["at"] = security.timestamp()
    flow["resend_at"] = security.to_timestamp(issued.cooldown_until)
    if _config("OTP_DEBUG_SHOW"):
        flow["dbg"] = issued.code
    session[FLOW_KEY] = flow
    flash("کد جدید ارسال شد.", "success")
    return redirect(url_for("auth.verify"))


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return _already_signed_in()
    roles = _registration_roles()
    verified = _verified()
    open_ = _registration_open()
    context = {
        "roles": roles,
        "verified_mobile": verified["m"] if verified else None,
        "registration_open": open_,
        "next": _form_next() or (verified or {}).get("next", ""),
    }
    if request.method == "GET":
        return render_template("auth/register.html", form={}, errors={}, **context), (200 if open_ else 403)
    if not open_:
        return render_template("auth/register.html", form={}, errors={}, **context), 403

    form = Form(request.form)
    name = form.text("full_name", "نام و نام خانوادگی", required=True, min_len=2, max_len=120)
    role = form.choice("role", "نقش", [c["value"] for c in roles], required=True)
    mobile = verified["m"] if verified else form.mobile("mobile", required=True)
    remember = form.boolean("remember")
    if not form.ok:
        return render_template("auth/register.html", form=form.values, errors=form.errors, **context), 422

    if verified:  # the number was proven already (they started on the login page)
        try:
            user = _create_user(mobile, name, role)
        except ValueError:
            db.session.rollback()
            form.error("role", "این نقش برای ثبت‌نام مجاز نیست.")
            return render_template("auth/register.html", form=form.values, errors=form.errors, **context), 422
        return _finish_login(
            user, "otp", remember=bool(verified.get("remember")) or remember, next_url=verified.get("next", ""), new=True
        )
    return _begin(
        mobile,
        purpose="login",
        template="auth/register.html",
        context={**context, "form": form.values, "errors": {}},
        next_url=context["next"],
        remember=remember,
        name=name,
        role=role,
    )


@bp.get("/pending")
@login_required(allow_pending=True)
def pending():
    user = get_user()
    if user.is_approved:
        return redirect(url_for("main.dashboard"))
    return render_template("auth/pending.html", user=user)


# --------------------------------------------------------------------------
# password sign-in (optional)
# --------------------------------------------------------------------------


def _password_keys(mobile: str) -> tuple[str, str]:
    ip = security.client_ip()
    return f"pwlogin:{ip}:{mobile}", f"pwlogin-ip:{ip}"


@bp.route("/login/password", methods=["GET", "POST"])
def login_password():
    if not _config("PASSWORD_LOGIN_ENABLED"):
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated:
        return _already_signed_in()
    context = {"next": _form_next()}
    if request.method == "GET":
        return render_template("auth/login_password.html", form={}, errors={}, **context)

    form = Form(request.form)
    raw_password = request.form.get("password", "")
    remember = form.boolean("remember")
    mobile = form.mobile("mobile", required=True)
    if not form.ok or not raw_password:
        errors = dict(form.errors)
        if not raw_password:
            errors.setdefault("password", "رمز عبور را وارد کنید.")
        return render_template("auth/login_password.html", form=form.values, errors=errors, **context), 422

    limiter = get_rate_limiter()
    window = float(_config("PASSWORD_FAILURE_WINDOW_SECONDS"))
    pair_key, ip_key = _password_keys(mobile)
    pair = limiter.check(pair_key, int(_config("PASSWORD_MAX_FAILURES")), window)
    per_ip = limiter.check(ip_key, int(_config("PASSWORD_MAX_FAILURES_PER_IP")), window)
    if not pair or not per_ip:
        retry = max(pair.retry_after, per_ip.retry_after)
        minutes = max(1, -(-retry // 60))
        from app.services.money import to_persian_digits

        message = f"تلاش‌های ناموفق زیاد بود؛ {to_persian_digits(minutes)} دقیقهٔ دیگر دوباره امتحان کنید یا با کد پیامکی وارد شوید."
        return (
            render_template("auth/login_password.html", form=form.values, errors={}, error=message, **context),
            429,
            {"Retry-After": str(retry)},
        )

    user = db.session.execute(sa.select(User).where(User.mobile == mobile)).scalar_one_or_none()
    if user is not None and user.password_hash:
        valid = user.check_password(raw_password)
    else:
        security.burn_password_check(raw_password)  # same cost for unknown accounts
        valid = False
    if not valid:
        limiter.record(pair_key, window)
        limiter.record(ip_key, window)
        audit.log(user, "auth.login_failed", user, {"method": "password", "mobile": security.mask_mobile(mobile)})
        db.session.commit()
        return (
            render_template("auth/login_password.html", form=form.values, errors={}, error=GENERIC_LOGIN_ERROR, **context),
            422,
        )
    if not user.is_active:
        audit.log(user, "auth.login_blocked", user, {"reason": "suspended"})
        db.session.commit()
        return render_template("auth/login_password.html", form=form.values, errors={}, error=SUSPENDED_MESSAGE, **context), 403
    limiter.reset(pair_key)
    return _finish_login(user, "password", remember=remember, next_url=context["next"])


# --------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":  # a link or a bookmark must not log anybody out (CSRF); just go home
        return redirect(url_for("auth.account") if current_user.is_authenticated else url_for("main.landing"))
    if current_user.is_authenticated:
        user = get_user()
        audit.log(user, "auth.logout", user)
        db.session.commit()
    logout_user()  # also tells Flask-Login to delete the "remember me" cookie (session["_remember"] = "clear")
    for key in list(session):
        if key != "_remember":  # everything else (selected atelier, flow state ...) goes
            session.pop(key)
    flash("از حساب خود خارج شدید.", "success")
    return redirect(url_for("main.landing"))


# --------------------------------------------------------------------------
# account and password
# --------------------------------------------------------------------------


@bp.get("/account")
@login_required(allow_pending=True)
def account():
    return render_template("auth/account.html", user=get_user())


@bp.post("/password/code")
@login_required(allow_pending=True)
def password_code():
    """Send a code to my own number so I may set a first password."""
    user = get_user()
    return _begin(
        user.mobile,
        purpose="password",
        template="auth/password.html",
        context={"user": user, "has_password": bool(user.password_hash), "reauth_ok": False, "errors": {}, "form": {}},
    )


@bp.route("/password", methods=["GET", "POST"])
@login_required(allow_pending=True)
def password():
    user = get_user()
    has_password = bool(user.password_hash)
    reauth_ok = float(session.get(REAUTH_KEY, 0)) > security.timestamp()
    context = {"user": user, "has_password": has_password, "reauth_ok": reauth_ok}
    if request.method == "GET":
        return render_template("auth/password.html", errors={}, form={}, **context)

    errors: dict[str, str] = {}
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    limiter = get_rate_limiter()
    key = f"pwchange:{user.id}"
    window = float(_config("PASSWORD_FAILURE_WINDOW_SECONDS"))

    if has_password:
        if not limiter.check(key, int(_config("PASSWORD_MAX_FAILURES")), window):
            errors["current_password"] = "تلاش‌های ناموفق زیاد بود؛ کمی بعد دوباره امتحان کنید."
        elif not user.check_password(current_pw):
            limiter.record(key, window)
            errors["current_password"] = "رمز عبور فعلی نادرست است."
    elif not reauth_ok:
        errors["_"] = "برای تعیین رمز عبور ابتدا شمارهٔ موبایل خود را با کد پیامکی تأیید کنید."
    problems = security.validate_password(new_pw, mobile=user.mobile)
    if problems:
        errors["new_password"] = problems[0]
    elif new_pw != confirm:
        errors["confirm_password"] = "تکرار رمز عبور با رمز جدید یکی نیست."
    if has_password and "new_password" not in errors and current_pw == new_pw:
        errors["new_password"] = "رمز جدید باید با رمز فعلی فرق داشته باشد."
    if errors:
        return render_template("auth/password.html", errors=errors, form={}, **context), 422

    user.set_password(new_pw)
    audit.log(user, "auth.password_changed", user, {"first_password": not has_password})
    notifications.notify(user, NotificationKind.ACCOUNT, "رمز عبور شما تغییر کرد", "اگر این کار از شما نبوده، فوراً با پشتیبانی تماس بگیرید.")
    session.pop(REAUTH_KEY, None)
    limiter.reset(key)
    db.session.commit()
    flash("رمز عبور ذخیره شد.", "success")
    return redirect(url_for("auth.account"))
