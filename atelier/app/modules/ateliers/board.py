"""Read side of the staffing board (افیش): batch queries, completeness, conflicts, directory.

Everything here *reads*.  Writes go through ``app.services.staffing`` (assign, transition,
terms, status) so the rules live in one place.  The functions below exist because list pages
must not run one query per event or per person (the services answer one event at a time):

* :func:`load_boards`        assignments + summaries of many events in two queries
* :func:`completeness`       the meter shown on cards (filled / waiting / missing slots)
* :class:`BusyIndex`         "is this person busy that day?" for a whole picker list at once
* :func:`search_directory`   the staff directory (listed profiles, or an exact mobile number)
* :func:`prefill_terms`      which terms the assignment form starts with, and why
* :func:`copy_plan`          what "copy staffing from a previous event" would do
* :func:`share_text`         the plain-text afish for messengers

The authoritative checks stay in ``staffing`` (``check_assignable`` / ``assign_person``);
the picker's busy flag is a convenience that is verified again when the form is opened and
again when it is submitted.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import sqlalchemy as sa
from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import (
    Assignment,
    Atelier,
    Event,
    RosterMember,
    StaffAvailability,
    StaffProfile,
    User,
    normalize_mobile,
)
from app.services import jalali, staffing
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    CLOSED_EVENT_STATUSES,
    FILLED_ASSIGNMENT_STATUSES,
    ROLE_DISPLAY_ORDER,
    AvailabilityStatus,
    EventStatus,
    Role,
    Skill,
    UserStatus,
)
from app.services.forms import clean_text
from app.services.money import to_persian_digits
from app.services.staffing import RoleSlots, StaffingSummary

# --------------------------------------------------------------------------
# summaries and completeness
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Completeness:
    """Slot counts of one event, each role capped at its head-count (never negative)."""

    needed: int
    filled: int  # accepted / confirmed / completed
    invited: int  # asked, waiting for an answer
    missing: int  # nobody asked yet

    @property
    def percent(self) -> int:
        """Share of slots filled by a committed person (100 when nothing is needed)."""
        if self.needed <= 0:
            return 100
        return int(round(100 * self.filled / self.needed))

    @property
    def is_complete(self) -> bool:
        return self.missing == 0 and self.invited == 0

    @property
    def is_fully_staffed(self) -> bool:
        return self.filled >= self.needed

    @property
    def tone(self) -> str:
        """``success`` when everyone is committed, ``warning`` when only answers are missing, else ``danger``."""
        if self.needed <= 0 or self.is_fully_staffed:
            return "success"
        if self.missing == 0:
            return "warning"
        return "danger"

    def pips(self) -> list[str]:
        """One state per slot, in the order filled -> invited -> missing (for the meter)."""
        return ["filled"] * self.filled + ["invited"] * self.invited + ["missing"] * self.missing


def completeness(summary: StaffingSummary) -> Completeness:
    needed = filled = invited = missing = 0
    for slots in summary.roles.values():
        role_filled = min(slots.filled, slots.needed)
        role_invited = min(slots.invited, slots.needed - role_filled)
        needed += slots.needed
        filled += role_filled
        invited += role_invited
        missing += slots.needed - role_filled - role_invited
    return Completeness(needed, filled, invited, missing)


def build_summary(event: Event, assignments: Iterable[Assignment]) -> StaffingSummary:
    """Same numbers as :func:`staffing.staffing_summary`, from assignments already in memory."""
    roles: dict[str, RoleSlots] = {
        role.value: RoleSlots(role=role, needed=event.needed(role), filled=0, invited=0)
        for role in ROLE_DISPLAY_ORDER
    }
    for assignment in assignments:
        slots = roles.get(Skill.coerce(assignment.role).value)
        if slots is None or assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
            continue
        slots.assignments.append(assignment)
        if assignment.status in FILLED_ASSIGNMENT_STATUSES:
            slots.filled += 1
        else:
            slots.invited += 1
    return StaffingSummary(event_id=event.id, roles=roles)


@dataclass
class EventBoard:
    """One event with its afish: all assignments (any status) and the staffing summary."""

    event: Event
    assignments: list[Assignment]
    summary: StaffingSummary

    @property
    def completeness(self) -> Completeness:
        return completeness(self.summary)

    def history(self, role: Any) -> list[Assignment]:
        """Declined and cancelled rows of *role* (the manager still wants to see who said no)."""
        value = Skill.coerce(role).value
        return [
            a
            for a in self.assignments
            if Skill.coerce(a.role).value == value and a.status not in ACTIVE_ASSIGNMENT_STATUSES
        ]

    def declined(self) -> list[Assignment]:
        return [a for a in self.assignments if a.status.value == "declined"]


def load_boards(events: Sequence[Event]) -> dict[int, EventBoard]:
    """Boards of *events* with one query for all assignments (users and roster members joined)."""
    ids = [event.id for event in events]
    grouped: dict[int, list[Assignment]] = {event_id: [] for event_id in ids}
    if ids:
        rows = (
            db.session.execute(
                sa.select(Assignment)
                .where(Assignment.event_id.in_(ids))
                .options(joinedload(Assignment.user), joinedload(Assignment.roster_member))
                .order_by(Assignment.id)
            )
            .scalars()
            .unique()
            .all()
        )
        for row in rows:
            grouped[row.event_id].append(row)
    return {
        event.id: EventBoard(event, grouped[event.id], build_summary(event, grouped[event.id]))
        for event in events
    }


def load_board(event: Event) -> EventBoard:
    return load_boards([event])[event.id]


def is_staffable(event: Event) -> bool:
    """The afish of this event can still change (an atelier is set and the event is not closed)."""
    return event.atelier_id is not None and event.status not in CLOSED_EVENT_STATUSES


# --------------------------------------------------------------------------
# who is busy that day
# --------------------------------------------------------------------------


@dataclass
class BusyIndex:
    """Everybody who has an active assignment on one date (anywhere on the platform).

    Built with a single query and matched the way :func:`staffing.resolve_identity` does
    (account, roster entry, or the same mobile number), so the picker can flag a whole list
    of candidates without asking the service about each one.
    """

    user_ids: set[int] = field(default_factory=set)
    roster_ids: set[int] = field(default_factory=set)
    mobiles: set[str] = field(default_factory=set)

    def member_busy(self, member: RosterMember) -> bool:
        if member.id in self.roster_ids:
            return True
        if member.user_id is not None and member.user_id in self.user_ids:
            return True
        if member.mobile and member.mobile in self.mobiles:
            return True
        user = member.user
        return bool(user is not None and (user.id in self.user_ids or user.mobile in self.mobiles))

    def user_busy(self, user: User) -> bool:
        return user.id in self.user_ids or user.mobile in self.mobiles


def busy_index(day: dt.date, *, exclude_event_id: Optional[int] = None) -> BusyIndex:
    stmt = (
        sa.select(
            Assignment.user_id,
            Assignment.roster_member_id,
            RosterMember.user_id.label("roster_user_id"),
            RosterMember.mobile.label("roster_mobile"),
            User.mobile.label("user_mobile"),
        )
        .join(Event, Event.id == Assignment.event_id)
        .outerjoin(RosterMember, RosterMember.id == Assignment.roster_member_id)
        .outerjoin(User, User.id == Assignment.user_id)
        .where(
            Event.date == day,
            Event.status != EventStatus.CANCELLED,
            Assignment.status.in_(sorted(ACTIVE_ASSIGNMENT_STATUSES)),
        )
    )
    if exclude_event_id is not None:
        stmt = stmt.where(Event.id != exclude_event_id)
    index = BusyIndex()
    for row in db.session.execute(stmt):
        if row.user_id is not None:
            index.user_ids.add(row.user_id)
        if row.roster_member_id is not None:
            index.roster_ids.add(row.roster_member_id)
        if row.roster_user_id is not None:
            index.user_ids.add(row.roster_user_id)
        if row.roster_mobile:
            index.mobiles.add(row.roster_mobile)
        if row.user_mobile:
            index.mobiles.add(row.user_mobile)
    return index


def unavailable_user_ids(user_ids: Iterable[int], day: dt.date) -> set[int]:
    """Accounts among *user_ids* that marked *day* "unavailable" (one query for the whole list)."""
    ids = sorted({int(i) for i in user_ids if i is not None})
    if not ids:
        return set()
    return set(
        db.session.execute(
            sa.select(StaffAvailability.user_id).where(
                StaffAvailability.user_id.in_(ids),
                StaffAvailability.date == day,
                StaffAvailability.status == AvailabilityStatus.UNAVAILABLE,
            )
        ).scalars()
    )


def describe_conflicts(conflicts: Iterable[Assignment], atelier_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Display data for a person's clashing assignments.

    Clashes inside my own ateliers show hall, time and role; those of other ateliers are only
    "busy" (no names, no details), as the staffing service documents.
    """
    mine = set(atelier_ids)
    out: list[dict[str, Any]] = []
    for assignment in conflicts:
        event = assignment.event
        if event.atelier_id in mine:
            out.append(
                {
                    "own": True,
                    "event_id": event.id,
                    "hall": event.hall.name if event.hall is not None else "",
                    "time": event.start_time,
                    "role": Skill.coerce(assignment.role).label,
                    "status": assignment.status,
                }
            )
        else:
            out.append({"own": False})
    return out


# --------------------------------------------------------------------------
# staff directory
# --------------------------------------------------------------------------

TOKEN_SALT = "ateliers.directory"
TOKEN_MAX_AGE = 3600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=TOKEN_SALT)


def directory_token(manager_id: int, user_id: int) -> str:
    """Short-lived proof that *manager_id* found *user_id* by typing their exact mobile number."""
    return _serializer().dumps([int(manager_id), int(user_id)])


def valid_directory_token(token: Optional[str], manager_id: int, user_id: int) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
    except BadSignature:
        return False
    return data == [int(manager_id), int(user_id)]


@dataclass
class DirectoryHit:
    user: User
    profile: Optional[StaffProfile]
    listed: bool
    token: Optional[str] = None  # needed to open the afish form for an unlisted person

    @property
    def skills(self) -> list[Skill]:
        if self.profile is None:
            return []
        return [Skill.coerce(value) for value in self.profile.skills]


def like_pattern(text: str) -> str:
    """``%text%`` for LIKE / ILIKE with the wildcards of *text* escaped (use ``escape="\\"``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def looks_like_mobile(text: str) -> bool:
    """True when *text* is a complete Iranian mobile number in any format or digit system."""
    try:
        normalize_mobile(clean_text(text))
    except ValueError:
        return False
    return True


def search_directory(
    manager_id: int,
    role: Optional[Skill] = None,
    query: str = "",
    *,
    limit: int = 20,
) -> list[DirectoryHit]:
    """Platform staff the atelier manager may pick.

    * a full mobile number finds that exact (active) staff account, listed or not;
    * anything else searches the *listed* profiles by name or city (and skill).

    Unlisted people therefore cannot be enumerated or browsed: they can only be reached by
    someone who already knows their number.
    """
    text = clean_text(query)
    base = (
        sa.select(User, StaffProfile)
        .outerjoin(StaffProfile, StaffProfile.user_id == User.id)
        .where(User.role == Role.STAFF, User.status == UserStatus.ACTIVE)
        .options(selectinload(StaffProfile.skill_rows))
    )
    mobile: Optional[str] = None
    if text:
        try:
            mobile = normalize_mobile(text)
        except ValueError:
            mobile = None
    hits: list[DirectoryHit] = []
    if mobile:
        for user, profile in db.session.execute(base.where(User.mobile == mobile).limit(1)):
            listed = bool(profile is not None and profile.is_listed)
            hits.append(
                DirectoryHit(user, profile, listed, None if listed else directory_token(manager_id, user.id))
            )
        return hits
    stmt = base.where(StaffProfile.is_listed.is_(True))
    if role is not None:
        stmt = stmt.where(StaffProfile.with_skill(role))
    if text:
        pattern = like_pattern(text)
        stmt = stmt.where(
            sa.or_(
                User.full_name.ilike(pattern, escape="\\"),
                User.city.ilike(pattern, escape="\\"),
                StaffProfile.city.ilike(pattern, escape="\\"),
            )
        )
    stmt = stmt.order_by(StaffProfile.rating_avg.desc(), StaffProfile.rating_count.desc(), User.full_name, User.id).limit(
        limit
    )
    for user, profile in db.session.execute(stmt):
        hits.append(DirectoryHit(user, profile, True))
    return hits


def directory_user(manager_id: int, user_id: Any, token: Optional[str] = None) -> Optional[User]:
    """The platform staff account *user_id* if this manager may pick it, else ``None``.

    Allowed: active staff accounts with a listed profile, or any active staff account when the
    request carries the signed token a search by exact mobile number produced.
    """
    try:
        key = int(user_id)
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, key)
    if user is None or user.role != Role.STAFF or user.status != UserStatus.ACTIVE:
        return None
    profile = user.staff_profile
    if profile is not None and profile.is_listed:
        return user
    return user if valid_directory_token(token, manager_id, user.id) else None


# --------------------------------------------------------------------------
# terms of a new assignment
# --------------------------------------------------------------------------

EMPTY_TERMS = {"base": 0, "commission_per_unit": 0, "bonus_tiers": []}

TERMS_SOURCE_LABELS = {
    "previous": "شرایط نفر قبلی همین جایگاه کپی شده است.",
    "member": "از پیش‌فرض همین نیروی ثابت پر شده است.",
    "preset": "از پیش‌فرض نقش در تنظیمات اتلیه پر شده است.",
    "profile": "از درخواست خود نیرو در پروفایلش پر شده است؛ پیش از ثبت بررسی کنید.",
    "none": "برای این نقش پیش‌فرضی ثبت نشده است؛ شرایط را وارد کنید.",
}


def prefill_terms(
    atelier: Atelier,
    role: Skill,
    *,
    member: Optional[RosterMember] = None,
    user: Optional[User] = None,
    previous: Optional[Assignment] = None,
) -> tuple[dict[str, Any], str]:
    """Terms the afish form starts with and where they came from (``previous|member|preset|profile|none``).

    Presets and defaults only *pre-fill* the form; what the manager submits is what is
    snapshotted on the assignment.
    """
    # Deep copies everywhere: the stored JSON of presets and defaults must never be edited through the result.
    if previous is not None:
        return previous.terms_dict(), "previous"
    if member is not None and (member.default_terms or {}).get(role.value):
        return copy.deepcopy(member.terms_for(role) or EMPTY_TERMS), "member"
    preset = atelier.preset_for(role)
    if preset:
        return copy.deepcopy(preset), "preset"
    if user is not None and user.staff_profile is not None:
        profile = user.staff_profile
        if profile.expected_base or profile.expected_commission_per_unit:
            return (
                {
                    "base": profile.expected_base or 0,
                    "commission_per_unit": profile.expected_commission_per_unit or 0,
                    "bonus_tiers": [],
                },
                "profile",
            )
    return copy.deepcopy(EMPTY_TERMS), "none"


# --------------------------------------------------------------------------
# copy staffing from another event
# --------------------------------------------------------------------------

COPYABLE_STATUSES = ACTIVE_ASSIGNMENT_STATUSES


@dataclass
class CopyRow:
    """One assignment of the source event and whether it can be repeated on the target."""

    assignment: Assignment
    skip_reason: Optional[str] = None  # None -> can be copied
    note: Optional[str] = None  # non-blocking remark

    @property
    def copyable(self) -> bool:
        return self.skip_reason is None


def copy_plan(source: Event, target: Event) -> list[CopyRow]:
    """For every active assignment of *source*: can the same person take the same role on *target*?"""
    rows: list[CopyRow] = []
    candidates = [a for a in source.assignments if a.status in COPYABLE_STATUSES]
    for assignment in candidates:
        row = CopyRow(assignment)
        member, user = assignment.roster_member, assignment.user
        if member is not None and (not member.is_active or member.atelier_id != target.atelier_id):
            row.skip_reason = "نیروی ثابت غیرفعال است."
        elif user is not None and user.status == UserStatus.SUSPENDED:
            row.skip_reason = "حساب این نیرو مسدود است."
        else:
            check = staffing.check_assignable(target, assignment.role, user=user, roster_member=member)
            if check.duplicates:
                row.skip_reason = "در این مراسم همین نقش را دارد."
            elif check.conflicts:
                row.skip_reason = "در همان روز مراسم دیگری دارد؛ از «افیش با تداخل» استفاده کنید."
            elif check.unavailable:
                row.skip_reason = "این روز را «در دسترس نیستم» ثبت کرده است."
        rows.append(row)
    return rows


def previous_events(exclude: Event, limit: int = 20) -> list[Event]:
    """Recent events of the same atelier that have a staffed afish (candidates to copy from)."""
    if exclude.atelier_id is None:
        return []
    return list(
        db.session.execute(
            sa.select(Event)
            .where(
                Event.atelier_id == exclude.atelier_id,
                Event.id != exclude.id,
                Event.status != EventStatus.CANCELLED,
                Event.id.in_(
                    sa.select(Assignment.event_id).where(
                        Assignment.status.in_(sorted(COPYABLE_STATUSES))
                    )
                ),
            )
            .options(joinedload(Event.hall))
            .order_by(Event.date.desc(), Event.id.desc())
            .limit(limit)
        )
        .scalars()
        .unique()
        .all()
    )


# --------------------------------------------------------------------------
# text for messengers
# --------------------------------------------------------------------------


def share_text(event: Event, board: EventBoard) -> str:
    """The afish as plain text (no phone numbers, no pay) for WhatsApp / Telegram / Eitaa ..."""
    hall = event.hall
    lines = [
        f"افیش مراسم {jalali.to_jalali_str(event.date, 'weekday_long', persian_digits=True)}",
        f"{hall.name}{' - ' + hall.city if hall is not None and hall.city else ''}" if hall is not None else "",
    ]
    if event.atelier is not None:
        lines.insert(0, event.atelier.name)
    if event.start_time:
        lines.append(f"ساعت {to_persian_digits(event.start_time.strftime('%H:%M'))}")
    for role in ROLE_DISPLAY_ORDER:
        slots = board.summary[role]
        names = [assignment.person_name for assignment in slots.assignments]
        lines.append(f"{role.label}: {'، '.join(names) if names else '—'}")
    return "\n".join(line for line in lines if line)
