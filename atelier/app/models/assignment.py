"""Assignment ("afish"): one person staffed on one role of one event."""

import datetime as dt
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    choice,
    fk,
    money_checks,
)
from app.models.event import Event
from app.models.terms import TERMS_FIELDS, TermsMixin
from app.models.venue import RosterMember
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    AssignmentSource,
    AssignmentStatus,
    Skill,
)


class TermsLockedError(ValueError):
    """Raised when pay terms (or identity) of a locked assignment are changed.

    Terms are locked when an invoice using them is submitted; the lock keeps
    past payroll reproducible.  Unlock explicitly (``staffing.unlock_terms``)
    and flush before editing again.
    """


_ACTIVE_SQL = "status IN (%s)" % ", ".join(
    f"'{status.value}'" for status in sorted(ACTIVE_ASSIGNMENT_STATUSES)
)

# Columns that may not change while terms_locked_at is set.
LOCKED_FIELDS: tuple[str, ...] = (*TERMS_FIELDS, "event_id", "role", "user_id", "roster_member_id")


class Assignment(TermsMixin, BaseModel, TimestampMixin):
    """The afish: *who* works *which role* at *which event*, on *which terms*.

    Exactly one person: either a platform ``user`` or an atelier
    ``roster_member`` (CHECK).  The pay terms (``base_pay``,
    ``commission_per_unit``, ``bonus_tiers``) are a **snapshot** agreed when the
    assignment is made.  Later changes to the person's asking terms, the roster
    default or the atelier presets never touch existing assignments, so payroll
    of past events stays reproducible.  After invoice submission the snapshot is
    additionally *locked* (``terms_locked_at``) and enforced by an ORM guard.

    Partial unique indexes prevent the same person holding the same role twice
    on one event while the assignment is active; the same person *may* hold two
    different roles (e.g. photographer + seller) - each gets its own terms.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND roster_member_id IS NULL) "
            "OR (user_id IS NULL AND roster_member_id IS NOT NULL)",
            name="exactly_one_person",
        ),
        *money_checks("base_pay", "commission_per_unit"),
        sa.Index("ix_assignments_event_role", "event_id", "role"),
        sa.Index("ix_assignments_user_status", "user_id", "status"),
        sa.Index("ix_assignments_roster_status", "roster_member_id", "status"),
        sa.Index(
            "uq_assignments_active_user_role",
            "event_id",
            "role",
            "user_id",
            unique=True,
            sqlite_where=sa.text(f"user_id IS NOT NULL AND {_ACTIVE_SQL}"),
            postgresql_where=sa.text(f"user_id IS NOT NULL AND {_ACTIVE_SQL}"),
        ),
        sa.Index(
            "uq_assignments_active_roster_role",
            "event_id",
            "role",
            "roster_member_id",
            unique=True,
            sqlite_where=sa.text(f"roster_member_id IS NOT NULL AND {_ACTIVE_SQL}"),
            postgresql_where=sa.text(f"roster_member_id IS NOT NULL AND {_ACTIVE_SQL}"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = fk("events.id", ondelete="CASCADE", index=False)
    role: Mapped[Skill] = mapped_column(choice(Skill, "role"), nullable=False)
    user_id: Mapped[Optional[int]] = fk(
        "users.id", ondelete="RESTRICT", nullable=True, index=False
    )
    roster_member_id: Mapped[Optional[int]] = fk(
        "roster_members.id", ondelete="RESTRICT", nullable=True, index=False
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        choice(AssignmentStatus, "status"), nullable=False, default=AssignmentStatus.INVITED
    )
    call_time: Mapped[Optional[dt.time]] = mapped_column(sa.Time)
    notes: Mapped[Optional[str]] = mapped_column(sa.Text)
    source: Mapped[AssignmentSource] = mapped_column(
        choice(AssignmentSource, "source"), nullable=False, default=AssignmentSource.DIRECT
    )
    job_application_id: Mapped[Optional[int]] = fk(
        "job_applications.id", ondelete="SET NULL", nullable=True
    )
    created_by_id: Mapped[Optional[int]] = fk("users.id", ondelete="SET NULL", nullable=True)
    responded_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    terms_locked_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)

    event: Mapped[Event] = relationship(back_populates="assignments")
    user: Mapped[Optional["User"]] = relationship(foreign_keys=[user_id])  # noqa: F821
    roster_member: Mapped[Optional[RosterMember]] = relationship()
    created_by: Mapped[Optional["User"]] = relationship(  # noqa: F821
        foreign_keys=[created_by_id]
    )
    job_application: Mapped[Optional["JobApplication"]] = relationship()  # noqa: F821
    payroll_lines: Mapped[list["PayrollLine"]] = relationship(  # noqa: F821
        back_populates="assignment"
    )

    # -- who ----------------------------------------------------------------

    @property
    def person_user_id(self) -> Optional[int]:
        """The account of the assigned person (directly or via the roster)."""
        if self.user_id is not None:
            return self.user_id
        if self.roster_member is not None:
            return self.roster_member.user_id
        return None

    @property
    def person_name(self) -> str:
        if self.roster_member is not None:
            return self.roster_member.display_name
        if self.user is not None:
            return self.user.display_name
        return ""

    @property
    def person_mobile(self) -> Optional[str]:
        if self.roster_member is not None:
            return self.roster_member.mobile or (
                self.roster_member.user.mobile if self.roster_member.user else None
            )
        return self.user.mobile if self.user is not None else None

    @classmethod
    def involving_user(cls, user_id: int):
        """SQL condition: assignments of *user_id*, direct or via a roster entry."""
        return sa.or_(
            cls.user_id == user_id,
            cls.roster_member_id.in_(
                sa.select(RosterMember.id).where(RosterMember.user_id == user_id)
            ),
        )

    # -- state --------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Still counts toward staffing / the person's calendar."""
        return self.status in ACTIVE_ASSIGNMENT_STATUSES

    @property
    def is_terms_locked(self) -> bool:
        return self.terms_locked_at is not None


@event.listens_for(Assignment, "before_update")
def _protect_locked_assignment(mapper: Any, connection: Any, target: Assignment) -> None:
    """Hard guard: a locked assignment's terms and identity cannot change.

    Runs before every UPDATE of an assignment.  Only when a protected column
    shows a pending change does it read the *stored* row (so a stale in-memory
    copy cannot bypass a lock set by another request) and compare.
    """
    state = sa.inspect(target)
    if not any(state.attrs[name].history.has_changes() for name in LOCKED_FIELDS):
        return
    table = Assignment.__table__
    stored = connection.execute(
        sa.select(*(table.c[name] for name in LOCKED_FIELDS), table.c.terms_locked_at).where(
            table.c.id == target.id
        )
    ).one_or_none()
    if stored is None or stored._mapping["terms_locked_at"] is None:
        return  # not locked in the database
    changed = [
        name for name in LOCKED_FIELDS if getattr(target, name) != stored._mapping[name]
    ]
    if changed:
        raise TermsLockedError(
            f"assignment {target.id} is locked; cannot change {', '.join(changed)}"
        )
