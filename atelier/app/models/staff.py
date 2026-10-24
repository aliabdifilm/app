"""Marketplace-side staff data: profile, skills, portfolio, availability."""

import datetime as dt
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.extensions import db
from app.models.base import (
    BaseModel,
    TimestampMixin,
    check_int,
    check_money,
    choice,
    fk,
    money,
    money_checks,
)
from app.models.terms import normalize_skills
from app.services.constants import AvailabilityStatus, Skill


class StaffSkill(BaseModel):
    """One skill of a staff profile (``photographer`` ...).

    A child table instead of a JSON list so the marketplace can filter
    ("photographers in Shiraz") with an indexed, portable query.
    """

    __tablename__ = "staff_skills"
    __table_args__ = (
        sa.UniqueConstraint("profile_id", "skill", name="uq_staff_skills_profile_skill"),
        sa.Index("ix_staff_skills_skill", "skill"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = fk("staff_profiles.id", ondelete="CASCADE", index=False)
    skill: Mapped[Skill] = mapped_column(choice(Skill, "skill"), nullable=False)

    profile: Mapped["StaffProfile"] = relationship(back_populates="skill_rows")


class StaffProfile(BaseModel, TimestampMixin):
    """What a staff member offers on the marketplace (one per user).

    ``expected_*`` are the person's asking terms, shown to ateliers as a hint.
    They are *not* applied to anything: pay terms are snapshotted per
    assignment (see :class:`~app.models.assignment.Assignment`).
    """

    __tablename__ = "staff_profiles"
    __table_args__ = (
        sa.CheckConstraint("experience_years >= 0", name="experience_nonneg"),
        sa.CheckConstraint("rating_count >= 0", name="rating_count_nonneg"),
        sa.CheckConstraint("rating_avg >= 0 AND rating_avg <= 5", name="rating_range"),
        sa.Index("ix_staff_profiles_listed_city", "is_listed", "city"),
        *money_checks("expected_base", "expected_commission_per_unit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", unique=True)
    bio: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    experience_years: Mapped[int] = mapped_column(nullable=False, default=0)
    city: Mapped[Optional[str]] = mapped_column(sa.String(80))
    instagram: Mapped[Optional[str]] = mapped_column(sa.String(80))

    expected_base: Mapped[Optional[int]] = money(nullable=True, default=None)
    expected_commission_per_unit: Mapped[Optional[int]] = money(nullable=True, default=None)

    owns_camera: Mapped[bool] = mapped_column(nullable=False, default=False)
    owns_flash: Mapped[bool] = mapped_column(nullable=False, default=False)
    owns_lens: Mapped[bool] = mapped_column(nullable=False, default=False)
    owns_phone_light: Mapped[bool] = mapped_column(nullable=False, default=False)
    owns_printer: Mapped[bool] = mapped_column(nullable=False, default=False)
    owns_laptop: Mapped[bool] = mapped_column(nullable=False, default=False)

    is_listed: Mapped[bool] = mapped_column(nullable=False, default=False)
    rating_avg: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    rating_count: Mapped[int] = mapped_column(nullable=False, default=0)

    user: Mapped["User"] = relationship(back_populates="staff_profile")  # noqa: F821
    skill_rows: Mapped[list[StaffSkill]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="StaffSkill.id",
    )

    @validates("expected_base", "expected_commission_per_unit")
    def _validate_money(self, key: str, value: Any) -> Optional[int]:
        return check_money(value, key)

    @validates("experience_years")
    def _validate_experience(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=0, maximum=80)

    # -- skills -------------------------------------------------------------

    @property
    def skills(self) -> list[str]:
        """Skill values in canonical order (``['photographer', 'seller']``)."""
        present = {Skill.coerce(row.skill) for row in self.skill_rows}
        return [member.value for member in Skill if member in present]

    @skills.setter
    def skills(self, values: Iterable[Any]) -> None:
        wanted = normalize_skills(values)
        for row in list(self.skill_rows):
            if Skill.coerce(row.skill).value not in wanted:
                self.skill_rows.remove(row)
        have = {Skill.coerce(row.skill).value for row in self.skill_rows}
        for value in wanted:
            if value not in have:
                self.skill_rows.append(StaffSkill(skill=Skill(value)))

    def has_skill(self, skill: Any) -> bool:
        return Skill.coerce(skill).value in self.skills

    @classmethod
    def with_skill(cls, skill: Any):
        """SQL expression: profiles that have *skill* (for ``.where(...)``)."""
        return cls.skill_rows.any(StaffSkill.skill == Skill.coerce(skill))

    # -- rating -------------------------------------------------------------

    def refresh_rating(self) -> None:
        """Recompute ``rating_avg`` / ``rating_count`` from visible reviews."""
        from app.models.marketplace import Review

        average, count = db.session.execute(
            sa.select(sa.func.avg(Review.rating), sa.func.count(Review.id)).where(
                Review.subject_id == self.user_id,
                Review.is_hidden.is_(False),
            )
        ).one()
        self.rating_count = int(count or 0)
        self.rating_avg = round(float(average), 2) if average is not None else 0.0

    def expected_terms(self) -> dict[str, int | None]:
        return {
            "base": self.expected_base,
            "commission_per_unit": self.expected_commission_per_unit,
        }


class PortfolioItem(BaseModel, TimestampMixin):
    """A sample work of a staff member (stored file path + caption)."""

    __tablename__ = "portfolio_items"
    __table_args__ = (sa.Index("ix_portfolio_items_user_sort", "user_id", "sort_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=False)
    title: Mapped[str] = mapped_column(sa.String(150), nullable=False, default="")
    caption: Mapped[Optional[str]] = mapped_column(sa.String(500))
    image_path: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    is_public: Mapped[bool] = mapped_column(nullable=False, default=True)

    user: Mapped["User"] = relationship()  # noqa: F821


class StaffAvailability(BaseModel, TimestampMixin):
    """A day on which a staff member explicitly set (un)availability.

    Absence of a row means "unknown / probably free".  The pair
    (user, date) is unique so a day has at most one state.
    """

    __tablename__ = "staff_availability"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "date", name="uq_staff_availability_user_date"),
        sa.Index("ix_staff_availability_date_status", "date", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=False)
    date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[AvailabilityStatus] = mapped_column(
        choice(AvailabilityStatus, "status"),
        nullable=False,
        default=AvailabilityStatus.UNAVAILABLE,
    )
    note: Mapped[Optional[str]] = mapped_column(sa.String(300))

    user: Mapped["User"] = relationship()  # noqa: F821
