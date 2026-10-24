"""Changing the mobile number: two codes, never one.

The mobile number is the login of the account, so a stolen session must not be able to move the account to
a number the thief owns.  The flow therefore proves **both** ends:

1. the person who has a password types it (the current owner proves themselves); a password-less person
   receives a code on the *current* number (``stage = "old"``);
2. a code is sent to the *new* number and must be typed (``stage = "new"``) - proof that the new number is theirs.

Only then is ``User.mobile`` replaced.  State lives in the signed session (``SESSION_KEY``) for 15 minutes; codes
use the shared OTP pipeline of the auth module (hashed at rest, 2 minutes, 5 attempts, resend cooldown, hourly
caps per number and per IP).  Nothing here commits - the route does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import sqlalchemy as sa
from flask import current_app, session

from app.extensions import db
from app.models import User
from app.modules.auth import otp
from app.services import audit, notifications, security
from app.services.constants import NotificationKind

SESSION_KEY = "staff_mobile_change"
TTL_SECONDS = 15 * 60
PURPOSE_OLD = "mobile_change_old"
PURPOSE_NEW = "mobile_change"

STAGE_OLD = "old"
STAGE_NEW = "new"


@dataclass
class Flow:
    new: str  # the number the account is moving to (09xxxxxxxxx)
    stage: str  # STAGE_OLD: waiting for the code sent to the current number; STAGE_NEW: to the new one
    at: float  # when the flow started / last progressed (security.timestamp())
    resend_at: float = 0.0
    debug_code: Optional[str] = None  # shown only when OTP_DEBUG_SHOW is on (development)

    def to_session(self) -> dict[str, Any]:
        return {"new": self.new, "stage": self.stage, "at": self.at, "resend_at": self.resend_at, "dbg": self.debug_code}

    @property
    def purpose(self) -> str:
        return PURPOSE_OLD if self.stage == STAGE_OLD else PURPOSE_NEW

    def target(self, user: User) -> str:
        """The number the current code goes to."""
        return user.mobile if self.stage == STAGE_OLD else self.new


def load() -> Optional[Flow]:
    data = session.get(SESSION_KEY)
    if not isinstance(data, dict) or data.get("stage") not in (STAGE_OLD, STAGE_NEW) or not data.get("new"):
        return None
    if security.timestamp() - float(data.get("at", 0)) > TTL_SECONDS:
        session.pop(SESSION_KEY, None)
        return None
    return Flow(
        new=str(data["new"]),
        stage=data["stage"],
        at=float(data.get("at", 0)),
        resend_at=float(data.get("resend_at", 0)),
        debug_code=data.get("dbg"),
    )


def store(flow: Flow) -> None:
    session[SESSION_KEY] = flow.to_session()


def clear() -> None:
    session.pop(SESSION_KEY, None)


def issue_code(user: User, flow: Flow) -> Flow:
    """Send the code of the flow's current stage.  Raises ``otp.OtpRateLimited`` / ``otp.OtpSendFailed``."""
    issued = otp.issue(flow.target(user), flow.purpose)
    flow.at = security.timestamp()
    flow.resend_at = security.to_timestamp(issued.cooldown_until)
    flow.debug_code = issued.code if current_app.config.get("OTP_DEBUG_SHOW") else None
    return flow


def number_is_taken(user: User, mobile: str) -> bool:
    """Does another account use *mobile*?  (Only asked after its owner proved the number.)"""
    return (
        db.session.scalar(sa.select(User.id).where(User.mobile == mobile, User.id != user.id).limit(1))
        is not None
    )


def apply_change(user: User, new_mobile: str) -> str:
    """Replace the number of *user*; returns the old one.  The caller commits and clears the flow."""
    old = user.mobile
    user.mobile = new_mobile
    db.session.flush()
    audit.log(
        user,
        "user.mobile_changed",
        user,
        {"old": security.mask_mobile(old), "new": security.mask_mobile(new_mobile)},
    )
    notifications.notify(
        user,
        NotificationKind.ACCOUNT,
        "شمارهٔ موبایل حساب شما تغییر کرد",
        f"از این پس با شمارهٔ {security.mask_mobile(new_mobile)} وارد شوید. اگر این کار از شما نبوده، فوراً با پشتیبانی تماس بگیرید.",
    )
    return old
