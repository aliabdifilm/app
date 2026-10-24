"""Authorisation helpers: role checks and *ownership* checks.

Two layers, both required on every protected route:

1. **Role** - ``@role_required(Role.ATELIER_MANAGER)`` says *who may call the route at all*.
2. **Ownership** - a role is not enough: an atelier manager may only see *their*
   atelier's events.  Load objects through the helpers below; they answer with
   **404** (not 403) for somebody else's object, so ids cannot be probed::

       @bp.route("/events/<int:event_id>")
       @role_required(Role.ATELIER_MANAGER)
       def show(event_id):
           event = event_or_404(event_id, as_=("atelier",))      # 404 unless it is my atelier's
           ...

       @bp.route("/roster/<int:member_id>/delete", methods=["POST"])
       @role_required(Role.ATELIER_MANAGER)
       def delete(member_id):
           member = roster_member_or_404(member_id)             # member of one of my ateliers
           ...

       member = owned_or_404(Widget, widget_id,                 # anything else: say how ownership works
                             lambda w, user: w.atelier_id in my_atelier_ids(user))

   ``owner_check`` is **required** - there is no "allow everything" default.

Lists are scoped with :func:`scope_to_my_ateliers` / :func:`scope_to_my_halls`
(or ``Model.atelier_id.in_(my_atelier_ids())``) - never filter in Python after
loading everything.

Decorators
    :func:`role_required` (roles, optionally ``allow_pending=True``), :func:`login_required`,
    :func:`admin_required`.  Anonymous visitors are sent to the login page
    (JSON callers get 401); a wrong role gets the 403 page (JSON: 403 body);
    accounts waiting for admin approval are sent to ``/auth/pending`` unless
    the route is marked ``allow_pending=True`` (profile / onboarding pages).

Ids cached per request: :func:`my_hall_ids`, :func:`my_atelier_ids`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import sqlalchemy as sa
from flask import abort, current_app, has_request_context, request, session
from flask_login import current_user

from app.extensions import db
from app.models import (
    Assignment,
    Atelier,
    Equipment,
    Event,
    EventOffer,
    Hall,
    Invoice,
    JobApplication,
    JobPost,
    RosterMember,
    StockItem,
)
from app.services.constants import AssignmentStatus, EventStatus, OfferMode, OfferStatus, Role
from app.services.http import json_error, wants_json

__all__ = [
    "OnboardingRequired",
    "EventAccess",
    "is_admin",
    "get_user",
    "login_required",
    "role_required",
    "admin_required",
    "my_hall_ids",
    "my_atelier_ids",
    "get_my_halls",
    "get_my_hall",
    "get_my_ateliers",
    "get_my_atelier",
    "set_current_atelier",
    "owned_or_404",
    "hall_or_404",
    "atelier_or_404",
    "roster_member_or_404",
    "event_access",
    "event_or_404",
    "assignment_or_404",
    "invoice_or_404",
    "stock_item_or_404",
    "equipment_or_404",
    "job_post_or_404",
    "job_application_or_404",
    "scope_to_my_ateliers",
    "scope_to_my_halls",
]

SESSION_ATELIER_KEY = "atelier_id"


class OnboardingRequired(Exception):
    """The user must first create their hall / atelier / profile.

    Raised by :func:`get_my_atelier` / :func:`get_my_hall` when ``required=True``
    and nothing exists yet.  The app turns it into a redirect to the role's
    onboarding page (``nav.register_onboarding``) with a Persian flash message.
    """

    def __init__(self, role: Any = None, what: str = "profile") -> None:
        super().__init__(f"onboarding required: {what}")
        self.role = role
        self.what = what


# --------------------------------------------------------------------------
# who is asking
# --------------------------------------------------------------------------


def get_user(user: Any = None):
    """The concrete User for *user* (default: the logged-in one), or None for anonymous."""
    if user is None:
        user = current_user
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    getter = getattr(user, "_get_current_object", None)
    return getter() if getter else user


def is_admin(user: Any = None) -> bool:
    """True for an authenticated admin (default: the logged-in user)."""
    account = get_user(user)
    return account is not None and account.role == Role.ADMIN


def _require_user(user: Any = None):
    account = get_user(user)
    if account is None:
        abort(401)
    return account


def _cache() -> Optional[dict]:
    """Per-request memo (lives in the WSGI environ, so it never outlives the request)."""
    if has_request_context():
        return request.environ.setdefault("atelier.perm_cache", {})
    return None


def _memo(key: tuple, compute: Callable[[], Any]) -> Any:
    cache = _cache()
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


# --------------------------------------------------------------------------
# decorators
# --------------------------------------------------------------------------


def _pending_response():
    if wants_json():
        return json_error("حساب شما هنوز توسط مدیر سامانه تأیید نشده است.", 403, "pending_approval")
    from flask import redirect, url_for
    from werkzeug.routing import BuildError

    try:
        return redirect(url_for("auth.pending"))
    except BuildError:  # auth module disabled: fall back to the 403 page
        abort(403)


def _guard(roles: Optional[frozenset[str]], allow_pending: bool):
    """Return a response when access is denied, else None."""
    account = get_user()
    if account is None:
        return current_app.login_manager.unauthorized()
    if roles is not None and account.role.value not in roles:
        abort(403)
    if not allow_pending and not account.is_approved:
        return _pending_response()
    return None


def _decorate(roles: Optional[frozenset[str]], allow_pending: bool) -> Callable:
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            denied = _guard(roles, allow_pending)
            if denied is not None:
                return denied
            return view(*args, **kwargs)

        wrapped.__atelier_roles__ = roles  # introspection for the routes-matrix command
        return wrapped

    return decorator


def role_required(*roles: Any, allow_pending: bool = False) -> Callable:
    """Only users whose role is one of *roles* may call the view.

    ``@role_required(Role.STAFF)``, ``@role_required("hall_manager", "admin")``.
    Admins are *not* implicitly allowed: list ``Role.ADMIN`` where they should get in.
    """
    if not roles:
        raise ValueError("role_required needs at least one role; use login_required for 'any role'")
    if len(roles) == 1 and isinstance(roles[0], (list, tuple, set, frozenset)):
        roles = tuple(roles[0])
    values = frozenset(Role.coerce(role).value for role in roles)
    return _decorate(values, allow_pending)


def login_required(view: Optional[Callable] = None, *, allow_pending: bool = False) -> Callable:
    """Any logged-in user.  Usable bare (``@login_required``) or ``@login_required(allow_pending=True)``."""
    if view is not None and callable(view):
        return _decorate(None, False)(view)
    return _decorate(None, allow_pending)


def admin_required(view: Callable) -> Callable:
    """Shortcut for ``@role_required(Role.ADMIN)``."""
    return role_required(Role.ADMIN)(view)


# --------------------------------------------------------------------------
# what do I own?
# --------------------------------------------------------------------------


def my_hall_ids(user: Any = None) -> frozenset[int]:
    """Ids of the halls managed by *user* (empty for anonymous users)."""
    account = get_user(user)
    if account is None:
        return frozenset()

    def compute() -> frozenset[int]:
        rows = db.session.execute(sa.select(Hall.id).where(Hall.manager_id == account.id)).scalars()
        return frozenset(rows)

    return _memo(("hall_ids", account.id), compute)


def my_atelier_ids(user: Any = None) -> frozenset[int]:
    """Ids of the ateliers managed by *user* (empty for anonymous users)."""
    account = get_user(user)
    if account is None:
        return frozenset()

    def compute() -> frozenset[int]:
        rows = db.session.execute(sa.select(Atelier.id).where(Atelier.manager_id == account.id)).scalars()
        return frozenset(rows)

    return _memo(("atelier_ids", account.id), compute)


def get_my_halls(*, active_only: bool = False) -> list[Hall]:
    """The logged-in hall manager's halls, ordered by name."""
    account = _require_user()
    stmt = sa.select(Hall).where(Hall.manager_id == account.id).order_by(Hall.name, Hall.id)
    if active_only:
        stmt = stmt.where(Hall.is_active.is_(True))
    return list(db.session.execute(stmt).scalars())


def get_my_hall(hall_id: Optional[int] = None, *, required: bool = True) -> Optional[Hall]:
    """One of my halls.

    With *hall_id*: that hall, or 404 when it is not mine.  Without: the only
    hall, or the one chosen in the session; if I have none and *required* is
    true, :class:`OnboardingRequired` (-> "create your hall") is raised.
    """
    account = _require_user()
    halls = get_my_halls()
    if hall_id is not None:
        for hall in halls:
            if hall.id == int(hall_id):
                return hall
        abort(404)
    if not halls:
        if required:
            raise OnboardingRequired(account.role, "hall")
        return None
    chosen = session.get("hall_id")
    for hall in halls:
        if hall.id == chosen:
            return hall
    return halls[0]


def get_my_ateliers(*, active_only: bool = False) -> list[Atelier]:
    """The logged-in atelier manager's ateliers, ordered by name."""
    account = _require_user()
    stmt = sa.select(Atelier).where(Atelier.manager_id == account.id).order_by(Atelier.name, Atelier.id)
    if active_only:
        stmt = stmt.where(Atelier.is_active.is_(True))
    return list(db.session.execute(stmt).scalars())


def get_my_atelier(atelier_id: Optional[int] = None, *, required: bool = True) -> Optional[Atelier]:
    """One of my ateliers.

    With *atelier_id*: that atelier, or 404 when it is not mine.  Without: the
    atelier selected with :func:`set_current_atelier`, else my first one; if
    I have none and *required* is true, :class:`OnboardingRequired`.
    """
    account = _require_user()
    ateliers = get_my_ateliers()
    if atelier_id is not None:
        for atelier in ateliers:
            if atelier.id == int(atelier_id):
                return atelier
        abort(404)
    if not ateliers:
        if required:
            raise OnboardingRequired(account.role, "atelier")
        return None
    chosen = session.get(SESSION_ATELIER_KEY)
    for atelier in ateliers:
        if atelier.id == chosen:
            return atelier
    return ateliers[0]


def set_current_atelier(atelier: Any) -> None:
    """Remember which of my ateliers the UI is working on (404 if it is not mine)."""
    atelier_id = int(getattr(atelier, "id", atelier))
    if atelier_id not in my_atelier_ids():
        abort(404)
    session[SESSION_ATELIER_KEY] = atelier_id


# --------------------------------------------------------------------------
# object-level checks
# --------------------------------------------------------------------------

OwnerCheck = Callable[[Any, Any], bool]


def owned_or_404(
    model: type,
    pk: Any,
    owner_check: OwnerCheck,
    *,
    allow_admin: bool = False,
    options: Iterable[Any] = (),
) -> Any:
    """Load ``model`` row *pk* and return it only if ``owner_check(obj, user)`` is true.

    404 for a missing id **and** for somebody else's object (same answer, so
    existence cannot be probed).  *owner_check* is mandatory on purpose.
    ``allow_admin=True`` lets site admins through.
    """
    account = _require_user()
    try:
        key = int(pk)
    except (TypeError, ValueError):
        abort(404)
    obj = db.session.get(model, key, options=tuple(options))
    if obj is None:
        abort(404)
    if allow_admin and account.role == Role.ADMIN:
        return obj
    if not owner_check(obj, account):
        abort(404)
    return obj


def hall_or_404(hall_id: Any, *, allow_admin: bool = False) -> Hall:
    """A hall I manage."""
    return owned_or_404(Hall, hall_id, lambda hall, user: hall.manager_id == user.id, allow_admin=allow_admin)


def atelier_or_404(atelier_id: Any, *, allow_admin: bool = False) -> Atelier:
    """An atelier I manage."""
    return owned_or_404(
        Atelier, atelier_id, lambda atelier, user: atelier.manager_id == user.id, allow_admin=allow_admin
    )


def roster_member_or_404(member_id: Any, *, allow_admin: bool = False) -> RosterMember:
    """A fixed-staff entry of one of my ateliers."""
    return owned_or_404(
        RosterMember,
        member_id,
        lambda member, user: member.atelier_id in my_atelier_ids(user),
        allow_admin=allow_admin,
    )


@dataclass(frozen=True)
class EventAccess:
    """How the current user relates to an event."""

    hall: bool = False  # I manage the hall that hosts it
    atelier: bool = False  # it is one of my atelier's events
    assignee: bool = False  # I am (or was invited as) staff on it
    offered: bool = False  # it is open on the marketplace / offered to my atelier
    admin: bool = False

    @property
    def any(self) -> bool:
        return self.hall or self.atelier or self.assignee or self.offered or self.admin

    def allows(self, capacities: Iterable[str]) -> bool:
        return any(getattr(self, capacity) for capacity in capacities)


def event_access(event: Event, user: Any = None) -> EventAccess:
    """Every way *user* is connected to *event* (all False for anonymous users)."""
    account = get_user(user)
    if account is None:
        return EventAccess()
    atelier_ids = my_atelier_ids(account)
    hall = event.hall_id in my_hall_ids(account)
    atelier = event.atelier_id is not None and event.atelier_id in atelier_ids

    assignee = False
    if account.role == Role.STAFF or not (hall or atelier):
        assignee = (
            db.session.execute(
                sa.select(Assignment.id)
                .where(
                    Assignment.event_id == event.id,
                    Assignment.involving_user(account.id),
                    Assignment.status != AssignmentStatus.CANCELLED,
                )
                .limit(1)
            ).first()
            is not None
        )

    offered = False
    if atelier_ids and not atelier:
        if event.offer_mode == OfferMode.OPEN and event.status == EventStatus.OPEN:
            offered = True
        else:
            offered = (
                db.session.execute(
                    sa.select(EventOffer.id)
                    .where(
                        EventOffer.event_id == event.id,
                        EventOffer.atelier_id.in_(sorted(atelier_ids)),
                        EventOffer.status.in_([OfferStatus.PENDING, OfferStatus.ACCEPTED]),
                    )
                    .limit(1)
                ).first()
                is not None
            )
    return EventAccess(hall=hall, atelier=atelier, assignee=assignee, offered=offered, admin=account.role == Role.ADMIN)


def event_or_404(
    event_id: Any,
    *,
    as_: Iterable[str] = ("hall", "atelier"),
    allow_admin: bool = False,
) -> Event:
    """An event I may see in one of the capacities *as_*.

    Capacities: ``"hall"`` (I manage its hall), ``"atelier"`` (my atelier works it),
    ``"assignee"`` (I am staffed on it), ``"offered"`` (open on the marketplace or
    offered to my atelier).  Pick the narrowest set the route needs - e.g. editing
    a ceremony is ``("hall",)``, staffing it is ``("atelier",)``.
    """
    account = _require_user()
    try:
        key = int(event_id)
    except (TypeError, ValueError):
        abort(404)
    event = db.session.get(Event, key)
    if event is None:
        abort(404)
    if allow_admin and account.role == Role.ADMIN:
        return event
    capacities = tuple(as_)
    if not event_access(event, account).allows(capacities):
        abort(404)
    return event


def assignment_or_404(
    assignment_id: Any,
    *,
    as_: Iterable[str] = ("atelier", "assignee"),
    allow_admin: bool = False,
) -> Assignment:
    """An afish row I may touch.

    ``"atelier"``: the event belongs to my atelier; ``"assignee"``: the assignment is
    mine (directly or through a roster entry linked to my account); ``"hall"``: the
    event is in my hall.
    """
    capacities = tuple(as_)

    def check(assignment: Assignment, user: Any) -> bool:
        event = assignment.event
        if "atelier" in capacities and event.atelier_id is not None and event.atelier_id in my_atelier_ids(user):
            return True
        if "hall" in capacities and event.hall_id in my_hall_ids(user):
            return True
        if "assignee" in capacities and assignment.person_user_id == user.id:
            return True
        return False

    return owned_or_404(Assignment, assignment_id, check, allow_admin=allow_admin)


def invoice_or_404(invoice_id: Any, *, allow_admin: bool = False) -> Invoice:
    """An invoice of one of my ateliers (hall managers and staff never see the books)."""
    return owned_or_404(
        Invoice, invoice_id, lambda inv, user: inv.atelier_id in my_atelier_ids(user), allow_admin=allow_admin
    )


def stock_item_or_404(item_id: Any, *, allow_admin: bool = False) -> StockItem:
    """A stock item of one of my ateliers."""
    return owned_or_404(
        StockItem, item_id, lambda item, user: item.atelier_id in my_atelier_ids(user), allow_admin=allow_admin
    )


def equipment_or_404(equipment_id: Any, *, allow_admin: bool = False) -> Equipment:
    """A piece of equipment of one of my ateliers."""
    return owned_or_404(
        Equipment, equipment_id, lambda eq, user: eq.atelier_id in my_atelier_ids(user), allow_admin=allow_admin
    )


def job_post_or_404(post_id: Any, *, allow_admin: bool = False) -> JobPost:
    """A job post published by one of my ateliers (for applicants use the public listing)."""
    return owned_or_404(
        JobPost, post_id, lambda post, user: post.atelier_id in my_atelier_ids(user), allow_admin=allow_admin
    )


def job_application_or_404(
    application_id: Any,
    *,
    as_: Iterable[str] = ("applicant", "atelier"),
    allow_admin: bool = False,
) -> JobApplication:
    """An application I submitted (``"applicant"``) or one sent to my atelier's post (``"atelier"``)."""
    capacities = tuple(as_)

    def check(application: JobApplication, user: Any) -> bool:
        if "applicant" in capacities and application.user_id == user.id:
            return True
        if "atelier" in capacities and application.post.atelier_id in my_atelier_ids(user):
            return True
        return False

    return owned_or_404(JobApplication, application_id, check, allow_admin=allow_admin)


# --------------------------------------------------------------------------
# scoping list queries
# --------------------------------------------------------------------------


def scope_to_my_ateliers(stmt: sa.Select, column: Any) -> sa.Select:
    """``stmt.where(column IN my atelier ids)`` - e.g. ``scope_to_my_ateliers(select(Event), Event.atelier_id)``."""
    return stmt.where(column.in_(sorted(my_atelier_ids())))


def scope_to_my_halls(stmt: sa.Select, column: Any) -> sa.Select:
    """``stmt.where(column IN my hall ids)`` - e.g. ``scope_to_my_halls(select(Event), Event.hall_id)``."""
    return stmt.where(column.in_(sorted(my_hall_ids())))
