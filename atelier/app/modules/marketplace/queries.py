"""Read-side queries of the marketplace.

Every list is built as one ``select`` (scoped to the actor in SQL, never filtered in Python after
loading), and the numbers a page needs besides the rows - accepted counts, applicant counts, atelier
ratings - are fetched with **one grouped query per page** (no N+1).

Visibility rules implemented here:

* the public board shows only ``open`` posts of active ateliers whose date has not passed and whose
  linked event (if any) is still staffable;
* the talent directory shows only active staff who switched ``is_listed`` on;
* a staff profile that is *not* listed can still be opened by the atelier that has an application
  from, or an assignment / invitation for, that person (``Relation.connected``);
* contact details are revealed only for ``Relation.contact`` (an accepted application or a
  committed assignment between the two parties).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from app.extensions import db
from app.models import (
    Assignment,
    Atelier,
    Event,
    Hall,
    JobApplication,
    JobPost,
    PortfolioItem,
    Review,
    RosterMember,
    StaffAvailability,
    StaffProfile,
    User,
)
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    CLOSED_EVENT_STATUSES,
    COMMITTED_ASSIGNMENT_STATUSES,
    ApplicationStatus,
    AssignmentStatus,
    AvailabilityStatus,
    EventStatus,
    JobPostStatus,
    Role,
    UserStatus,
)

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def like_pattern(text: str) -> str:
    """``%text%`` with LIKE wildcards in *text* escaped (use with ``escape='\\'``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True)
class Rating:
    average: float
    count: int

    def __bool__(self) -> bool:
        return self.count > 0


NO_RATING = Rating(0.0, 0)


# --------------------------------------------------------------------------
# the public job board
# --------------------------------------------------------------------------


def _open_conditions(today: dt.date) -> list[Any]:
    """WHERE parts that make a post publicly listable (needs the Atelier, User and Event joins)."""
    return [
        JobPost.status == JobPostStatus.OPEN,
        JobPost.date >= today,
        Atelier.is_active.is_(True),
        User.status != UserStatus.SUSPENDED,
        sa.or_(JobPost.event_id.is_(None), Event.status.notin_(list(CLOSED_EVENT_STATUSES))),
    ]


def _joined_posts(stmt: sa.Select) -> sa.Select:
    return (
        stmt.join(Atelier, Atelier.id == JobPost.atelier_id)
        .join(User, User.id == Atelier.manager_id)
        .outerjoin(Event, Event.id == JobPost.event_id)
    )


def board_select(
    today: dt.date,
    *,
    city: Optional[str] = None,
    role: Any = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
) -> sa.Select:
    """Open, future, listable posts matching the filters, soonest first."""
    stmt = _joined_posts(sa.select(JobPost)).where(*_open_conditions(today))
    if city:
        stmt = stmt.where(JobPost.city == city)
    if role:
        stmt = stmt.where(JobPost.role == role)
    if date_from:
        stmt = stmt.where(JobPost.date >= date_from)
    if date_to:
        stmt = stmt.where(JobPost.date <= date_to)
    return stmt.options(
        contains_eager(JobPost.atelier).contains_eager(Atelier.manager),
        contains_eager(JobPost.event).joinedload(Event.hall),
    )


def board_cities(today: dt.date) -> list[str]:
    """Cities that have at least one listable post (options of the city filter)."""
    stmt = (
        _joined_posts(sa.select(JobPost.city))
        .where(*_open_conditions(today), JobPost.city != "")
        .distinct()
        .order_by(JobPost.city)
    )
    return list(db.session.execute(stmt).scalars())


def accepted_counts(post_ids: Iterable[int]) -> dict[int, int]:
    """``{post_id: accepted applications}`` for the given posts (missing = 0)."""
    ids = sorted(set(post_ids))
    if not ids:
        return {}
    rows = db.session.execute(
        sa.select(JobApplication.post_id, sa.func.count(JobApplication.id))
        .where(JobApplication.post_id.in_(ids), JobApplication.status == ApplicationStatus.ACCEPTED)
        .group_by(JobApplication.post_id)
    )
    return {post_id: int(count) for post_id, count in rows}


def application_counts(post_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """``{post_id: {status: count}}`` - one grouped query for a whole page of posts."""
    ids = sorted(set(post_ids))
    result: dict[int, dict[str, int]] = {post_id: {} for post_id in ids}
    if not ids:
        return result
    rows = db.session.execute(
        sa.select(JobApplication.post_id, JobApplication.status, sa.func.count(JobApplication.id))
        .where(JobApplication.post_id.in_(ids))
        .group_by(JobApplication.post_id, JobApplication.status)
    )
    for post_id, status, count in rows:
        result[post_id][ApplicationStatus.coerce(status).value] = int(count)
    return result


def count_accepted(post_id: int) -> int:
    """Accepted applications of one post, straight from the database (never a stale collection)."""
    return int(
        db.session.scalar(
            sa.select(sa.func.count(JobApplication.id)).where(
                JobApplication.post_id == post_id, JobApplication.status == ApplicationStatus.ACCEPTED
            )
        )
        or 0
    )


def atelier_ratings(atelier_ids: Iterable[int]) -> dict[int, Rating]:
    """Average star rating the ateliers received from staff (visible reviews only).

    A review about an atelier is one whose ``subject`` is the atelier's manager and whose assignment
    belongs to an event of that atelier; the average is computed on the fly (there is no stored
    atelier rating), one grouped query for any number of ateliers.
    """
    ids = sorted(set(atelier_ids))
    if not ids:
        return {}
    rows = db.session.execute(
        sa.select(Event.atelier_id, sa.func.avg(Review.rating), sa.func.count(Review.id))
        .join(Assignment, Assignment.id == Review.assignment_id)
        .join(Event, Event.id == Assignment.event_id)
        .join(Atelier, Atelier.id == Event.atelier_id)
        .where(
            Event.atelier_id.in_(ids),
            Review.subject_id == Atelier.manager_id,
            Review.is_hidden.is_(False),
        )
        .group_by(Event.atelier_id)
    )
    return {atelier_id: Rating(round(float(avg), 2), int(count)) for atelier_id, avg, count in rows}


def get_visible_post(post_id: int, viewer: Any = None) -> Optional[JobPost]:
    """A post for its public page, or ``None`` (-> 404).

    Closed / filled / expired posts stay reachable (a link shared in a messenger should explain that
    the call is over); ``hidden`` ones (moderated by an admin) only for their owner and admins; posts
    of an inactive atelier or a suspended manager are gone.
    """
    post = db.session.scalar(
        sa.select(JobPost)
        .where(JobPost.id == post_id)
        .options(
            joinedload(JobPost.atelier).joinedload(Atelier.manager),
            joinedload(JobPost.event).joinedload(Event.hall),
        )
    )
    if post is None:
        return None
    atelier = post.atelier
    if not atelier.is_active or atelier.manager.status == UserStatus.SUSPENDED:
        return None
    if post.status == JobPostStatus.HIDDEN:
        is_owner = viewer is not None and atelier.manager_id == getattr(viewer, "id", None)
        is_admin = viewer is not None and getattr(viewer, "role", None) == Role.ADMIN
        if not (is_owner or is_admin):
            return None
    return post


# --------------------------------------------------------------------------
# the atelier manager's side
# --------------------------------------------------------------------------

MANAGE_TABS = ("open", "filled", "closed", "all")


def manage_select(atelier_ids: Sequence[int], tab: str, today: dt.date) -> sa.Select:
    """The posts of the manager's ateliers for one tab of ``/jobs/manage``."""
    stmt = sa.select(JobPost).where(JobPost.atelier_id.in_(sorted(atelier_ids)))
    if tab == "open":
        stmt = stmt.where(JobPost.status == JobPostStatus.OPEN, JobPost.date >= today)
    elif tab == "filled":
        stmt = stmt.where(JobPost.status == JobPostStatus.FILLED)
    elif tab == "closed":
        stmt = stmt.where(
            sa.or_(
                JobPost.status.in_([JobPostStatus.CLOSED, JobPostStatus.HIDDEN]),
                sa.and_(JobPost.status == JobPostStatus.OPEN, JobPost.date < today),
            )
        )
    return stmt.options(
        joinedload(JobPost.atelier).joinedload(Atelier.manager),
        joinedload(JobPost.event).joinedload(Event.hall),
    ).order_by(JobPost.date.desc(), JobPost.id.desc())


def manage_tab_counts(atelier_ids: Sequence[int], today: dt.date) -> dict[str, int]:
    """Badge numbers of the tabs (one query)."""
    ids = sorted(atelier_ids)
    if not ids:
        return {tab: 0 for tab in MANAGE_TABS}
    expired = sa.and_(JobPost.status == JobPostStatus.OPEN, JobPost.date < today)
    rows = db.session.execute(
        sa.select(
            sa.func.count(JobPost.id),
            sa.func.sum(sa.case((sa.and_(JobPost.status == JobPostStatus.OPEN, JobPost.date >= today), 1), else_=0)),
            sa.func.sum(sa.case((JobPost.status == JobPostStatus.FILLED, 1), else_=0)),
            sa.func.sum(
                sa.case((sa.or_(JobPost.status.in_([JobPostStatus.CLOSED, JobPostStatus.HIDDEN]), expired), 1), else_=0)
            ),
        ).where(JobPost.atelier_id.in_(ids))
    ).one()
    total, open_, filled, closed = (int(value or 0) for value in rows)
    return {"all": total, "open": open_, "filled": filled, "closed": closed}


INBOX_TABS = ("pending", "accepted", "rejected", "withdrawn", "all")


def inbox_select(atelier_ids: Sequence[int], tab: str, post_id: Optional[int] = None) -> sa.Select:
    """Applications to the posts of the manager's ateliers (optionally for one post), newest first."""
    stmt = (
        sa.select(JobApplication)
        .join(JobPost, JobPost.id == JobApplication.post_id)
        .where(JobPost.atelier_id.in_(sorted(atelier_ids)))
        .options(
            contains_eager(JobApplication.post),
            joinedload(JobApplication.user).selectinload(User.staff_profile).selectinload(StaffProfile.skill_rows),
        )
        .order_by(JobApplication.created_at.desc(), JobApplication.id.desc())
    )
    if tab in {"pending", "accepted", "rejected", "withdrawn"}:
        stmt = stmt.where(JobApplication.status == ApplicationStatus.coerce(tab))
    if post_id is not None:
        stmt = stmt.where(JobApplication.post_id == post_id)
    return stmt


def inbox_counts(atelier_ids: Sequence[int], post_id: Optional[int] = None) -> dict[str, int]:
    """``{status: count}`` for the inbox tabs, plus ``all``."""
    ids = sorted(atelier_ids)
    counts = {tab: 0 for tab in INBOX_TABS}
    if not ids:
        return counts
    stmt = (
        sa.select(JobApplication.status, sa.func.count(JobApplication.id))
        .join(JobPost, JobPost.id == JobApplication.post_id)
        .where(JobPost.atelier_id.in_(ids))
        .group_by(JobApplication.status)
    )
    if post_id is not None:
        stmt = stmt.where(JobApplication.post_id == post_id)
    for status, count in db.session.execute(stmt):
        counts[ApplicationStatus.coerce(status).value] = int(count)
    counts["all"] = sum(counts[tab] for tab in INBOX_TABS if tab != "all")
    return counts


def pending_applicant_count(manager_id: int) -> int:
    """Pending applications across all ateliers of a manager (menu badge: one COUNT)."""
    return int(
        db.session.scalar(
            sa.select(sa.func.count(JobApplication.id))
            .join(JobPost, JobPost.id == JobApplication.post_id)
            .join(Atelier, Atelier.id == JobPost.atelier_id)
            .where(Atelier.manager_id == manager_id, JobApplication.status == ApplicationStatus.PENDING)
        )
        or 0
    )


def candidate_events(atelier_id: int, day: dt.date) -> list[Event]:
    """Staffable events of one atelier on *day* (where an accepted applicant can be placed)."""
    return list(
        db.session.scalars(
            sa.select(Event)
            .where(
                Event.atelier_id == atelier_id,
                Event.date == day,
                Event.status.notin_(list(CLOSED_EVENT_STATUSES)),
            )
            .options(joinedload(Event.hall))
            .order_by(Event.start_time, Event.id)
        )
    )


def upcoming_events(atelier_ids: Sequence[int], today: dt.date, limit: int = 200) -> list[Event]:
    """Events of the manager's ateliers that can still be staffed (options of the post / invite forms)."""
    ids = sorted(atelier_ids)
    if not ids:
        return []
    return list(
        db.session.scalars(
            sa.select(Event)
            .where(
                Event.atelier_id.in_(ids),
                Event.date >= today,
                Event.status.notin_(list(CLOSED_EVENT_STATUSES)),
            )
            .options(joinedload(Event.hall))
            .order_by(Event.date, Event.start_time, Event.id)
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------
# the staff member's side
# --------------------------------------------------------------------------

MY_TABS = ("all", "pending", "accepted", "rejected", "withdrawn")


def my_applications_select(user_id: int, tab: str) -> sa.Select:
    stmt = (
        sa.select(JobApplication)
        .join(JobPost, JobPost.id == JobApplication.post_id)
        .where(JobApplication.user_id == user_id)
        .options(
            contains_eager(JobApplication.post).joinedload(JobPost.atelier).joinedload(Atelier.manager),
            contains_eager(JobApplication.post).joinedload(JobPost.event).joinedload(Event.hall),
        )
        .order_by(JobApplication.created_at.desc(), JobApplication.id.desc())
    )
    if tab in {"pending", "accepted", "rejected", "withdrawn"}:
        stmt = stmt.where(JobApplication.status == ApplicationStatus.coerce(tab))
    return stmt


def my_application_counts(user_id: int) -> dict[str, int]:
    counts = {tab: 0 for tab in MY_TABS}
    rows = db.session.execute(
        sa.select(JobApplication.status, sa.func.count(JobApplication.id))
        .where(JobApplication.user_id == user_id)
        .group_by(JobApplication.status)
    )
    for status, count in rows:
        counts[ApplicationStatus.coerce(status).value] = int(count)
    counts["all"] = sum(counts[tab] for tab in MY_TABS if tab != "all")
    return counts


def invitations_select(user_id: int, today: dt.date) -> sa.Select:
    """Unanswered invitations (assignments in ``invited``) of a staff member for events still ahead."""
    return (
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Assignment.user_id == user_id,
            Assignment.status == AssignmentStatus.INVITED,
            Event.date >= today,
            Event.status.notin_(list(CLOSED_EVENT_STATUSES)),
        )
        .options(
            contains_eager(Assignment.event).joinedload(Event.hall),
            contains_eager(Assignment.event).joinedload(Event.atelier).joinedload(Atelier.manager),
        )
        .order_by(Event.date, Assignment.id)
    )


def pending_invitation_count(user_id: int, today: dt.date) -> int:
    return int(
        db.session.scalar(
            sa.select(sa.func.count(Assignment.id))
            .join(Event, Event.id == Assignment.event_id)
            .where(
                Assignment.user_id == user_id,
                Assignment.status == AssignmentStatus.INVITED,
                Event.date >= today,
                Event.status.notin_(list(CLOSED_EVENT_STATUSES)),
            )
        )
        or 0
    )


# --------------------------------------------------------------------------
# the talent directory
# --------------------------------------------------------------------------

TALENT_SORTS = ("rating", "experience", "new")


def _free_on(day: dt.date) -> list[Any]:
    """WHERE parts: the person has no 'unavailable' mark and no active assignment on *day*."""
    marked = (
        sa.exists()
        .where(
            StaffAvailability.user_id == StaffProfile.user_id,
            StaffAvailability.date == day,
            StaffAvailability.status == AvailabilityStatus.UNAVAILABLE,
        )
        .correlate(StaffProfile)
    )
    busy = (
        sa.exists()
        .where(
            Assignment.event_id == Event.id,
            Event.date == day,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_(list(ACTIVE_ASSIGNMENT_STATUSES)),
            Assignment.involving_user(StaffProfile.user_id),
        )
        .correlate(StaffProfile)
    )
    return [~marked, ~busy]


def talent_select(
    *,
    skill: Any = None,
    city: Optional[str] = None,
    min_rating: Optional[int] = None,
    on_date: Optional[dt.date] = None,
    name: Optional[str] = None,
    sort: str = "rating",
) -> sa.Select:
    """Listed, active staff matching the filters (the only people the directory ever shows)."""
    city_expr = sa.func.coalesce(StaffProfile.city, User.city)
    stmt = (
        sa.select(StaffProfile)
        .join(User, User.id == StaffProfile.user_id)
        .where(
            User.role == Role.STAFF,
            User.status == UserStatus.ACTIVE,
            StaffProfile.is_listed.is_(True),
        )
        .options(contains_eager(StaffProfile.user), selectinload(StaffProfile.skill_rows))
    )
    if skill:
        stmt = stmt.where(StaffProfile.with_skill(skill))
    if city:
        stmt = stmt.where(city_expr == city)
    if min_rating:
        stmt = stmt.where(StaffProfile.rating_count > 0, StaffProfile.rating_avg >= min_rating)
    if name:
        stmt = stmt.where(User.full_name.like(like_pattern(name), escape="\\"))
    if on_date:
        stmt = stmt.where(*_free_on(on_date))
    if sort == "experience":
        stmt = stmt.order_by(StaffProfile.experience_years.desc(), StaffProfile.rating_avg.desc(), StaffProfile.id)
    elif sort == "new":
        stmt = stmt.order_by(StaffProfile.id.desc())
    else:
        stmt = stmt.order_by(
            StaffProfile.rating_avg.desc(),
            StaffProfile.rating_count.desc(),
            StaffProfile.experience_years.desc(),
            StaffProfile.id,
        )
    return stmt


def talent_cities() -> list[str]:
    city_expr = sa.func.coalesce(StaffProfile.city, User.city)
    stmt = (
        sa.select(city_expr)
        .select_from(StaffProfile)
        .join(User, User.id == StaffProfile.user_id)
        .where(
            User.role == Role.STAFF,
            User.status == UserStatus.ACTIVE,
            StaffProfile.is_listed.is_(True),
            city_expr.is_not(None),
            city_expr != "",
        )
        .distinct()
        .order_by(city_expr)
    )
    return list(db.session.execute(stmt).scalars())


def get_profile(user_id: int) -> Optional[StaffProfile]:
    """The staff profile (with user and skills) of an active staff account, or ``None``."""
    return db.session.scalar(
        sa.select(StaffProfile)
        .join(User, User.id == StaffProfile.user_id)
        .where(StaffProfile.user_id == user_id, User.role == Role.STAFF, User.status != UserStatus.SUSPENDED)
        .options(contains_eager(StaffProfile.user), selectinload(StaffProfile.skill_rows))
    )


def public_portfolio(user_id: int, limit: int = 12) -> list[PortfolioItem]:
    return list(
        db.session.scalars(
            sa.select(PortfolioItem)
            .where(PortfolioItem.user_id == user_id, PortfolioItem.is_public.is_(True))
            .order_by(PortfolioItem.sort_order, PortfolioItem.id)
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------
# who is related to whom (privacy by design)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    """How a viewer is connected to a staff member.

    ``connected``  the viewer's atelier has an application from, or an assignment / invitation for,
                   the person: their profile may be opened even when it is not listed;
    ``contact``    an accepted application or a committed (accepted / confirmed / completed)
                   assignment: the mobile number is revealed.
    """

    connected: bool = False
    contact: bool = False


def _atelier_ids(viewer: Any) -> list[int]:
    return list(db.session.scalars(sa.select(Atelier.id).where(Atelier.manager_id == viewer.id)))


def _hall_ids(viewer: Any) -> list[int]:
    return list(db.session.scalars(sa.select(Hall.id).where(Hall.manager_id == viewer.id)))


def committed_partners(viewer: Any, user_ids: Iterable[int]) -> set[int]:
    """Which of *user_ids* have an accepted application / committed assignment with *viewer*.

    Admins are partners of everybody.  Atelier managers: through their ateliers' posts and events.
    Hall managers: through assignments at events of their halls.  Everyone else: nobody.
    """
    ids = sorted(set(user_ids))
    if not ids or viewer is None:
        return set()
    if viewer.role == Role.ADMIN:
        return set(ids)
    found: set[int] = set()
    if viewer.role == Role.ATELIER_MANAGER:
        atelier_ids = _atelier_ids(viewer)
        if atelier_ids:
            found.update(
                db.session.scalars(
                    sa.select(Assignment.user_id)
                    .join(Event, Event.id == Assignment.event_id)
                    .where(
                        Event.atelier_id.in_(atelier_ids),
                        Assignment.user_id.in_(ids),
                        Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
                    )
                )
            )
            found.update(
                db.session.scalars(
                    sa.select(JobApplication.user_id)
                    .join(JobPost, JobPost.id == JobApplication.post_id)
                    .where(
                        JobPost.atelier_id.in_(atelier_ids),
                        JobApplication.user_id.in_(ids),
                        JobApplication.status == ApplicationStatus.ACCEPTED,
                    )
                )
            )
    elif viewer.role == Role.HALL_MANAGER:
        hall_ids = _hall_ids(viewer)
        if hall_ids:
            found.update(
                db.session.scalars(
                    sa.select(Assignment.user_id)
                    .join(Event, Event.id == Assignment.event_id)
                    .where(
                        Event.hall_id.in_(hall_ids),
                        Assignment.user_id.in_(ids),
                        Assignment.status.in_(list(COMMITTED_ASSIGNMENT_STATUSES)),
                    )
                )
            )
    return {value for value in found if value is not None}


def relation_to(viewer: Any, user_id: int) -> Relation:
    """The viewer's :class:`Relation` to one staff member (a handful of cheap EXISTS queries)."""
    if viewer is None:
        return Relation()
    if viewer.role == Role.ADMIN:
        return Relation(connected=True, contact=True)
    contact = user_id in committed_partners(viewer, [user_id])
    if contact:
        return Relation(connected=True, contact=True)
    connected = False
    if viewer.role == Role.ATELIER_MANAGER:
        atelier_ids = _atelier_ids(viewer)
        if atelier_ids:
            connected = bool(
                db.session.scalar(
                    sa.select(JobApplication.id)
                    .join(JobPost, JobPost.id == JobApplication.post_id)
                    .where(JobPost.atelier_id.in_(atelier_ids), JobApplication.user_id == user_id)
                    .limit(1)
                )
            ) or bool(
                db.session.scalar(
                    sa.select(Assignment.id)
                    .join(Event, Event.id == Assignment.event_id)
                    .where(Event.atelier_id.in_(atelier_ids), Assignment.user_id == user_id)
                    .limit(1)
                )
            )
    elif viewer.role == Role.HALL_MANAGER:
        hall_ids = _hall_ids(viewer)
        if hall_ids:
            connected = bool(
                db.session.scalar(
                    sa.select(Assignment.id)
                    .join(Event, Event.id == Assignment.event_id)
                    .where(Event.hall_id.in_(hall_ids), Assignment.user_id == user_id)
                    .limit(1)
                )
            )
    return Relation(connected=connected, contact=False)


# --------------------------------------------------------------------------
# reviews
# --------------------------------------------------------------------------

REVIEW_TABS = ("pending", "written", "received")


def _review_options(stmt: sa.Select) -> sa.Select:
    return stmt.options(
        selectinload(Review.assignment).selectinload(Assignment.event).selectinload(Event.atelier),
        selectinload(Review.assignment).selectinload(Assignment.event).selectinload(Event.hall),
        joinedload(Review.author),
        joinedload(Review.subject),
    )


def reviews_about_user(user_id: int) -> sa.Select:
    """Visible reviews whose subject is *user_id*, newest first."""
    return _review_options(
        sa.select(Review)
        .where(Review.subject_id == user_id, Review.is_hidden.is_(False))
        .order_by(Review.created_at.desc(), Review.id.desc())
    )


def reviews_by_user(user_id: int) -> sa.Select:
    return _review_options(
        sa.select(Review).where(Review.author_id == user_id).order_by(Review.created_at.desc(), Review.id.desc())
    )


def pending_reviews_select(user: Any) -> sa.Select:
    """Completed assignments the user may review but has not reviewed yet.

    Atelier managers review the person of an assignment of one of their ateliers' events (the person
    needs an account); staff review the atelier of an event they completed.
    """
    mine = ~sa.exists().where(Review.assignment_id == Assignment.id, Review.author_id == user.id).correlate(Assignment)
    base = (
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(Assignment.status == AssignmentStatus.COMPLETED, mine)
        .options(
            contains_eager(Assignment.event).joinedload(Event.hall),
            contains_eager(Assignment.event).joinedload(Event.atelier),
            joinedload(Assignment.user),
            selectinload(Assignment.roster_member).joinedload(RosterMember.user),
        )
        .order_by(Event.date.desc(), Assignment.id.desc())
    )
    if user.role == Role.ATELIER_MANAGER:
        atelier_ids = _atelier_ids(user)
        person = sa.func.coalesce(Assignment.user_id, RosterMember.user_id)
        return (
            base.outerjoin(RosterMember, RosterMember.id == Assignment.roster_member_id)
            .where(Event.atelier_id.in_(atelier_ids or [0]), person.is_not(None))
        )
    return base.where(Assignment.involving_user(user.id), Event.atelier_id.is_not(None))


def count_of(stmt: sa.Select) -> int:
    """``COUNT(*)`` of any of the list statements above (loader options are dropped)."""
    from sqlalchemy.orm import lazyload

    sub = stmt.options(lazyload("*")).order_by(None).subquery()
    return int(db.session.scalar(sa.select(sa.func.count()).select_from(sub)) or 0)
