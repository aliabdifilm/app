"""Read side of the staff panel.

Every query starts from :meth:`Assignment.involving_user`, i.e. "the afish rows of *this* account, directly
or through a roster entry linked to it", so nothing here can return another person's data.  Lists load the
event, hall and atelier with the rows (no N+1) and are paginated by the routes.
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, JobApplication, JobPost, PortfolioItem, Review, StaffProfile
from app.services.constants import (
    COMMITTED_ASSIGNMENT_STATUSES,
    AssignmentStatus,
    EventStatus,
)

CLOSED_EVENT = (EventStatus.CANCELLED, EventStatus.DONE, EventStatus.INVOICED)
#: Statuses of the people who are (still) going to work: they appear in the schedule.
SCHEDULE_STATUSES = (AssignmentStatus.INVITED, AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED)

_EAGER = (
    joinedload(Assignment.event).joinedload(Event.hall),
    joinedload(Assignment.event).joinedload(Event.atelier),
)


def _mine(user_id: int) -> sa.Select:
    return (
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(Assignment.involving_user(user_id))
        .options(*_EAGER)
    )


# -- invitations -----------------------------------------------------------------


def _pending(today: dt.date):
    return sa.and_(
        Assignment.status == AssignmentStatus.INVITED,
        Event.date >= today,
        Event.status.not_in(CLOSED_EVENT),
    )


def pending_invitation_count(user_id: int, today: dt.date) -> int:
    """Invitations waiting for an answer (also the badge of the menu: one COUNT per page)."""
    return int(
        db.session.scalar(
            sa.select(sa.func.count(Assignment.id))
            .join(Event, Event.id == Assignment.event_id)
            .where(Assignment.involving_user(user_id), _pending(today))
        )
        or 0
    )


def invitations_select(user_id: int, today: dt.date) -> sa.Select:
    """Open invitations, the nearest date first."""
    return _mine(user_id).where(_pending(today)).order_by(Event.date, Event.start_time, Assignment.id)


def _history(today: dt.date):
    expired = sa.and_(
        Assignment.status == AssignmentStatus.INVITED,
        sa.or_(Event.date < today, Event.status.in_(CLOSED_EVENT)),
    )
    answered = Assignment.status.in_([AssignmentStatus.DECLINED, AssignmentStatus.CANCELLED])
    return sa.or_(expired, answered)


def invitation_history_select(user_id: int, today: dt.date) -> sa.Select:
    """Invitations that are over: declined, cancelled, or never answered before the date passed."""
    return _mine(user_id).where(_history(today)).order_by(Event.date.desc(), Assignment.id.desc())


def invitation_counts(user_id: int, today: dt.date) -> tuple[int, int]:
    """``(pending, history)`` row counts for the two tabs."""
    history = db.session.scalar(
        sa.select(sa.func.count(Assignment.id))
        .join(Event, Event.id == Assignment.event_id)
        .where(Assignment.involving_user(user_id), _history(today))
    )
    return pending_invitation_count(user_id, today), int(history or 0)


# -- schedule --------------------------------------------------------------------


def schedule_between(user_id: int, first: dt.date, last: dt.date) -> list[Assignment]:
    """Assignments (not declined / cancelled) of events from *first* to *last*, in date order."""
    stmt = (
        _mine(user_id)
        .where(
            Event.date.between(first, last),
            Assignment.status.in_([*SCHEDULE_STATUSES, AssignmentStatus.COMPLETED]),
            Event.status != EventStatus.CANCELLED,
        )
        .order_by(Event.date, Event.start_time, Assignment.id)
    )
    return list(db.session.execute(stmt).scalars().unique())


def schedule_select(user_id: int, today: dt.date, when: str) -> sa.Select:
    """The agenda: ``upcoming`` (soonest first) or ``past`` (latest first)."""
    stmt = _mine(user_id).where(Event.status != EventStatus.CANCELLED)
    if when == "past":
        return stmt.where(
            Assignment.status.in_([*SCHEDULE_STATUSES, AssignmentStatus.COMPLETED]),
            sa.or_(Event.date < today, Assignment.status == AssignmentStatus.COMPLETED),
        ).order_by(Event.date.desc(), Assignment.id.desc())
    return stmt.where(
        Assignment.status.in_(SCHEDULE_STATUSES),
        Event.date >= today,
    ).order_by(Event.date, Event.start_time, Assignment.id)


def upcoming_calendar_rows(user_id: int, today: dt.date, limit: int = 200) -> list[Assignment]:
    """Committed or tentative future afish rows (the .ics download of the whole schedule)."""
    stmt = (
        _mine(user_id)
        .where(Event.date >= today, Event.status != EventStatus.CANCELLED, Assignment.status.in_(SCHEDULE_STATUSES))
        .order_by(Event.date, Event.start_time, Assignment.id)
        .limit(limit)
    )
    return list(db.session.execute(stmt).scalars().unique())


def next_assignment(user_id: int, today: dt.date) -> Optional[Assignment]:
    """The nearest future afish row the person said yes to (accepted or confirmed)."""
    stmt = (
        _mine(user_id)
        .where(
            Event.date >= today,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_([AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED]),
        )
        .order_by(Event.date, Event.start_time, Assignment.id)
        .limit(1)
    )
    return db.session.execute(stmt).scalars().unique().first()


def upcoming_committed(user_id: int, today: dt.date, limit: int = 4) -> list[Assignment]:
    stmt = (
        _mine(user_id)
        .where(
            Event.date >= today,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
        )
        .order_by(Event.date, Event.start_time, Assignment.id)
        .limit(limit)
    )
    return list(db.session.execute(stmt).scalars().unique())


def committed_counts(user_id: int, today: dt.date) -> tuple[int, int]:
    """``(upcoming committed, completed)`` afish rows for the dashboard."""
    upcoming = db.session.scalar(
        sa.select(sa.func.count(Assignment.id))
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Assignment.involving_user(user_id),
            Event.date >= today,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_([AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED]),
        )
    )
    done = db.session.scalar(
        sa.select(sa.func.count(Assignment.id)).where(
            Assignment.involving_user(user_id), Assignment.status == AssignmentStatus.COMPLETED
        )
    )
    return int(upcoming or 0), int(done or 0)


def teammates(assignment: Assignment) -> list[Assignment]:
    """The other committed people on the same event (names and roles only; never their pay)."""
    stmt = (
        sa.select(Assignment)
        .where(
            Assignment.event_id == assignment.event_id,
            Assignment.id != assignment.id,
            Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
        )
        .options(joinedload(Assignment.user), joinedload(Assignment.roster_member))
        .order_by(Assignment.id)
    )
    return list(db.session.execute(stmt).scalars().unique())


def same_day_others(user_id: int, assignment: Assignment) -> list[Assignment]:
    """My other active afish rows on the same date (the double-booking hint)."""
    stmt = (
        _mine(user_id)
        .where(
            Event.date == assignment.event.date,
            Assignment.id != assignment.id,
            Assignment.status.in_([*SCHEDULE_STATUSES]),
            Event.status != EventStatus.CANCELLED,
        )
        .order_by(Event.start_time, Assignment.id)
    )
    return list(db.session.execute(stmt).scalars().unique())


def dates_with_assignments(rows: Iterable[Assignment]) -> dict[dt.date, int]:
    """``{date: how many of these rows fall on it}`` - rows are already loaded, this is pure Python."""
    counts: dict[dt.date, int] = {}
    for row in rows:
        counts[row.event.date] = counts.get(row.event.date, 0) + 1
    return counts


# -- applications, reviews, portfolio --------------------------------------------------


def applications_select(user_id: int) -> sa.Select:
    return (
        sa.select(JobApplication)
        .where(JobApplication.user_id == user_id)
        .options(joinedload(JobApplication.post).joinedload(JobPost.atelier))
        .order_by(JobApplication.created_at.desc(), JobApplication.id.desc())
    )


def reviews_select(user_id: int) -> sa.Select:
    """Reviews written about me (visible ones only) on afish rows - hall reviews of ateliers are not mine."""
    return (
        sa.select(Review)
        .where(
            Review.subject_id == user_id,
            Review.is_hidden.is_(False),
            Review.assignment_id.is_not(None),
        )
        .options(
            joinedload(Review.author),
            joinedload(Review.assignment).joinedload(Assignment.event).joinedload(Event.hall),
        )
        .order_by(Review.created_at.desc(), Review.id.desc())
    )


def portfolio_items(user_id: int) -> list[PortfolioItem]:
    return list(
        db.session.execute(
            sa.select(PortfolioItem)
            .where(PortfolioItem.user_id == user_id)
            .order_by(PortfolioItem.sort_order, PortfolioItem.id)
        ).scalars()
    )


def portfolio_count(user_id: int) -> int:
    return int(
        db.session.scalar(sa.select(sa.func.count(PortfolioItem.id)).where(PortfolioItem.user_id == user_id)) or 0
    )


def get_profile(user_id: int) -> Optional[StaffProfile]:
    return db.session.scalar(sa.select(StaffProfile).where(StaffProfile.user_id == user_id))
