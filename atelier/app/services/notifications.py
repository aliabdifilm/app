"""In-app notifications: ``notify`` / ``unread_count`` / ``mark_read``.

Services add rows to the caller's session and ``flush``; they never commit
(the request handler owns the transaction).  A notification may be created for
a person without an account (a roster member): pass ``None`` as the user and
nothing happens, so callers need no special case.

``url`` is optional.  When omitted, an optional resolver registered with
:func:`set_url_resolver` (by the app shell, which knows the routes) is asked to
build it from ``(kind, entity_type, entity_id)``; services therefore never
hard-code URLs.
"""

from __future__ import annotations

import enum
from typing import Any, Callable, Iterable, Optional, Sequence

import sqlalchemy as sa

from app.extensions import db
from app.models.base import utcnow
from app.models.platform import Notification
from app.services.audit import actor_id as _user_id

UrlResolver = Callable[[str, Optional[str], Optional[int]], Optional[str]]

_url_resolver: UrlResolver | None = None

#: Kinds a user can never mute: they concern the job itself (invitations, afish changes, answers to
#: them, event status) or the account.  Every other kind can be switched off in the user's settings
#: (``User.notification_prefs["muted"]``, edited on the staff module's settings page).
ESSENTIAL_KINDS: frozenset[str] = frozenset(
    {
        "assignment_invited",
        "assignment_changed",
        "assignment_response",
        "event_status",
        "offer_received",
        "offer_response",
        "account",
    }
)


def is_muted(user_id: int, kind: str) -> bool:
    """Has the user switched this (non-essential) kind off?  Unknown users are not muted."""
    if kind in ESSENTIAL_KINDS:
        return False
    from app.models.user import User

    user = db.session.get(User, user_id)
    prefs = getattr(user, "notification_prefs", None) or {}
    muted = prefs.get("muted") if isinstance(prefs, dict) else None
    return isinstance(muted, (list, tuple)) and kind in muted


def set_url_resolver(resolver: UrlResolver | None) -> None:
    """Register ``resolver(kind, entity_type, entity_id) -> url | None`` (or clear it)."""
    global _url_resolver
    _url_resolver = resolver


def _kind_text(kind: Any) -> str:
    return str(kind.value if isinstance(kind, enum.Enum) else kind).strip()


def _entity_ref(entity: Any) -> tuple[Optional[str], Optional[int]]:
    if entity is None:
        return None, None
    if isinstance(entity, (tuple, list)) and len(entity) == 2:
        return str(entity[0]), (None if entity[1] is None else int(entity[1]))
    return getattr(entity, "__tablename__", None), getattr(entity, "id", None)


def notify(
    user_or_id: Any,
    kind: Any,
    title: str,
    body: str = "",
    url: Optional[str] = None,
    *,
    entity: Any = None,
) -> Optional[Notification]:
    """Create an unread notification for a user; returns it (or None for no user).

    Returns ``None`` as well when the user muted this (non-essential) kind, see :data:`ESSENTIAL_KINDS`.
    *entity* (a model instance or ``(table, id)``) records what the
    notification is about and lets the URL resolver build a link.
    """
    user_id = _user_id(user_or_id)
    if user_id is None:
        return None
    kind_text = _kind_text(kind)
    if not kind_text:
        raise ValueError("notification kind is required")
    if not title or not str(title).strip():
        raise ValueError("notification title is required")
    if is_muted(user_id, kind_text):
        return None
    entity_type, entity_id = _entity_ref(entity)
    if url is None and _url_resolver is not None:
        try:
            url = _url_resolver(kind_text, entity_type, entity_id)
        except Exception:  # a bad resolver must not lose the notification
            url = None
    row = Notification(
        user_id=user_id,
        kind=kind_text[:40],
        title=str(title).strip()[:200],
        body=body or "",
        url=url,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def notify_many(
    users: Iterable[Any],
    kind: Any,
    title: str,
    body: str = "",
    url: Optional[str] = None,
    *,
    entity: Any = None,
) -> list[Notification]:
    """:func:`notify` for several recipients (duplicates and None are skipped)."""
    seen: set[int] = set()
    rows: list[Notification] = []
    for user in users:
        user_id = _user_id(user)
        if user_id is None or user_id in seen:
            continue
        seen.add(user_id)
        row = notify(user_id, kind, title, body, url, entity=entity)
        if row is not None:
            rows.append(row)
    return rows


def unread_count(user_or_id: Any) -> int:
    """Number of unread notifications of a user (0 for no user)."""
    user_id = _user_id(user_or_id)
    if user_id is None:
        return 0
    return int(
        db.session.execute(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id == user_id, Notification.read_at.is_(None)
            )
        ).scalar_one()
    )


def recent(user_or_id: Any, limit: int = 20, *, unread_only: bool = False) -> Sequence[Notification]:
    """Newest-first notifications of a user."""
    user_id = _user_id(user_or_id)
    if user_id is None:
        return []
    stmt = sa.select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    return db.session.execute(stmt).scalars().all()


def mark_read(user_or_id: Any, notification_ids: Iterable[int] | int | None = None) -> int:
    """Mark notifications read; returns how many changed.

    Only the user's own unread notifications are touched, so passing someone
    else's ids is harmless.  ``notification_ids=None`` marks everything read.
    """
    user_id = _user_id(user_or_id)
    if user_id is None:
        return 0
    stmt = (
        sa.update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )
    if notification_ids is not None:
        ids = [notification_ids] if isinstance(notification_ids, int) else list(notification_ids)
        if not ids:
            return 0
        stmt = stmt.where(Notification.id.in_(ids))
    result = db.session.execute(stmt)
    db.session.flush()
    return int(result.rowcount or 0)


def mark_all_read(user_or_id: Any) -> int:
    return mark_read(user_or_id, None)
