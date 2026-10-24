"""Platform plumbing: notifications, audit log, settings, announcements."""

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    choice,
    fk,
    json_col,
    utcnow,
)
from app.services.constants import (
    SETTING_DEFAULTS,
    AnnouncementAudience,
    AnnouncementLevel,
    Role,
)


class Notification(BaseModel):
    """An in-app notification for one user.

    ``kind`` is a short free string (see ``constants.NotificationKind`` for the
    well-known ones).  ``entity_type`` / ``entity_id`` optionally point at the
    thing it is about, so the UI can build a link even when ``url`` is empty.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.Index("ix_notifications_user_read", "user_id", "read_at"),
        sa.Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=False)
    kind: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    url: Mapped[Optional[str]] = mapped_column(sa.String(500))
    entity_type: Mapped[Optional[str]] = mapped_column(sa.String(40))
    entity_id: Mapped[Optional[int]] = mapped_column()
    read_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    user: Mapped["User"] = relationship()  # noqa: F821

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class AuditLogImmutableError(RuntimeError):
    """Audit rows are append-only."""


class AuditLog(BaseModel):
    """Who did what to which entity, and when (append-only).

    ``entity_type`` is the table name of the entity (``assignments``,
    ``invoices`` ...).  The actor is NULL for system actions.  ``data`` holds
    small JSON detail (old/new values, reason); never put secrets in it.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_logs_action_created", "action", "created_at"),
        sa.Index("ix_audit_logs_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    action: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(sa.String(40))
    entity_id: Mapped[Optional[int]] = mapped_column()
    data: Mapped[Optional[dict]] = json_col(None, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(sa.String(45))
    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    actor: Mapped[Optional["User"]] = relationship()  # noqa: F821


@event.listens_for(AuditLog, "before_update")
def _audit_no_update(mapper: Any, connection: Any, target: AuditLog) -> None:
    raise AuditLogImmutableError("audit log rows cannot be modified")


@event.listens_for(AuditLog, "before_delete")
def _audit_no_delete(mapper: Any, connection: Any, target: AuditLog) -> None:
    raise AuditLogImmutableError("audit log rows cannot be deleted")


class Setting(BaseModel):
    """Site-wide key/value setting (JSON value, edited by admins)."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(100), unique=True, nullable=False)
    value: Mapped[Any] = mapped_column(sa.JSON(none_as_null=True), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(sa.String(300))
    updated_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """Value of *key*; falls back to *default*, then ``SETTING_DEFAULTS``."""
        row = db.session.execute(sa.select(cls).where(cls.key == key)).scalar_one_or_none()
        if row is not None and row.value is not None:
            return row.value
        return default if default is not None else SETTING_DEFAULTS.get(key)

    @classmethod
    def set(cls, key: str, value: Any, *, actor: Any = None) -> "Setting":
        """Create or update *key* (the caller commits).  Assigns a fresh value."""
        row = db.session.execute(sa.select(cls).where(cls.key == key)).scalar_one_or_none()
        if row is None:
            row = cls(key=key)
            db.session.add(row)
        row.value = value
        row.updated_by_id = getattr(actor, "id", actor)
        return row


class Announcement(BaseModel, TimestampMixin):
    """A banner message from the admins, optionally scheduled and role-targeted."""

    __tablename__ = "announcements"
    __table_args__ = (sa.Index("ix_announcements_published_window", "is_published", "starts_at", "ends_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    audience: Mapped[AnnouncementAudience] = mapped_column(
        choice(AnnouncementAudience, "audience"),
        nullable=False,
        default=AnnouncementAudience.ALL,
    )
    level: Mapped[AnnouncementLevel] = mapped_column(
        choice(AnnouncementLevel, "level"), nullable=False, default=AnnouncementLevel.INFO
    )
    is_published: Mapped[bool] = mapped_column(nullable=False, default=True)
    starts_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    ends_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    created_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)

    created_by: Mapped[Optional["User"]] = relationship()  # noqa: F821

    @classmethod
    def active_for(cls, role: Any, now: Optional[dt.datetime] = None) -> sa.Select:
        """``select(Announcement)`` of published, in-window items for *role*."""
        now = now or utcnow()
        audience = AnnouncementAudience(Role.coerce(role).value)
        return (
            sa.select(cls)
            .where(
                cls.is_published.is_(True),
                cls.audience.in_([AnnouncementAudience.ALL, audience]),
                sa.or_(cls.starts_at.is_(None), cls.starts_at <= now),
                sa.or_(cls.ends_at.is_(None), cls.ends_at > now),
            )
            .order_by(cls.created_at.desc())
        )
