"""Accounts: User and one-time login codes (OtpCode)."""

import datetime as dt
import hashlib
import hmac
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager
from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    choice,
    json_col,
    normalize_mobile,
    utcnow,
)
from app.services.constants import Role, UserStatus


class User(BaseModel, TimestampMixin):
    """A platform account.

    Login is by mobile number + OTP (assumption A6); the password is optional.
    One user has exactly one :class:`Role`.  Staff may additionally own a
    :class:`~app.models.staff.StaffProfile`.

    Flask-Login contract: ``is_active`` is False only for *suspended* users, so
    ``pending`` users can sign in (to finish their profile / see the "awaiting
    approval" screen).  Gate privileged features with :attr:`is_approved`.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    mobile: Mapped[str] = mapped_column(sa.String(11), unique=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(sa.String(255))
    full_name: Mapped[str] = mapped_column(sa.String(120), nullable=False, default="")
    role: Mapped[Role] = mapped_column(
        choice(Role, "role"), nullable=False, default=Role.STAFF, index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        choice(UserStatus, "status"), nullable=False, default=UserStatus.PENDING, index=True
    )
    is_verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    city: Mapped[Optional[str]] = mapped_column(sa.String(80), index=True)
    avatar_path: Mapped[Optional[str]] = mapped_column(sa.String(300))
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    is_public_profile: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Notification preferences chosen by the user: ``{"muted": ["review_received", ...]}``.
    # Written by the staff module's settings page, read by ``notifications.notify`` (essential kinds
    # such as invitations can never be muted).  Existing databases: see docs/DATA_MODEL.md section 8.
    notification_prefs: Mapped[dict] = json_col(dict)

    staff_profile: Mapped[Optional["StaffProfile"]] = relationship(  # noqa: F821
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    halls: Mapped[list["Hall"]] = relationship(back_populates="manager")  # noqa: F821
    ateliers: Mapped[list["Atelier"]] = relationship(back_populates="manager")  # noqa: F821

    # -- validation -------------------------------------------------------

    @validates("mobile")
    def _validate_mobile(self, key: str, value: Any) -> str:
        return normalize_mobile(value)

    # -- Flask-Login protocol (no UserMixin: its __eq__ equates unsaved users) --

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @property
    def is_active(self) -> bool:
        return self.status != UserStatus.SUSPENDED

    def get_id(self) -> str:
        return str(self.id)

    # -- helpers ----------------------------------------------------------

    @property
    def is_approved(self) -> bool:
        """True once an admin (or the signup flow) activated the account."""
        return self.status == UserStatus.ACTIVE

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def has_role(self, *roles: Any) -> bool:
        return self.role in {Role.coerce(role) for role in roles}

    @property
    def display_name(self) -> str:
        """Name for UI; falls back to the mobile number for unnamed accounts."""
        return self.full_name.strip() or self.mobile

    def set_password(self, password: str) -> None:
        if not password or len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        return check_password_hash(self.password_hash, password)

    def touch_login(self, when: dt.datetime | None = None) -> None:
        self.last_login_at = when or utcnow()


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    """Flask-Login session loader; suspended users are logged out on the next request."""
    try:
        pk = int(user_id)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, pk)
    if user is None or user.status == UserStatus.SUSPENDED:
        return None
    return user


class OtpCode(BaseModel):
    """A one-time login code that was sent to a mobile number.

    Only a keyed hash of the code is stored (:meth:`hash_code`).  Rows are
    append-mostly: ``attempts`` counts wrong guesses, ``consumed_at`` marks a
    successful use.  The (mobile, created_at) and (ip, created_at) indexes
    serve rate limiting ("how many codes in the last hour?").
    """

    __tablename__ = "otp_codes"
    __table_args__ = (
        sa.Index("ix_otp_codes_mobile_created", "mobile", "created_at"),
        sa.Index("ix_otp_codes_ip_created", "ip", "created_at"),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mobile: Mapped[str] = mapped_column(sa.String(11), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="login")
    code_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    consumed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    ip: Mapped[Optional[str]] = mapped_column(sa.String(45))
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    @validates("mobile")
    def _validate_mobile(self, key: str, value: Any) -> str:
        return normalize_mobile(value)

    @staticmethod
    def hash_code(mobile: str, code: str, secret: str) -> str:
        """Keyed hash (HMAC-SHA256) of a code, bound to the mobile number."""
        message = f"{normalize_mobile(mobile)}:{code}".encode()
        return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    def is_usable(self, max_attempts: int = 5, now: dt.datetime | None = None) -> bool:
        return (
            self.consumed_at is None
            and not self.is_expired(now)
            and self.attempts < max_attempts
        )

    def verify(
        self,
        code: str,
        secret: str,
        *,
        max_attempts: int = 5,
        now: dt.datetime | None = None,
    ) -> bool:
        """Check a submitted code; counts the attempt and consumes on success.

        Returns False for expired, consumed or over-attempted codes without
        revealing which.  Comparison is constant-time.
        """
        now = now or utcnow()
        if not self.is_usable(max_attempts, now):
            return False
        self.attempts += 1
        expected = self.hash_code(self.mobile, str(code), secret)
        if hmac.compare_digest(self.code_hash, expected):
            self.consumed_at = now
            return True
        return False
