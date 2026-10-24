"""Audit trail helper: ``audit.log(actor, action, entity, data)``.

Writes an append-only :class:`~app.models.platform.AuditLog` row in the
caller's transaction (``add`` + ``flush``; never commits).  Conventions:

* ``action`` is a dotted verb, e.g. ``assignment.created``,
  ``assignment.accepted``, ``event.cancelled``, ``invoice.submitted``.
* ``entity`` is a model instance (type = its table name, id = its primary
  key), a ``(table_name, id)`` pair, or None for non-entity actions (login).
* ``data`` is small JSON-able detail.  Values are made JSON-safe (dates,
  enums, decimals, models) and secrets under well-known key names
  (``password``, ``code``, ``token``, ...) are replaced with ``***``.
* ``actor`` is a User, a user id, or None (system).
* ``ip`` defaults to the current request's address when called inside a
  request, else None.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
from typing import Any, Mapping

from flask import has_request_context, request

from app.extensions import db
from app.models.platform import AuditLog

REDACTED_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "old_password",
        "code",
        "otp",
        "code_hash",
        "token",
        "secret",
        "api_key",
        "authorization",
    }
)
_MAX_DEPTH = 6
_MAX_STRING = 2000


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Convert *value* into something ``json.dumps`` accepts (best effort)."""
    if depth > _MAX_DEPTH:
        return "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, enum.Enum):
        return _jsonable(value.value, depth + 1)
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "..."
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): ("***" if str(key).lower() in REDACTED_KEYS else _jsonable(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, depth + 1) for item in value]
    table = getattr(value, "__tablename__", None)
    if table is not None:
        return {"type": table, "id": getattr(value, "id", None)}
    return str(value)


def _entity_ref(entity: Any) -> tuple[str | None, int | None]:
    if entity is None:
        return None, None
    if isinstance(entity, (tuple, list)) and len(entity) == 2:
        return str(entity[0]), (None if entity[1] is None else int(entity[1]))
    table = getattr(entity, "__tablename__", None)
    if table is None:
        raise TypeError(f"cannot audit entity of type {type(entity).__name__}")
    return table, getattr(entity, "id", None)


def actor_id(actor: Any) -> int | None:
    """User id of *actor* (a User, an int id, None, or an anonymous proxy)."""
    if actor is None or isinstance(actor, bool):
        return None
    if isinstance(actor, int):
        return actor
    if getattr(actor, "is_authenticated", True) is False:
        return None
    value = getattr(actor, "id", None)
    return int(value) if value is not None else None


def _request_ip() -> str | None:
    if has_request_context():
        return (request.remote_addr or None)
    return None


def log(
    actor: Any,
    action: str,
    entity: Any = None,
    data: Any = None,
    *,
    ip: str | None = None,
) -> AuditLog:
    """Record one audit entry in the current session and flush it.

    Returns the :class:`AuditLog` row (with its id).  Because the entity id is
    read here, flush the entity first when logging its creation (all services
    in this package do).
    """
    if not action or not str(action).strip():
        raise ValueError("audit action is required")
    entity_type, entity_id = _entity_ref(entity)
    payload = _jsonable(data) if data is not None else None
    row = AuditLog(
        actor_id=actor_id(actor),
        action=str(action).strip()[:80],
        entity_type=entity_type,
        entity_id=entity_id,
        data=payload,
        ip=(ip or _request_ip()),
    )
    db.session.add(row)
    db.session.flush()
    return row
