"""Job marketplace: posts, applications, reviews."""

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
    money_checks,
)
from app.models.terms import TermsMixin
from app.services.constants import (
    MAX_RATING,
    MIN_RATING,
    ApplicationStatus,
    JobPostStatus,
    Skill,
)


class JobPost(TermsMixin, BaseModel, TimestampMixin):
    """An atelier's public call for staff ("آگهی همکاری").

    Carries the *offered* pay terms (same shape as an assignment's snapshot);
    when an application is accepted these terms are copied into the new
    :class:`~app.models.assignment.Assignment`.  ``slots`` is the number of
    people wanted; the post becomes ``filled`` when that many are accepted.
    """

    __tablename__ = "job_posts"
    __table_args__ = (
        sa.CheckConstraint("slots >= 1", name="slots_positive"),
        *money_checks("base_pay", "commission_per_unit"),
        sa.Index("ix_job_posts_status_date", "status", "date"),
        sa.Index("ix_job_posts_city_role", "city", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    atelier_id: Mapped[int] = fk("ateliers.id", ondelete="CASCADE")
    event_id: Mapped[Optional[int]] = fk("events.id", ondelete="SET NULL", nullable=True)
    role: Mapped[Skill] = mapped_column(choice(Skill, "role"), nullable=False)
    date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    city: Mapped[str] = mapped_column(sa.String(80), nullable=False, default="")
    title: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text)
    slots: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[JobPostStatus] = mapped_column(
        choice(JobPostStatus, "status"), nullable=False, default=JobPostStatus.OPEN
    )

    atelier: Mapped["Atelier"] = relationship()  # noqa: F821
    event: Mapped[Optional["Event"]] = relationship()  # noqa: F821
    applications: Mapped[list["JobApplication"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("slots")
    def _validate_slots(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=1, maximum=50)

    @property
    def accepted_count(self) -> int:
        return sum(1 for app in self.applications if app.status == ApplicationStatus.ACCEPTED)

    @property
    def is_full(self) -> bool:
        return self.accepted_count >= self.slots


class JobApplication(BaseModel, TimestampMixin):
    """A staff member applying to a :class:`JobPost` (one per post and user)."""

    __tablename__ = "job_applications"
    __table_args__ = (
        sa.UniqueConstraint("post_id", "user_id", name="uq_job_applications_post_user"),
        sa.Index("ix_job_applications_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = fk("job_posts.id", ondelete="CASCADE", index=False)
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=False)
    message: Mapped[Optional[str]] = mapped_column(sa.Text)
    status: Mapped[ApplicationStatus] = mapped_column(
        choice(ApplicationStatus, "status"), nullable=False, default=ApplicationStatus.PENDING
    )
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)

    post: Mapped[JobPost] = relationship(back_populates="applications")
    user: Mapped["User"] = relationship()  # noqa: F821


class Review(BaseModel, TimestampMixin):
    """A 1-5 star review (one per author and target).

    Two kinds of target:

    * **staff review** - attached to an ``assignment``: the atelier manager reviews the
      person (``subject`` = staff user) and the person reviews the atelier manager;
    * **event review** - ``assignment_id`` empty, ``event_id`` and ``atelier_id`` set: the
      hall manager rates the atelier that worked an event (``subject`` = the atelier's
      manager).  One per (event, author); the ``halls`` module writes and averages them.

    Hidden reviews (admin moderation) are excluded from ratings; see
    ``StaffProfile.refresh_rating``.
    """

    __tablename__ = "reviews"
    __table_args__ = (
        sa.UniqueConstraint("assignment_id", "author_id", name="uq_reviews_assignment_author"),
        sa.UniqueConstraint("event_id", "author_id", name="uq_reviews_event_author"),
        sa.CheckConstraint(
            f"rating >= {MIN_RATING} AND rating <= {MAX_RATING}", name="rating_range"
        ),
        sa.CheckConstraint("author_id <> subject_id", name="no_self_review"),
        sa.CheckConstraint(
            "assignment_id IS NOT NULL OR (event_id IS NOT NULL AND atelier_id IS NOT NULL)",
            name="has_target",
        ),
        sa.Index("ix_reviews_subject_hidden", "subject_id", "is_hidden"),
        sa.Index("ix_reviews_atelier_hidden", "atelier_id", "is_hidden"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[Optional[int]] = fk(
        "assignments.id", ondelete="CASCADE", nullable=True, index=False
    )
    event_id: Mapped[Optional[int]] = fk("events.id", ondelete="CASCADE", nullable=True, index=False)
    atelier_id: Mapped[Optional[int]] = fk("ateliers.id", ondelete="CASCADE", nullable=True, index=False)
    author_id: Mapped[int] = fk("users.id", ondelete="CASCADE")
    subject_id: Mapped[int] = fk("users.id", ondelete="CASCADE", index=False)
    rating: Mapped[int] = mapped_column(nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(sa.Text)
    is_hidden: Mapped[bool] = mapped_column(nullable=False, default=False)

    assignment: Mapped[Optional["Assignment"]] = relationship()  # noqa: F821
    event: Mapped[Optional["Event"]] = relationship()  # noqa: F821
    atelier: Mapped[Optional["Atelier"]] = relationship()  # noqa: F821
    author: Mapped["User"] = relationship(foreign_keys=[author_id])  # noqa: F821
    subject: Mapped["User"] = relationship(foreign_keys=[subject_id])  # noqa: F821

    @validates("rating")
    def _validate_rating(self, key: str, value: Any) -> Optional[int]:
        return check_int(value, key, minimum=MIN_RATING, maximum=MAX_RATING)
