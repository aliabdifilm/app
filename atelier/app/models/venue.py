"""Organisations: halls (venues), ateliers (employers), roster (fixed staff)."""

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.extensions import db
from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    check_int,
    check_money,
    clean_phone,
    fk,
    json_col,
    money,
    money_checks,
    normalize_mobile,
)
from app.models.terms import normalize_skills, normalize_terms_by_role
from app.services.constants import CeremonyType, Skill

# Keys of ``Hall.logistics`` (set-up notes for the atelier team; all optional free text).
HALL_LOGISTICS_KEYS: tuple[str, ...] = (
    "loading",  # where to unload the equipment
    "power",  # power outlets
    "wifi",  # Wi-Fi / mobile coverage
    "parking",
    "setup_spot",  # where the atelier sets up
    "contact_name",  # the person to call on site
    "contact_phone",
)
MAX_HALL_PHOTOS = 12


class Hall(BaseModel, TimestampMixin):
    """A wedding hall / garden (تالار / باغ) run by a hall manager.

    Besides the basics the profile carries what an atelier team needs on the day:
    ``map_url`` and ``logistics`` (loading entrance, power, Wi-Fi, parking, set-up spot,
    on-site contact), the ``default_ceremony_types`` the hall usually hosts, photos
    (:class:`HallPhoto`) and a ``default_rent`` - what the hall usually charges an atelier
    per event (a suggestion for the invoice's venue-rent line, see
    :meth:`Atelier.rent_for_hall`).  ``single_atelier_per_day`` makes the hall grant a date
    to one atelier only (the exclusivity check of ``app.services.event_offers``).
    """

    __tablename__ = "halls"
    __table_args__ = (
        sa.UniqueConstraint("manager_id", "name", name="uq_halls_manager_name"),
        sa.Index("ix_halls_city_active", "city", "is_active"),
        *money_checks("default_rent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    manager_id: Mapped[int] = fk("users.id", ondelete="RESTRICT", index=False)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    city: Mapped[str] = mapped_column(sa.String(80), nullable=False, default="")
    address: Mapped[Optional[str]] = mapped_column(sa.String(300))
    phone: Mapped[Optional[str]] = mapped_column(sa.String(30))
    notes: Mapped[Optional[str]] = mapped_column(sa.Text)
    map_url: Mapped[Optional[str]] = mapped_column(sa.String(500))
    logistics: Mapped[dict] = json_col(dict)
    default_ceremony_types: Mapped[list] = json_col(list)
    default_rent: Mapped[Optional[int]] = money(nullable=True, default=None)
    single_atelier_per_day: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    manager: Mapped["User"] = relationship(back_populates="halls")  # noqa: F821
    events: Mapped[list["Event"]] = relationship(back_populates="hall")  # noqa: F821
    atelier_links: Mapped[list["AtelierHall"]] = relationship(
        back_populates="hall", cascade="all, delete-orphan", passive_deletes=True
    )
    photos: Mapped[list["HallPhoto"]] = relationship(
        back_populates="hall",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="HallPhoto.sort_order, HallPhoto.id",
    )
    agreements: Mapped[list["HallAgreement"]] = relationship(
        back_populates="hall",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="HallAgreement.start_date.desc(), HallAgreement.id.desc()",
    )

    @validates("phone")
    def _validate_phone(self, key: str, value: Any) -> Optional[str]:
        return clean_phone(value)

    @validates("default_rent")
    def _validate_default_rent(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("logistics")
    def _validate_logistics(self, key: str, value: Any) -> dict:
        """Keep only the known keys, as trimmed text of at most 300 characters (empty values dropped)."""
        if value is None:
            return {}
        if not hasattr(value, "items"):
            raise TypeError("logistics must be a mapping")
        clean: dict[str, str] = {}
        for name in HALL_LOGISTICS_KEYS:
            text = to_text(value.get(name))
            if text:
                clean[name] = text[:300]
        return clean

    @validates("default_ceremony_types")
    def _validate_ceremony_types(self, key: str, value: Any) -> list[str]:
        if value is None:
            return []
        seen: list[str] = []
        for item in value:
            member = CeremonyType.coerce(item).value
            if member not in seen:
                seen.append(member)
        return seen

    @property
    def ceremony_types(self) -> list[CeremonyType]:
        """The default ceremony types as enum members (stored as plain strings)."""
        return [CeremonyType.coerce(value) for value in (self.default_ceremony_types or [])]


def to_text(value: Any) -> str:
    """Trimmed ``str`` of *value* (``''`` for None)."""
    return "" if value is None else str(value).strip()


class HallPhoto(BaseModel, TimestampMixin):
    """One picture of a hall (stored through ``app.services.uploads`` with kind ``hall``)."""

    __tablename__ = "hall_photos"
    __table_args__ = (sa.Index("ix_hall_photos_hall_order", "hall_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hall_id: Mapped[int] = fk("halls.id", ondelete="CASCADE", index=False)
    image_path: Mapped[str] = mapped_column(sa.String(300), nullable=False, unique=True)
    caption: Mapped[Optional[str]] = mapped_column(sa.String(200))
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)

    hall: Mapped[Hall] = relationship(back_populates="photos")


class HallAgreement(BaseModel, TimestampMixin):
    """The commercial terms between a hall and an atelier (توافق‌نامهٔ تالار و اتلیه).

    A structured record instead of a paper contract: the period, whether the atelier is the
    hall's only one in that period (``is_exclusive``), rent per event *or* a revenue share,
    the deposit and a contact.  It is used by

    * the offer / claim workflow (``app.services.event_offers``): while an exclusive agreement
      covers a date only its atelier may be offered or claim events of the hall;
    * :meth:`Atelier.rent_for_hall`: ``rent_per_event`` pre-fills the invoice's venue rent;
    * the ``halls-agreement-reminders`` command: both managers are told before it ends.

    Active on a day *d* when it was not terminated, ``start_date <= d`` and
    ``end_date`` is empty or ``d <= end_date``.
    """

    __tablename__ = "hall_agreements"
    __table_args__ = (
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
        sa.CheckConstraint(
            "revenue_share_percent IS NULL OR (revenue_share_percent >= 0 AND revenue_share_percent <= 100)",
            name="share_range",
        ),
        *money_checks("rent_per_event", "deposit"),
        sa.Index("ix_hall_agreements_hall_period", "hall_id", "start_date", "end_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hall_id: Mapped[int] = fk("halls.id", ondelete="CASCADE", index=False)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE")
    start_date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    end_date: Mapped[Optional[dt.date]] = mapped_column(sa.Date)
    is_exclusive: Mapped[bool] = mapped_column(nullable=False, default=False)
    rent_per_event: Mapped[Optional[int]] = money(nullable=True, default=None)
    revenue_share_percent: Mapped[Optional[int]] = mapped_column()
    deposit: Mapped[Optional[int]] = money(nullable=True, default=None)
    contact_name: Mapped[Optional[str]] = mapped_column(sa.String(120))
    contact_phone: Mapped[Optional[str]] = mapped_column(sa.String(30))
    notes: Mapped[Optional[str]] = mapped_column(sa.Text)
    terminated_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    created_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    reminded_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)

    hall: Mapped[Hall] = relationship(back_populates="agreements")
    atelier: Mapped["Atelier"] = relationship()
    created_by: Mapped[Optional["User"]] = relationship()  # noqa: F821

    @validates("rent_per_event", "deposit")
    def _validate_money(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("revenue_share_percent")
    def _validate_share(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=0, maximum=100)

    @validates("contact_phone")
    def _validate_contact_phone(self, key: str, value: Any) -> Optional[str]:
        return clean_phone(value)

    def is_active_on(self, day: dt.date) -> bool:
        """True when the agreement covers *day* and was not terminated."""
        if self.terminated_at is not None or day < self.start_date:
            return False
        return self.end_date is None or day <= self.end_date

    @classmethod
    def active_on(cls, day: dt.date) -> Any:
        """SQL condition: the agreement covers *day* (use inside ``where``)."""
        return sa.and_(
            cls.terminated_at.is_(None),
            cls.start_date <= day,
            sa.or_(cls.end_date.is_(None), cls.end_date >= day),
        )


class Atelier(BaseModel, TimestampMixin):
    """A mobile photo studio (the employer / کارفرما).

    ``default_pay_presets`` holds suggested pay terms per role
    (``{"photographer": {"base", "commission_per_unit", "bonus_tiers"}, ...}``)
    used to pre-fill the afish form.  They are suggestions only; the agreed
    terms are snapshotted on each assignment.

    ``print_yield`` overrides ``constants.DEFAULT_PRINT_YIELD`` (assumption A4)
    when set.
    """

    __tablename__ = "ateliers"
    __table_args__ = (
        sa.UniqueConstraint("manager_id", "name", name="uq_ateliers_manager_name"),
        sa.Index("ix_ateliers_city_active", "city", "is_active"),
        *money_checks("default_venue_rent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    manager_id: Mapped[int] = fk("users.id", ondelete="RESTRICT", index=False)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    city: Mapped[str] = mapped_column(sa.String(80), nullable=False, default="")
    phone: Mapped[Optional[str]] = mapped_column(sa.String(30))
    description: Mapped[Optional[str]] = mapped_column(sa.Text)
    logo_path: Mapped[Optional[str]] = mapped_column(sa.String(300))
    default_venue_rent: Mapped[int] = money()
    default_pay_presets: Mapped[dict] = json_col(dict)
    print_yield: Mapped[Optional[dict]] = json_col(None, nullable=True)
    # Double-booking policy of the afish board: False = warn and allow an explicit override,
    # True = refuse to staff a person who is busy / marked the day unavailable.
    block_double_booking: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    manager: Mapped["User"] = relationship(back_populates="ateliers")  # noqa: F821
    hall_links: Mapped[list["AtelierHall"]] = relationship(
        back_populates="atelier", cascade="all, delete-orphan", passive_deletes=True
    )
    roster: Mapped[list["RosterMember"]] = relationship(
        back_populates="atelier",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RosterMember.display_name",
    )

    @validates("default_venue_rent")
    def _validate_rent(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("default_pay_presets")
    def _validate_presets(self, key: str, value: Any) -> dict:
        return normalize_terms_by_role(value)

    @validates("phone")
    def _validate_phone(self, key: str, value: Any) -> Optional[str]:
        return clean_phone(value)

    def preset_for(self, role: Any) -> Optional[dict]:
        """Suggested terms dict for *role*, or None."""
        presets = self.default_pay_presets or {}
        data = presets.get(Skill.coerce(role).value)
        return dict(data) if data else None

    def rent_for_hall(self, hall: Any, on: Optional[dt.date] = None) -> int:
        """Default venue rent when working in *hall* (a Hall or its id).

        Sources, the first one that is set wins (the user may still override the invoice line):

        1. the atelier's own per-hall price, ``AtelierHall.rent_amount``;
        2. ``rent_per_event`` of an agreement with that hall that is active on *on*
           (default: today) - the terms both sides signed;
        3. the hall's list price ``Hall.default_rent``;
        4. :attr:`default_venue_rent`, the atelier-wide default.
        """
        hall_id = getattr(hall, "id", hall)
        if self.id is not None and hall_id is not None:
            amount = db.session.execute(
                sa.select(AtelierHall.rent_amount).where(
                    AtelierHall.atelier_id == self.id,
                    AtelierHall.hall_id == hall_id,
                )
            ).scalar_one_or_none()
            if amount is not None:
                return amount
            day = on or dt.datetime.now(dt.timezone.utc).date()
            agreed = db.session.execute(
                sa.select(HallAgreement.rent_per_event)
                .where(
                    HallAgreement.atelier_id == self.id,
                    HallAgreement.hall_id == hall_id,
                    HallAgreement.rent_per_event.is_not(None),
                    HallAgreement.active_on(day),
                )
                .order_by(HallAgreement.start_date.desc(), HallAgreement.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if agreed is not None:
                return agreed
            listed = db.session.execute(sa.select(Hall.default_rent).where(Hall.id == hall_id)).scalar_one_or_none()
            if listed is not None:
                return listed
        return self.default_venue_rent or 0


class AtelierHall(BaseModel, TimestampMixin):
    """An atelier's relationship with a hall and the usual rent there.

    ``rent_amount`` NULL means "no special price, use the atelier default".
    """

    __tablename__ = "atelier_halls"
    __table_args__ = (
        sa.UniqueConstraint("atelier_id", "hall_id", name="uq_atelier_halls_pair"),
        sa.Index("ix_atelier_halls_hall", "hall_id"),
        *money_checks("rent_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE", index=False)
    hall_id: Mapped[int] = fk("halls.id", ondelete="CASCADE", index=False)
    rent_amount: Mapped[Optional[int]] = money(nullable=True, default=None)

    atelier: Mapped[Atelier] = relationship(back_populates="hall_links")
    hall: Mapped[Hall] = relationship(back_populates="atelier_links")

    @validates("rent_amount")
    def _validate_rent(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)


class RosterMember(BaseModel, TimestampMixin):
    """A member of an atelier's fixed staff (نیروی ثابت).

    May have no account (assumption A7): then only ``display_name`` and
    optionally ``mobile`` are known.  ``user_id`` links the person to an account
    when they have one, so their assignments show up in their own panel.

    ``skills`` is a JSON list of roles; ``default_terms`` maps role -> terms dict
    and only pre-fills the afish form (terms are snapshotted per assignment).
    """

    __tablename__ = "roster_members"
    __table_args__ = (
        sa.UniqueConstraint("atelier_id", "user_id", name="uq_roster_members_atelier_user"),
        sa.Index("ix_roster_members_mobile", "mobile"),
        sa.Index("ix_roster_members_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE", index=False)
    user_id: Mapped[Optional[int]] = fk(
        "users.id", ondelete="SET NULL", nullable=True, index=False
    )
    display_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    mobile: Mapped[Optional[str]] = mapped_column(sa.String(11))
    skills: Mapped[list] = json_col(list)
    default_terms: Mapped[dict] = json_col(dict)
    notes: Mapped[Optional[str]] = mapped_column(sa.String(500))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    atelier: Mapped[Atelier] = relationship(back_populates="roster")
    user: Mapped[Optional["User"]] = relationship()  # noqa: F821

    @validates("mobile")
    def _validate_mobile(self, key: str, value: Any) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return normalize_mobile(value)

    @validates("skills")
    def _validate_skills(self, key: str, value: Any) -> list[str]:
        return normalize_skills(value)

    @validates("default_terms")
    def _validate_default_terms(self, key: str, value: Any) -> dict:
        return normalize_terms_by_role(value)

    def can_do(self, role: Any) -> bool:
        return Skill.coerce(role).value in (self.skills or [])

    def terms_for(self, role: Any) -> Optional[dict]:
        """Default terms for *role*: own default, else the atelier preset, else None."""
        key = Skill.coerce(role).value
        own = (self.default_terms or {}).get(key)
        if own:
            return dict(own)
        return self.atelier.preset_for(key) if self.atelier is not None else None
