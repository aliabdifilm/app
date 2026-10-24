"""Ceremonies (events) and the offers that hand them to ateliers."""

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    check_int,
    choice,
    fk,
    json_col,
    utcnow,
)
from app.services.constants import (
    DEFAULT_STAFFING_NEEDS,
    CeremonyType,
    EventStatus,
    OfferMode,
    OfferStatus,
    Skill,
)


def _default_staffing_needs() -> dict[str, int]:
    return dict(DEFAULT_STAFFING_NEEDS)


def normalize_staffing_needs(value: Any) -> dict[str, int]:
    """Validate ``{role: head_count}``; unknown roles / negatives are errors."""
    if value is None:
        return {}
    if not hasattr(value, "items"):
        raise TypeError("staffing_needs must be a mapping of role -> count")
    needs: dict[str, int] = {}
    for role, count in value.items():
        needs[Skill.coerce(role).value] = check_int(count, f"staffing need for {role}", minimum=0, maximum=50)
    return needs


class Event(BaseModel, TimestampMixin):
    """One ceremony at a hall on a date (کارت مراسم).

    * ``atelier_id`` is the atelier that will work the event; NULL until an
      offer is accepted or the atelier creates the event itself.
    * ``offer_mode`` says how the hall offers it (open marketplace, direct to
      chosen ateliers, or not at all).
    * ``staffing_needs`` is the head-count wanted per role (default 1 each).
    * A hall can host several events per day (lunch / dinner); ``start_time``
      tells them apart, so (hall, date) is deliberately *not* unique.
    """

    __tablename__ = "events"
    __table_args__ = (
        sa.CheckConstraint("guest_count_estimate >= 0", name="guests_nonneg"),
        sa.Index("ix_events_date_status", "date", "status"),
        sa.Index("ix_events_hall_date", "hall_id", "date"),
        sa.Index("ix_events_atelier_date", "atelier_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hall_id: Mapped[int] = fk("halls.id", ondelete="RESTRICT", index=False)
    atelier_id: Mapped[Optional[int]] = fk(
        "ateliers.id", ondelete="SET NULL", nullable=True, index=False
    )
    created_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    title: Mapped[Optional[str]] = mapped_column(sa.String(150))
    date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    start_time: Mapped[Optional[dt.time]] = mapped_column(sa.Time)
    ceremony_type: Mapped[CeremonyType] = mapped_column(
        choice(CeremonyType, "ceremony_type"), nullable=False, default=CeremonyType.WEDDING
    )
    guest_count_estimate: Mapped[Optional[int]] = mapped_column()
    notes: Mapped[Optional[str]] = mapped_column(sa.Text)
    offer_mode: Mapped[OfferMode] = mapped_column(
        choice(OfferMode, "offer_mode"), nullable=False, default=OfferMode.NONE
    )
    status: Mapped[EventStatus] = mapped_column(
        choice(EventStatus, "status"), nullable=False, default=EventStatus.DRAFT, index=True
    )
    staffing_needs: Mapped[dict] = json_col(_default_staffing_needs)

    hall: Mapped["Hall"] = relationship(back_populates="events")  # noqa: F821
    atelier: Mapped[Optional["Atelier"]] = relationship()  # noqa: F821
    created_by: Mapped[Optional["User"]] = relationship()  # noqa: F821
    offers: Mapped[list["EventOffer"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", passive_deletes=True
    )
    assignments: Mapped[list["Assignment"]] = relationship(  # noqa: F821
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Assignment.id",
    )
    invoice: Mapped[Optional["Invoice"]] = relationship(  # noqa: F821
        back_populates="event", uselist=False
    )
    equipment_usages: Mapped[list["EquipmentUsage"]] = relationship(  # noqa: F821
        back_populates="event", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("guest_count_estimate")
    def _validate_guests(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=0)

    @validates("staffing_needs")
    def _validate_needs(self, key: str, value: Any) -> dict[str, int]:
        return normalize_staffing_needs(value)

    @property
    def is_cancelled(self) -> bool:
        return self.status == EventStatus.CANCELLED

    @property
    def city(self) -> Optional[str]:
        return self.hall.city if self.hall is not None else None

    def needed(self, role: Any) -> int:
        """Head-count wanted for *role* (0 when not listed)."""
        return int((self.staffing_needs or {}).get(Skill.coerce(role).value, 0))


class EventOffer(BaseModel, TimestampMixin):
    """An event offered to one atelier (direct offer or answer to an open one).

    One row per (event, atelier); re-offering reuses the row.  At most one
    offer per event can be ``accepted`` (partial unique index).
    """

    __tablename__ = "event_offers"
    __table_args__ = (
        sa.UniqueConstraint("event_id", "atelier_id", name="uq_event_offers_pair"),
        sa.Index("ix_event_offers_atelier_status", "atelier_id", "status"),
        sa.Index(
            "uq_event_offers_one_accepted",
            "event_id",
            unique=True,
            sqlite_where=sa.text("status = 'accepted'"),
            postgresql_where=sa.text("status = 'accepted'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = fk("events.id", ondelete="CASCADE", index=False)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE", index=False)
    offered_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    status: Mapped[OfferStatus] = mapped_column(
        choice(OfferStatus, "status"), nullable=False, default=OfferStatus.PENDING
    )
    message: Mapped[Optional[str]] = mapped_column(sa.Text)
    # When the offer last became pending (creation, or a re-offer) and when the atelier answered;
    # the difference is the response time shown in the hall reports.
    offered_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime, default=utcnow)
    responded_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)

    event: Mapped[Event] = relationship(back_populates="offers")
    atelier: Mapped["Atelier"] = relationship()  # noqa: F821
    offered_by: Mapped[Optional["User"]] = relationship()  # noqa: F821
