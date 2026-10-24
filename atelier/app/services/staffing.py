"""Staffing ("afish") domain service shared by every module.

Everything that creates or changes an :class:`~app.models.Assignment` or moves
an :class:`~app.models.Event` through its life cycle goes through here, so the
rules live in one place:

* :func:`assign_person` - staff an event role with a user or a roster member,
  snapshotting the agreed pay terms.
* :func:`set_terms` / :func:`lock_terms` / :func:`unlock_terms` - edit or freeze
  the terms snapshot.
* :func:`transition` - move an assignment through its state machine
  (``constants.ASSIGNMENT_TRANSITIONS``) with permission checks.
* :func:`change_event_status` - move an event through
  ``constants.EVENT_TRANSITIONS`` (cascading to its assignments).
* :func:`attach_equipment` / :func:`detach_equipment` - gear used at an event
  (rent snapshotted, ``equipment.assigned`` published).
* :func:`conflicts_for` / :func:`check_assignable` - double-booking detection.
* :func:`staffing_summary` - slots per role, filled / waiting / missing.

Contract for all functions:

* They use ``db.session`` only (no request or Flask-context dependence), call
  ``flush`` but **never commit** - the caller owns the transaction.
* ``actor`` is a User, a user id, or ``None`` for the system (cascades, cron).
  Non-system actors are checked against ``check_permissions``; an anonymous
  actor is rejected, never treated as the system.
* Every change writes an audit row, publishes on the event bus
  (``assignment.changed`` / ``event.status_changed``) and notifies the people
  concerned.  Subscriber or notification failures never break the change.

Who may do what: *admins* and the manager of the event's *atelier* may do
everything; the *assignee* may only accept, decline or withdraw their own
assignment; the manager of the event's *hall* may change the event status but
not its staffing; everybody else is refused.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import sqlalchemy as sa

from app.extensions import db
from app.models import (
    Assignment,
    Equipment,
    EquipmentUsage,
    Event,
    RosterMember,
    StaffAvailability,
    TermsLockedError,
    User,
    normalize_terms,
    utcnow,
)
from app.services import audit, eventbus, notifications
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    ASSIGNMENT_TRANSITIONS,
    CLOSED_EVENT_STATUSES,
    COMMITTED_ASSIGNMENT_STATUSES,
    EVENT_TRANSITIONS,
    FILLED_ASSIGNMENT_STATUSES,
    ROLE_DISPLAY_ORDER,
    AssignmentSource,
    AssignmentStatus,
    AvailabilityStatus,
    EventStatus,
    InvoiceStatus,
    NotificationKind,
    Role,
    Skill,
    UserStatus,
    is_legal_transition,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class StaffingError(Exception):
    """Base class of every rule violation raised by this module."""


class InvalidTransition(StaffingError):
    """The requested status change is not in the transition table."""

    def __init__(self, kind: str, old: Any, new: Any) -> None:
        super().__init__(f"illegal {kind} transition: {old} -> {new}")
        self.kind, self.old, self.new = kind, old, new


class AssignmentConflict(StaffingError):
    """The person already has active assignments on that date."""

    def __init__(self, conflicts: Sequence[Assignment]) -> None:
        ids = ", ".join(str(a.id) for a in conflicts)
        super().__init__(f"person is already assigned on that date (assignments: {ids})")
        self.conflicts = list(conflicts)


class DuplicateAssignment(StaffingError):
    """The person already holds this role on this event."""

    def __init__(self, existing: Sequence[Assignment]) -> None:
        super().__init__("person already has an active assignment for this role and event")
        self.existing = list(existing)


class EventNotStaffable(StaffingError):
    """The event cannot be (re)staffed in its current state."""


class TermsLocked(StaffingError, TermsLockedError):
    """Pay terms are frozen (an invoice was submitted); unlock first."""


class StaffingPermissionError(StaffingError, PermissionError):
    """The actor is not allowed to perform this action."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value.strip())
    raise TypeError(f"expected a date, got {type(value).__name__}")


def _load(model: type, value: Any, what: str):
    """Accept an instance or a primary key; return a persistent instance."""
    if value is None:
        return None
    if isinstance(value, model):
        return value
    obj = db.session.get(model, int(value))
    if obj is None:
        raise StaffingError(f"{what} {value!r} does not exist")
    return obj


def _jdate(value: dt.date) -> str:
    """Jalali long date with Persian digits; ISO if the formatter is unavailable."""
    try:
        from app.services.jalali import to_jalali_str

        return to_jalali_str(value, "long", persian_digits=True)
    except Exception:  # noqa: BLE001 - cosmetic only
        return value.isoformat()


def _event_label(event: Event) -> str:
    hall = event.hall.name if event.hall is not None else "؟"
    return f"{hall} - {_jdate(event.date)}"


def _resolve_actor(actor: Any) -> Optional[User]:
    """None -> system; otherwise the (active) User row. Anonymous is refused."""
    if actor is None:
        return None
    uid = audit.actor_id(actor)
    if uid is None:
        raise StaffingPermissionError("authentication required")
    user = db.session.get(User, uid)
    if user is None or user.status == UserStatus.SUSPENDED:
        raise StaffingPermissionError("account is not allowed to act")
    return user


def _is_admin(user: Optional[User]) -> bool:
    return user is not None and user.role == Role.ADMIN


def _manages_atelier(user: Optional[User], event: Event) -> bool:
    return (
        user is not None
        and event.atelier is not None
        and event.atelier.manager_id == user.id
    )


def _manages_hall(user: Optional[User], event: Event) -> bool:
    return user is not None and event.hall is not None and event.hall.manager_id == user.id


def _can_manage_staffing(user: Optional[User], event: Event) -> bool:
    return user is None or _is_admin(user) or _manages_atelier(user, event)


# --------------------------------------------------------------------------
# Who is "the same person"?
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonIdentity:
    """All keys under which one real person may appear in assignments."""

    user_ids: frozenset[int]
    roster_ids: frozenset[int]
    mobiles: frozenset[str]


def resolve_identity(
    user_id: Optional[int] = None, roster_member_id: Optional[int] = None
) -> PersonIdentity:
    """Expand a user and/or roster member to every row that is the same person.

    A roster member is the same person as the user it links to, and roster
    entries (of any atelier) or accounts sharing a mobile number are treated as
    one person - so a freelancer cannot be booked by two ateliers on one day
    just because one of them never created an account for them.
    """
    user_ids: set[int] = set()
    roster_ids: set[int] = set()
    mobiles: set[str] = set()
    if user_id is not None:
        user_ids.add(int(user_id))
    if roster_member_id is not None:
        roster_ids.add(int(roster_member_id))
        row = db.session.execute(
            sa.select(RosterMember.user_id, RosterMember.mobile).where(
                RosterMember.id == roster_member_id
            )
        ).one_or_none()
        if row is not None:
            if row.user_id is not None:
                user_ids.add(row.user_id)
            if row.mobile:
                mobiles.add(row.mobile)
    if user_ids:
        mobiles.update(
            db.session.execute(sa.select(User.mobile).where(User.id.in_(user_ids))).scalars()
        )
    if mobiles:
        user_ids.update(
            db.session.execute(sa.select(User.id).where(User.mobile.in_(mobiles))).scalars()
        )
    clauses = []
    if user_ids:
        clauses.append(RosterMember.user_id.in_(user_ids))
    if mobiles:
        clauses.append(RosterMember.mobile.in_(mobiles))
    if clauses:
        roster_ids.update(
            db.session.execute(sa.select(RosterMember.id).where(sa.or_(*clauses))).scalars()
        )
    return PersonIdentity(frozenset(user_ids), frozenset(roster_ids), frozenset(mobiles))


def _person_clause(identity: PersonIdentity):
    clauses = []
    if identity.user_ids:
        clauses.append(Assignment.user_id.in_(identity.user_ids))
    if identity.roster_ids:
        clauses.append(Assignment.roster_member_id.in_(identity.roster_ids))
    return sa.or_(*clauses) if clauses else sa.false()


# --------------------------------------------------------------------------
# Conflict detection
# --------------------------------------------------------------------------


def conflicts_for(
    date: Any,
    user_id: Optional[int] = None,
    roster_member_id: Optional[int] = None,
    exclude_assignment_id: Optional[int] = None,
    *,
    exclude_event_id: Optional[int] = None,
    statuses: Optional[Iterable[AssignmentStatus]] = None,
) -> list[Assignment]:
    """Active assignments of the same person on *date* (a double-booking check).

    Give a ``user_id`` and/or a ``roster_member_id``; both are expanded with
    :func:`resolve_identity`, so a roster entry and the account it belongs to
    clash with each other.  Only assignments in *statuses* (default: invited,
    accepted, confirmed, completed) on non-cancelled events count.  Rows of
    ``exclude_assignment_id`` / ``exclude_event_id`` are ignored (use them
    when re-checking an existing assignment).  Oldest event first.

    Note for UI code: assignments may belong to *other* ateliers; show such
    clashes as "busy" without leaking event details.
    """
    if user_id is None and roster_member_id is None:
        raise ValueError("give user_id or roster_member_id")
    day = _as_date(date)
    wanted = list(statuses) if statuses is not None else list(ACTIVE_ASSIGNMENT_STATUSES)
    identity = resolve_identity(user_id, roster_member_id)
    stmt = (
        sa.select(Assignment)
        .join(Event, Event.id == Assignment.event_id)
        .where(
            Event.date == day,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_(wanted),
            _person_clause(identity),
        )
        .order_by(Event.start_time, Assignment.id)
    )
    if exclude_assignment_id is not None:
        stmt = stmt.where(Assignment.id != exclude_assignment_id)
    if exclude_event_id is not None:
        stmt = stmt.where(Assignment.event_id != exclude_event_id)
    return list(db.session.execute(stmt).scalars().all())


def is_unavailable(user_ids: Iterable[int], date: Any) -> Optional[StaffAvailability]:
    """The 'unavailable' availability row of any of *user_ids* on *date*, if any."""
    ids = [int(i) for i in user_ids]
    if not ids:
        return None
    return db.session.execute(
        sa.select(StaffAvailability)
        .where(
            StaffAvailability.user_id.in_(ids),
            StaffAvailability.date == _as_date(date),
            StaffAvailability.status == AvailabilityStatus.UNAVAILABLE,
        )
        .limit(1)
    ).scalar_one_or_none()


@dataclass
class AssignCheck:
    """Result of :func:`check_assignable` (what would go wrong if we assigned)."""

    conflicts: list[Assignment] = field(default_factory=list)  # other events, same day
    duplicates: list[Assignment] = field(default_factory=list)  # same event + role
    unavailable: Optional[StaffAvailability] = None  # person marked the day busy

    @property
    def ok(self) -> bool:
        return not (self.conflicts or self.duplicates or self.unavailable)

    @property
    def blocking(self) -> bool:
        """Duplicates can never be overridden; conflicts / availability can."""
        return bool(self.duplicates)


def check_assignable(
    event: Event,
    role: Any,
    *,
    user: Any = None,
    roster_member: Any = None,
    exclude_assignment_id: Optional[int] = None,
) -> AssignCheck:
    """Dry-run of :func:`assign_person` for UI warnings (writes nothing)."""
    user = _load(User, user, "user")
    roster_member = _load(RosterMember, roster_member, "roster member")
    if (user is None) == (roster_member is None):
        raise ValueError("give exactly one of user / roster_member")
    role = Skill.coerce(role)
    identity = resolve_identity(
        user.id if user is not None else None,
        roster_member.id if roster_member is not None else None,
    )
    check = AssignCheck()
    if event.id is not None:
        duplicate_stmt = sa.select(Assignment).where(
            Assignment.event_id == event.id,
            Assignment.role == role,
            Assignment.status.in_(list(ACTIVE_ASSIGNMENT_STATUSES)),
            _person_clause(identity),
        )
        if exclude_assignment_id is not None:
            duplicate_stmt = duplicate_stmt.where(Assignment.id != exclude_assignment_id)
        check.duplicates = list(db.session.execute(duplicate_stmt).scalars().all())
    check.conflicts = conflicts_for(
        event.date,
        user.id if user is not None else None,
        roster_member.id if roster_member is not None else None,
        exclude_assignment_id,
        exclude_event_id=event.id,
    )
    check.unavailable = is_unavailable(identity.user_ids, event.date)
    return check


# --------------------------------------------------------------------------
# Publishing / notifying
# --------------------------------------------------------------------------


def _publish_assignment(
    assignment: Assignment,
    change: str,
    old_status: Optional[AssignmentStatus],
    actor_user: Optional[User],
) -> None:
    eventbus.publish(
        eventbus.ASSIGNMENT_CHANGED,
        assignment_id=assignment.id,
        event_id=assignment.event_id,
        change=change,
        old_status=old_status.value if old_status is not None else None,
        new_status=assignment.status.value,
        actor_id=actor_user.id if actor_user is not None else None,
        user_id=assignment.person_user_id,
    )


def _safe_notify(*args: Any, **kwargs: Any) -> None:
    """Notifications are best effort: a failure must not undo a staffing change."""
    try:
        notifications.notify(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("staffing: notification failed")


def _notify_assignee(
    assignment: Assignment, actor_user: Optional[User], kind: NotificationKind, title: str, body: str
) -> None:
    target = assignment.person_user_id
    if target is None or (actor_user is not None and actor_user.id == target):
        return
    _safe_notify(target, kind, title, body, entity=assignment)


def _notify_managers(
    event: Event, actor_user: Optional[User], kind: NotificationKind, title: str, body: str,
    *, include_hall: bool = False, entity: Any = None,
) -> None:
    recipients: list[int] = []
    if event.atelier is not None:
        recipients.append(event.atelier.manager_id)
    if include_hall and event.hall is not None:
        recipients.append(event.hall.manager_id)
    seen: set[int] = set()
    for user_id in recipients:
        if user_id in seen or (actor_user is not None and actor_user.id == user_id):
            continue
        seen.add(user_id)
        _safe_notify(user_id, kind, title, body, entity=entity or event)


# --------------------------------------------------------------------------
# Assigning
# --------------------------------------------------------------------------


def assign_person(
    event: Any,
    role: Any,
    *,
    user: Any = None,
    roster_member: Any = None,
    terms: Any,
    actor: Any,
    source: Any = None,
    call_time: Optional[dt.time] = None,
    notes: Optional[str] = None,
    status: Any = None,
    job_application: Any = None,
    allow_conflict: bool = False,
    check_permissions: bool = True,
) -> Assignment:
    """Staff *role* of *event* with a platform user **or** a roster member.

    ``terms`` is mandatory and snapshotted onto the assignment: a mapping
    (``base`` / ``base_pay``, ``commission_per_unit``, ``bonus_tiers``) or a
    ``payroll.PayTerms``.  Nothing is inherited later from profiles, roster
    defaults or presets.

    Initial status: ``confirmed`` for roster members (fixed staff need no
    acceptance, assumption A7), ``invited`` for platform users; override with
    ``status``.  ``source`` defaults to ``roster`` / ``marketplace`` (when a
    ``job_application`` is given) / ``direct``.

    Raises :class:`EventNotStaffable`, :class:`DuplicateAssignment` (same
    person, same role, same event - never overridable),
    :class:`AssignmentConflict` (same person busy elsewhere that day; pass
    ``allow_conflict=True`` to override) or :class:`StaffingPermissionError`.
    """
    event = _load(Event, event, "event")
    user = _load(User, user, "user")
    roster_member = _load(RosterMember, roster_member, "roster member")
    if (user is None) == (roster_member is None):
        raise ValueError("give exactly one of user / roster_member")
    role = Skill.coerce(role)
    actor_user = _resolve_actor(actor)
    if check_permissions and not _can_manage_staffing(actor_user, event):
        raise StaffingPermissionError("only the atelier manager can staff this event")

    if event.atelier_id is None:
        raise EventNotStaffable("event has no atelier assigned yet")
    if event.status in CLOSED_EVENT_STATUSES:
        raise EventNotStaffable(f"event is {event.status.value}; it can no longer be staffed")
    if roster_member is not None:
        if roster_member.atelier_id != event.atelier_id:
            raise StaffingError("roster member belongs to a different atelier")
        if not roster_member.is_active:
            raise StaffingError("roster member is inactive")
    if user is not None and user.status == UserStatus.SUSPENDED:
        raise StaffingError("user account is suspended")

    data = normalize_terms(terms)

    check = check_assignable(event, role, user=user, roster_member=roster_member)
    if check.duplicates:
        raise DuplicateAssignment(check.duplicates)
    if check.conflicts and not allow_conflict:
        raise AssignmentConflict(check.conflicts)

    if status is not None:
        initial = AssignmentStatus.coerce(status)
    else:
        initial = AssignmentStatus.CONFIRMED if roster_member is not None else AssignmentStatus.INVITED
    if source is not None:
        origin = AssignmentSource.coerce(source)
    elif job_application is not None:
        origin = AssignmentSource.MARKETPLACE
    elif roster_member is not None:
        origin = AssignmentSource.ROSTER
    else:
        origin = AssignmentSource.DIRECT

    assignment = Assignment(
        event=event,
        role=role,
        user=user,
        roster_member=roster_member,
        status=initial,
        base_pay=data["base"],
        commission_per_unit=data["commission_per_unit"],
        bonus_tiers=data["bonus_tiers"],
        call_time=call_time,
        notes=notes,
        source=origin,
        job_application_id=getattr(job_application, "id", job_application),
        created_by_id=actor_user.id if actor_user is not None else None,
    )
    db.session.add(assignment)
    db.session.flush()

    audit.log(
        actor_user,
        "assignment.created",
        assignment,
        {
            "event_id": event.id,
            "role": role,
            "user_id": assignment.user_id,
            "roster_member_id": assignment.roster_member_id,
            "status": initial,
            "source": origin,
            "terms": data,
            "override_conflict": bool(check.conflicts),
        },
    )
    _publish_assignment(assignment, "created", None, actor_user)

    atelier_name = event.atelier.name if event.atelier is not None else ""
    if initial == AssignmentStatus.INVITED:
        _notify_assignee(
            assignment,
            actor_user,
            NotificationKind.ASSIGNMENT_INVITED,
            f"دعوت به همکاری به‌عنوان {role.label}",
            f"{atelier_name} شما را برای مراسم {_event_label(event)} دعوت کرده است.",
        )
    else:
        _notify_assignee(
            assignment,
            actor_user,
            NotificationKind.ASSIGNMENT_CHANGED,
            f"افیش جدید: {role.label}",
            f"برای مراسم {_event_label(event)} افیش شده‌اید.",
        )
    return assignment


# --------------------------------------------------------------------------
# Terms snapshot
# --------------------------------------------------------------------------


def set_terms(
    assignment: Assignment,
    terms: Any,
    actor: Any,
    *,
    reason: Optional[str] = None,
    check_permissions: bool = True,
) -> Assignment:
    """Replace the pay-terms snapshot of one assignment.

    Only the atelier manager (or an admin / the system) may do this, never the
    assignee.  Refused for cancelled assignments and while the terms are locked
    (:class:`TermsLocked`).  A no-op when the terms are unchanged.
    """
    actor_user = _resolve_actor(actor)
    event = assignment.event
    if check_permissions and not _can_manage_staffing(actor_user, event):
        raise StaffingPermissionError("only the atelier manager can change pay terms")
    if assignment.is_terms_locked:
        raise TermsLocked(f"assignment {assignment.id} terms are locked")
    if assignment.status == AssignmentStatus.CANCELLED:
        raise StaffingError("cannot change terms of a cancelled assignment")

    new_terms = normalize_terms(terms)
    old_terms = assignment.terms_dict()
    if new_terms == old_terms:
        return assignment
    assignment.apply_terms(new_terms)
    db.session.flush()

    audit.log(
        actor_user,
        "assignment.terms_changed",
        assignment,
        {"old": old_terms, "new": new_terms, "reason": reason},
    )
    _publish_assignment(assignment, "terms", assignment.status, actor_user)
    _notify_assignee(
        assignment,
        actor_user,
        NotificationKind.ASSIGNMENT_CHANGED,
        "شرایط پرداخت شما تغییر کرد",
        f"مراسم {_event_label(event)}: شرایط پرداخت به‌روز شد.",
    )
    return assignment


def lock_terms(
    assignment: Assignment, actor: Any = None, *, check_permissions: bool = True
) -> Assignment:
    """Freeze the terms (and identity) of an assignment; idempotent.

    Call this for every assignment of an event when its invoice is submitted
    (see :func:`lock_event_terms`).  From then on neither :func:`set_terms` nor
    a direct attribute edit can change them (an ORM guard raises
    ``TermsLockedError``).  Atelier manager / admin / system only.
    """
    actor_user = _resolve_actor(actor)
    if check_permissions and not _can_manage_staffing(actor_user, assignment.event):
        raise StaffingPermissionError("only the atelier manager can lock pay terms")
    if assignment.terms_locked_at is None:
        assignment.terms_locked_at = utcnow()
        db.session.flush()
        audit.log(actor_user, "assignment.terms_locked", assignment, None)
        _publish_assignment(assignment, "locked", assignment.status, actor_user)
    return assignment


def lock_event_terms(
    event: Event, actor: Any = None, *, check_permissions: bool = True
) -> list[Assignment]:
    """Lock every committed (accepted / confirmed / completed) assignment of *event*."""
    locked = []
    for assignment in list(event.assignments):
        if assignment.status in COMMITTED_ASSIGNMENT_STATUSES:
            locked.append(lock_terms(assignment, actor, check_permissions=check_permissions))
    return locked


def unlock_terms(
    assignment: Assignment,
    actor: Any,
    *,
    reason: Optional[str] = None,
    check_permissions: bool = True,
) -> Assignment:
    """Lift the lock (re-opening an invoice).  Admin / atelier manager only."""
    actor_user = _resolve_actor(actor)
    if check_permissions and not _can_manage_staffing(actor_user, assignment.event):
        raise StaffingPermissionError("only the atelier manager can unlock pay terms")
    if assignment.terms_locked_at is not None:
        assignment.terms_locked_at = None
        db.session.flush()
        audit.log(actor_user, "assignment.terms_unlocked", assignment, {"reason": reason})
        _publish_assignment(assignment, "unlocked", assignment.status, actor_user)
    return assignment


# --------------------------------------------------------------------------
# Assignment state machine
# --------------------------------------------------------------------------

_ASSIGNEE_ACTIONS = frozenset(
    {AssignmentStatus.ACCEPTED, AssignmentStatus.DECLINED, AssignmentStatus.CANCELLED}
)
_UNDO_STATES = frozenset(
    {AssignmentStatus.DECLINED, AssignmentStatus.CANCELLED, AssignmentStatus.INVITED}
)


def can_transition(old: Any, new: Any) -> bool:
    """True if ``old -> new`` is in the assignment transition table."""
    return is_legal_transition(
        ASSIGNMENT_TRANSITIONS, AssignmentStatus.coerce(old), AssignmentStatus.coerce(new)
    )


def _authorize_transition(
    actor_user: Optional[User], assignment: Assignment, new: AssignmentStatus
) -> None:
    if actor_user is None or _is_admin(actor_user) or _manages_atelier(actor_user, assignment.event):
        return
    person = assignment.person_user_id
    if person is not None and person == actor_user.id and new in _ASSIGNEE_ACTIONS:
        return
    raise StaffingPermissionError(
        f"user {actor_user.id} may not set assignment {assignment.id} to {new.value}"
    )


def _apply_status(
    assignment: Assignment,
    new: AssignmentStatus,
    actor_user: Optional[User],
    reason: Optional[str],
) -> None:
    """Perform a (already authorised and validated) status change and announce it."""
    old = assignment.status
    assignment.status = new
    if new in (AssignmentStatus.ACCEPTED, AssignmentStatus.DECLINED):
        assignment.responded_at = utcnow()
    db.session.flush()

    audit.log(
        actor_user,
        f"assignment.{new.value}",
        assignment,
        {"from": old, "to": new, "reason": reason},
    )
    _publish_assignment(assignment, "status", old, actor_user)

    event = assignment.event
    label = _event_label(event)
    person_user = assignment.person_user_id
    acted_by_assignee = (
        actor_user is not None and person_user is not None and actor_user.id == person_user
    )
    if acted_by_assignee:
        verb = {
            AssignmentStatus.ACCEPTED: "دعوت را پذیرفت",
            AssignmentStatus.DECLINED: "دعوت را رد کرد",
            AssignmentStatus.CANCELLED: "همکاری خود را لغو کرد",
        }.get(new)
        if verb:
            _notify_managers(
                event,
                actor_user,
                NotificationKind.ASSIGNMENT_RESPONSE,
                f"{assignment.person_name} {verb}",
                f"{assignment.role.label} - مراسم {label}",
                entity=assignment,
            )
        return
    titles = {
        AssignmentStatus.CONFIRMED: "افیش شما قطعی شد",
        AssignmentStatus.CANCELLED: "افیش شما لغو شد",
        AssignmentStatus.INVITED: "دعوت مجدد به همکاری",
        AssignmentStatus.DECLINED: "افیش شما رد شد",
    }
    if new in titles:
        _notify_assignee(
            assignment,
            actor_user,
            NotificationKind.ASSIGNMENT_CHANGED,
            titles[new],
            f"{assignment.role.label} - مراسم {label}" + (f"\n{reason}" if reason else ""),
        )


def transition(
    assignment: Assignment,
    new_status: Any,
    actor: Any,
    *,
    reason: Optional[str] = None,
    allow_conflict: bool = False,
    check_permissions: bool = True,
) -> Assignment:
    """Move an assignment to *new_status* following the transition table.

    * Same status is a no-op (idempotent, e.g. a double-clicked button).
    * Illegal moves raise :class:`InvalidTransition`.
    * Becoming committed (accepted / confirmed) re-checks the person's other
      *committed* assignments that day and raises :class:`AssignmentConflict`
      unless ``allow_conflict``.  Re-inviting (``declined -> invited``) runs the
      same duplicate / double-booking checks as :func:`assign_person`.
    * Once terms are locked an assignment cannot be declined / cancelled /
      re-invited (:class:`TermsLocked`) - unlock first.
    * On a cancelled event only ``cancelled`` is possible.

    Permissions: admin / atelier manager / system may do anything legal; the
    assignee may accept, decline or withdraw (cancel) their own assignment.
    """
    new = AssignmentStatus.coerce(new_status)
    actor_user = _resolve_actor(actor)
    old = assignment.status
    if new == old:
        return assignment
    if not is_legal_transition(ASSIGNMENT_TRANSITIONS, old, new):
        raise InvalidTransition("assignment", old.value, new.value)
    if check_permissions:
        _authorize_transition(actor_user, assignment, new)
    event = assignment.event
    if event.status == EventStatus.CANCELLED and new != AssignmentStatus.CANCELLED:
        raise EventNotStaffable("event is cancelled")
    if assignment.is_terms_locked and new in _UNDO_STATES:
        raise TermsLocked(f"assignment {assignment.id} is locked; unlock it first")

    if new == AssignmentStatus.INVITED:  # re-inviting someone who declined
        check = check_assignable(
            event,
            assignment.role,
            user=assignment.user,
            roster_member=assignment.roster_member,
            exclude_assignment_id=assignment.id,
        )
        if check.duplicates:
            raise DuplicateAssignment(check.duplicates)
        if check.conflicts and not allow_conflict:
            raise AssignmentConflict(check.conflicts)

    if new in (AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED) and old not in COMMITTED_ASSIGNMENT_STATUSES:
        clashes = conflicts_for(
            event.date,
            assignment.user_id,
            assignment.roster_member_id,
            exclude_assignment_id=assignment.id,
            exclude_event_id=event.id,
            statuses=COMMITTED_ASSIGNMENT_STATUSES,
        )
        if clashes and not allow_conflict:
            raise AssignmentConflict(clashes)

    _apply_status(assignment, new, actor_user, reason)
    return assignment


# --------------------------------------------------------------------------
# Event life cycle
# --------------------------------------------------------------------------


def can_change_event_status(old: Any, new: Any) -> bool:
    return is_legal_transition(
        EVENT_TRANSITIONS, EventStatus.coerce(old), EventStatus.coerce(new)
    )


def change_event_status(
    event: Event,
    new_status: Any,
    actor: Any,
    *,
    reason: Optional[str] = None,
    check_permissions: bool = True,
) -> Event:
    """Move an event through ``constants.EVENT_TRANSITIONS``.

    Same status is a no-op.  Side effects on assignments:

    * ``cancelled`` - every active assignment is cancelled;
    * ``done`` - accepted / confirmed assignments become ``completed`` and
      still-unanswered invitations are cancelled;
    * ``confirmed -> open`` (the atelier hands the event back) - the atelier is
      cleared and its active assignments are cancelled.

    ``confirmed`` requires an atelier.  Allowed actors: admin, system, the
    manager of the event's hall or of its atelier.
    """
    new = EventStatus.coerce(new_status)
    actor_user = _resolve_actor(actor)
    old = event.status
    if new == old:
        return event
    if not is_legal_transition(EVENT_TRANSITIONS, old, new):
        raise InvalidTransition("event", old.value, new.value)
    if check_permissions and not (
        actor_user is None
        or _is_admin(actor_user)
        or _manages_hall(actor_user, event)
        or _manages_atelier(actor_user, event)
    ):
        raise StaffingPermissionError("not allowed to change this event's status")
    if new == EventStatus.CONFIRMED and event.atelier_id is None:
        raise StaffingError("an event needs an atelier before it can be confirmed")

    cascade: list[tuple[Assignment, AssignmentStatus, str]] = []
    if new == EventStatus.CANCELLED or (old == EventStatus.CONFIRMED and new == EventStatus.OPEN):
        text = "مراسم لغو شد" if new == EventStatus.CANCELLED else "مراسم از اتلیه پس گرفته شد"
        for assignment in event.assignments:
            if assignment.status in (
                AssignmentStatus.INVITED,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.CONFIRMED,
            ):
                cascade.append((assignment, AssignmentStatus.CANCELLED, text))
    elif new == EventStatus.DONE:
        for assignment in event.assignments:
            if assignment.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED):
                cascade.append((assignment, AssignmentStatus.COMPLETED, "مراسم برگزار شد"))
            elif assignment.status == AssignmentStatus.INVITED:
                cascade.append((assignment, AssignmentStatus.CANCELLED, "دعوت بی‌پاسخ ماند"))

    for assignment, target, text in cascade:
        if assignment.is_terms_locked and target == AssignmentStatus.CANCELLED:
            raise TermsLocked(f"assignment {assignment.id} is locked; unlock it first")
    previous_atelier_id = event.atelier_id
    interested = {
        manager_id
        for manager_id in (
            event.atelier.manager_id if event.atelier is not None else None,
            event.hall.manager_id if event.hall is not None else None,
        )
        if manager_id is not None
    }
    event.status = new
    if old == EventStatus.CONFIRMED and new == EventStatus.OPEN:
        event.atelier = None
    db.session.flush()
    for assignment, target, text in cascade:
        _apply_status(assignment, target, actor_user, text)

    audit.log(
        actor_user,
        f"event.{new.value}",
        event,
        {"from": old, "to": new, "reason": reason, "atelier_id": previous_atelier_id},
    )
    eventbus.publish(
        eventbus.EVENT_STATUS_CHANGED,
        event_id=event.id,
        old_status=old.value,
        new_status=new.value,
        actor_id=actor_user.id if actor_user is not None else None,
    )

    text = f"مراسم {_event_label(event)}" + (f"\n{reason}" if reason else "")
    for user_id in sorted(interested):
        if actor_user is None or actor_user.id != user_id:
            _safe_notify(
                user_id,
                NotificationKind.EVENT_STATUS,
                f"وضعیت مراسم: {new.label}",
                text,
                entity=event,
            )
    return event


# --------------------------------------------------------------------------
# Equipment on events
# --------------------------------------------------------------------------


def attach_equipment(
    event: Any, equipment: Any, actor: Any, *, check_permissions: bool = True
) -> EquipmentUsage:
    """Use *equipment* at *event*, freezing its current rent as the cost.

    Idempotent: attaching the same gear twice returns the existing usage.
    Both must belong to the same atelier; invoiced or cancelled events are
    closed.  Publishes ``equipment.assigned`` (``removed=False``).
    """
    event = _load(Event, event, "event")
    equipment = _load(Equipment, equipment, "equipment")
    actor_user = _resolve_actor(actor)
    if check_permissions and not _can_manage_staffing(actor_user, event):
        raise StaffingPermissionError("only the atelier manager can assign equipment")
    if event.atelier_id is None:
        raise EventNotStaffable("event has no atelier assigned yet")
    if event.status in (EventStatus.INVOICED, EventStatus.CANCELLED):
        raise EventNotStaffable(f"event is {event.status.value}; equipment is closed")
    if equipment.atelier_id != event.atelier_id:
        raise StaffingError("equipment belongs to a different atelier")
    if not equipment.is_active:
        raise StaffingError("equipment is inactive")
    existing = db.session.execute(
        sa.select(EquipmentUsage).where(
            EquipmentUsage.event_id == event.id, EquipmentUsage.equipment_id == equipment.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    usage = EquipmentUsage.snapshot(event, equipment)
    db.session.add(usage)
    db.session.flush()
    audit.log(
        actor_user,
        "equipment.attached",
        usage,
        {"event_id": event.id, "equipment_id": equipment.id, "rent": usage.rent_cost_snapshot},
    )
    eventbus.publish(
        eventbus.EQUIPMENT_ASSIGNED,
        event_id=event.id,
        equipment_id=equipment.id,
        usage_id=usage.id,
        actor_id=actor_user.id if actor_user is not None else None,
        removed=False,
    )
    return usage


def detach_equipment(
    event: Any, equipment: Any, actor: Any, *, check_permissions: bool = True
) -> bool:
    """Stop using *equipment* at *event*; returns False if it was not attached.

    Refused once the event's invoice is submitted (reopen it first).  Publishes
    ``equipment.assigned`` with ``removed=True``.
    """
    event = _load(Event, event, "event")
    equipment = _load(Equipment, equipment, "equipment")
    actor_user = _resolve_actor(actor)
    if check_permissions and not _can_manage_staffing(actor_user, event):
        raise StaffingPermissionError("only the atelier manager can assign equipment")
    usage = db.session.execute(
        sa.select(EquipmentUsage).where(
            EquipmentUsage.event_id == event.id, EquipmentUsage.equipment_id == equipment.id
        )
    ).scalar_one_or_none()
    if usage is None:
        return False
    invoice = event.invoice
    if invoice is not None and invoice.status in (InvoiceStatus.SUBMITTED, InvoiceStatus.SETTLED):
        raise StaffingError("the invoice is already submitted; reopen it first")
    usage_id = usage.id
    audit.log(
        actor_user,
        "equipment.detached",
        usage,
        {"event_id": event.id, "equipment_id": equipment.id, "rent": usage.rent_cost_snapshot},
    )
    db.session.delete(usage)
    db.session.flush()
    eventbus.publish(
        eventbus.EQUIPMENT_ASSIGNED,
        event_id=event.id,
        equipment_id=equipment.id,
        usage_id=usage_id,
        actor_id=actor_user.id if actor_user is not None else None,
        removed=True,
    )
    return True


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------


@dataclass
class RoleSlots:
    """Head-count of one role on one event."""

    role: Skill
    needed: int
    filled: int  # accepted + confirmed + completed
    invited: int  # waiting for an answer
    assignments: list[Assignment] = field(default_factory=list)  # active ones

    @property
    def label(self) -> str:
        return self.role.label

    @property
    def missing(self) -> int:
        """Slots nobody has been asked about yet."""
        return max(0, self.needed - self.filled - self.invited)

    @property
    def shortfall(self) -> int:
        """Slots not yet filled by a committed person (``missing`` + waiting)."""
        return max(0, self.needed - self.filled)

    @property
    def surplus(self) -> int:
        return max(0, self.filled + self.invited - self.needed)

    @property
    def is_filled(self) -> bool:
        return self.filled >= self.needed

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "label": self.label,
            "needed": self.needed,
            "filled": self.filled,
            "invited": self.invited,
            "missing": self.missing,
            "shortfall": self.shortfall,
            "surplus": self.surplus,
            "assignment_ids": [a.id for a in self.assignments],
        }


@dataclass
class StaffingSummary:
    """Staffing status of an event, roles in on-site workflow order."""

    event_id: Optional[int]
    roles: dict[str, RoleSlots]

    @property
    def needed_total(self) -> int:
        return sum(r.needed for r in self.roles.values())

    @property
    def filled_total(self) -> int:
        return sum(min(r.filled, r.needed) for r in self.roles.values())

    @property
    def invited_total(self) -> int:
        return sum(r.invited for r in self.roles.values())

    @property
    def missing_total(self) -> int:
        return sum(r.missing for r in self.roles.values())

    @property
    def shortfall_total(self) -> int:
        return sum(r.shortfall for r in self.roles.values())

    @property
    def is_fully_staffed(self) -> bool:
        return all(r.is_filled for r in self.roles.values())

    def __getitem__(self, role: Any) -> RoleSlots:
        return self.roles[Skill.coerce(role).value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "needed_total": self.needed_total,
            "filled_total": self.filled_total,
            "invited_total": self.invited_total,
            "missing_total": self.missing_total,
            "shortfall_total": self.shortfall_total,
            "is_fully_staffed": self.is_fully_staffed,
            "roles": [r.to_dict() for r in self.roles.values()],
        }


def staffing_summary(event: Event) -> StaffingSummary:
    """Slots per role for *event*: needed, filled, waiting, missing.

    ``needed`` comes from ``event.staffing_needs`` (default one per role).
    ``filled`` counts accepted / confirmed / completed assignments, ``invited``
    those still waiting for an answer; declined and cancelled ones are ignored.
    """
    if event.id is not None:
        rows = db.session.execute(
            sa.select(Assignment)
            .where(Assignment.event_id == event.id)
            .order_by(Assignment.id)
        ).scalars().all()
    else:
        rows = list(event.assignments)
    by_role: dict[str, RoleSlots] = {}
    for role in ROLE_DISPLAY_ORDER:
        by_role[role.value] = RoleSlots(
            role=role, needed=event.needed(role), filled=0, invited=0
        )
    for assignment in rows:
        slots = by_role.get(Skill.coerce(assignment.role).value)
        if slots is None or assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
            continue
        slots.assignments.append(assignment)
        if assignment.status in FILLED_ASSIGNMENT_STATUSES:
            slots.filled += 1
        else:
            slots.invited += 1
    return StaffingSummary(event_id=event.id, roles=by_role)
