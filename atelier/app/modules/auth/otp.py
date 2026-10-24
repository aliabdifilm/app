"""One-time codes: issuing (with rate limits) and checking.

All state lives in the ``otp_codes`` table, so the limits hold across any
number of worker processes and need no Redis:

* **cooldown**   a new code for the same mobile only after ``OTP_RESEND_COOLDOWN_SECONDS``
* **per mobile** at most ``OTP_MAX_PER_MOBILE_PER_HOUR`` codes per rolling hour
* **per IP**     at most ``OTP_MAX_PER_IP_PER_HOUR`` codes per rolling hour
* **per code**   ``OTP_MAX_ATTEMPTS`` wrong guesses, then the code is dead
* **expiry**     ``OTP_TTL_SECONDS`` (2 minutes)

The functions add rows and ``flush``; the route commits.  Both ``issue`` and
``check`` behave the same for numbers that have an account and numbers that do
not, so neither leaks whether somebody is registered.
"""

from __future__ import annotations

import datetime as dt
import enum
import hmac
import re
from dataclasses import dataclass

import sqlalchemy as sa
from flask import current_app

from app.extensions import db
from app.models import OtpCode
from app.services import audit, security, sms
from app.services.money import normalize_digits, to_persian_digits


class OtpRateLimited(Exception):
    """Too many codes requested; ``retry_after`` seconds until the next one is allowed."""

    def __init__(self, retry_after: int, scope: str) -> None:
        super().__init__(f"otp rate limited ({scope})")
        self.retry_after = max(1, int(retry_after))
        self.scope = scope  # 'cooldown' | 'mobile' | 'ip'

    @property
    def message(self) -> str:
        if self.scope == "cooldown":
            return f"برای دریافت کد جدید {to_persian_digits(self.retry_after)} ثانیهٔ دیگر صبر کنید."
        minutes = max(1, -(-self.retry_after // 60))
        return f"تعداد درخواست کد زیاد بوده است؛ {to_persian_digits(minutes)} دقیقهٔ دیگر دوباره تلاش کنید."


class OtpSendFailed(Exception):
    """The SMS gateway did not accept the message."""


class CheckResult(enum.Enum):
    OK = "ok"
    INVALID = "invalid"  # wrong, expired or no code - deliberately indistinguishable
    EXHAUSTED = "exhausted"  # too many wrong attempts: ask for a new code


@dataclass(frozen=True)
class Issued:
    expires_at: dt.datetime
    cooldown_until: dt.datetime
    code: str  # only used for the development banner (OTP_DEBUG_SHOW); never log or store it


def secret() -> str:
    return str(current_app.config.get("OTP_SECRET") or current_app.config["SECRET_KEY"])


def normalize_code(text: object) -> str:
    """``'۱۲۳ ۴۵۶'`` -> ``'123456'`` (empty string when it is not all digits)."""
    digits = re.sub(r"[\s\-]", "", normalize_digits(str(text or "")))
    return digits if digits.isdigit() else ""


def _count(stmt: sa.Select) -> int:
    return int(db.session.execute(stmt).scalar_one())


def _retry_for(oldest: dt.datetime, window: dt.timedelta, now: dt.datetime) -> int:
    return int((oldest + window - now).total_seconds()) + 1


def issue(mobile: str, purpose: str = "login") -> Issued:
    """Create and send a code.  Raises :class:`OtpRateLimited` / :class:`OtpSendFailed`."""
    config = current_app.config
    now = security.now()
    ip = security.client_ip() or None
    hour = dt.timedelta(hours=1)

    # 1. cooldown between two codes for the same number
    cooldown = int(config["OTP_RESEND_COOLDOWN_SECONDS"])
    last = db.session.execute(sa.select(sa.func.max(OtpCode.created_at)).where(OtpCode.mobile == mobile)).scalar_one()
    if last is not None and (now - last).total_seconds() < cooldown:
        raise OtpRateLimited(cooldown - (now - last).total_seconds(), "cooldown")

    # 2. hourly caps per number and per IP
    since = now - hour
    for scope, column, value, limit in (
        ("mobile", OtpCode.mobile, mobile, int(config["OTP_MAX_PER_MOBILE_PER_HOUR"])),
        ("ip", OtpCode.ip, ip, int(config["OTP_MAX_PER_IP_PER_HOUR"])),
    ):
        if value is None:
            continue
        stmt = sa.select(OtpCode.created_at).where(column == value, OtpCode.created_at > since)
        stamps = sorted(db.session.execute(stmt).scalars())
        if len(stamps) >= limit:
            raise OtpRateLimited(_retry_for(stamps[len(stamps) - limit], hour, now), scope)

    # 3. create, send, then retire older codes (a failed send must not kill a still-valid code)
    code = security.generate_otp(int(config["OTP_LENGTH"]))
    expires = now + dt.timedelta(seconds=int(config["OTP_TTL_SECONDS"]))
    row = OtpCode(
        mobile=mobile,
        purpose=purpose,
        code_hash=OtpCode.hash_code(mobile, code, secret()),
        expires_at=expires,
        ip=ip,
        created_at=now,
    )
    db.session.add(row)
    db.session.flush()
    try:
        sms.send_otp(mobile, code)
    except sms.SmsError as exc:
        current_app.logger.error("could not send OTP: %s", exc)
        db.session.delete(row)
        db.session.flush()
        raise OtpSendFailed() from None
    db.session.execute(
        sa.update(OtpCode)
        .where(OtpCode.mobile == mobile, OtpCode.consumed_at.is_(None), OtpCode.id != row.id)
        .values(consumed_at=now)
    )
    audit.log(None, "auth.otp_sent", None, {"mobile": security.mask_mobile(mobile), "purpose": purpose}, ip=ip)
    return Issued(expires, now + dt.timedelta(seconds=cooldown), code)


def check(mobile: str, code: object, purpose: str = "login") -> CheckResult:
    """Verify *code* for *mobile*; counts the attempt and consumes the code on success."""
    config = current_app.config
    now = security.now()
    text = normalize_code(code)
    max_attempts = int(config["OTP_MAX_ATTEMPTS"])
    row = db.session.execute(
        sa.select(OtpCode)
        .where(OtpCode.mobile == mobile, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None))
        .order_by(OtpCode.created_at.desc(), OtpCode.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        # same work as a real check so timing does not tell "no code" from "wrong code"
        hmac.compare_digest(OtpCode.hash_code(mobile, text or "0", secret()), OtpCode.hash_code(mobile, "x", secret()))
        return CheckResult.INVALID
    if row.attempts >= max_attempts:
        return CheckResult.EXHAUSTED
    if len(text) != int(config["OTP_LENGTH"]):
        row.attempts += 1  # a malformed guess still costs an attempt
        db.session.flush()
        return CheckResult.EXHAUSTED if row.attempts >= max_attempts else CheckResult.INVALID
    if row.verify(text, secret(), max_attempts=max_attempts, now=now):
        db.session.flush()
        return CheckResult.OK
    db.session.flush()
    if row.attempts >= max_attempts:
        return CheckResult.EXHAUSTED
    return CheckResult.INVALID
