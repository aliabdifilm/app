"""In-process publish/subscribe bus decoupling modules.

Modules announce *what happened* (``invoice.saved``) without knowing who cares
(inventory deducting stock, notifications, reports ...).  Delivery is
synchronous, in the publisher's thread, inside the publisher's database
transaction - so a subscriber sees the same uncommitted state - and
**subscriber failures are logged and swallowed**: a broken listener must never
break the code path that published.

Known event names and payloads (all values are plain ids / scalars; re-load
rows with ``db.session.get`` if you need them):

``invoice.saved``
    ``invoice_id, event_id, atelier_id, actor_id, created`` - an invoice (and its
    lines / payments / expenses / payroll) was created or re-saved.
``assignment.changed``
    ``assignment_id, event_id, change, old_status, new_status, actor_id,
    user_id`` - ``change`` is one of ``created | status | terms | locked |
    unlocked``; ``user_id`` is the assigned person's account (or None).
``event.status_changed``
    ``event_id, old_status, new_status, actor_id``.
``equipment.assigned``
    ``event_id, equipment_id, usage_id, actor_id, removed`` - equipment was
    attached to (``removed=False``) or detached from an event.

Handlers receive the payload as keyword arguments.  A handler may declare only
the parameters it needs - unknown keys are dropped unless it has ``**kwargs``::

    @subscribe("invoice.saved")
    def deduct_stock(invoice_id, **_):
        ...

Rules for handlers: do not ``commit`` or ``rollback`` (the publisher owns the
transaction); wrap risky database work in ``db.session.begin_nested()`` so a
failure cannot poison the publisher's session; keep them fast.

``app.create_app`` is called many times in tests, so :func:`subscribe` is
idempotent for the same function and event name.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

INVOICE_SAVED = "invoice.saved"
ASSIGNMENT_CHANGED = "assignment.changed"
EVENT_STATUS_CHANGED = "event.status_changed"
EQUIPMENT_ASSIGNED = "equipment.assigned"

KNOWN_EVENTS: dict[str, str] = {
    INVOICE_SAVED: "invoice_id, event_id, atelier_id, actor_id, created",
    ASSIGNMENT_CHANGED: "assignment_id, event_id, change, old_status, new_status, actor_id, user_id",
    EVENT_STATUS_CHANGED: "event_id, old_status, new_status, actor_id",
    EQUIPMENT_ASSIGNED: "event_id, equipment_id, usage_id, actor_id, removed",
}

Handler = Callable[..., Any]

_lock = threading.RLock()
_subscribers: dict[str, list[Handler]] = defaultdict(list)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one :func:`publish` call."""

    delivered: int
    failed: int

    @property
    def total(self) -> int:
        return self.delivered + self.failed


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("event name must be a non-empty string")
    return name.strip()


def subscribe(name: str, fn: Handler | None = None):
    """Register *fn* for event *name*; also usable as ``@subscribe(name)``.

    Registering the same function twice for the same name is a no-op.
    Returns *fn* (so the decorator form leaves the function usable).
    """
    name = _check_name(name)
    if fn is None:

        def decorator(func: Handler) -> Handler:
            subscribe(name, func)
            return func

        return decorator
    if not callable(fn):
        raise TypeError("subscriber must be callable")
    with _lock:
        if fn not in _subscribers[name]:
            _subscribers[name].append(fn)
    return fn


def unsubscribe(name: str, fn: Handler) -> bool:
    """Remove *fn* from *name*; returns whether it was registered."""
    name = _check_name(name)
    with _lock:
        try:
            _subscribers[name].remove(fn)
        except ValueError:
            return False
        return True


def subscribers(name: str) -> tuple[Handler, ...]:
    """Snapshot of the handlers currently registered for *name*."""
    with _lock:
        return tuple(_subscribers.get(_check_name(name), ()))


def clear(name: str | None = None) -> None:
    """Drop all handlers of *name* (or of every event). Mostly for tests."""
    with _lock:
        if name is None:
            _subscribers.clear()
        else:
            _subscribers.pop(_check_name(name), None)


@contextmanager
def isolated() -> Iterator[None]:
    """Run a block with an empty registry, then restore the previous handlers.

    For tests: modules register their handlers at import time, so tests must
    not call :func:`clear` (it would silently unplug them for later tests).
    """
    with _lock:
        saved = {name: list(handlers) for name, handlers in _subscribers.items()}
        _subscribers.clear()
    try:
        yield
    finally:
        with _lock:
            _subscribers.clear()
            _subscribers.update(saved)


def _accepts_var_kwargs(fn: Handler) -> tuple[bool, set[str] | None]:
    """(accepts **kwargs, named parameters) for *fn*; named is None if unknown."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # builtins / odd callables: pass everything
        return True, None
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    names = {
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return var_kw, names


def _call(fn: Handler, payload: dict[str, Any]) -> None:
    var_kw, names = _accepts_var_kwargs(fn)
    if var_kw or names is None:
        fn(**payload)
    else:
        fn(**{k: v for k, v in payload.items() if k in names})


def publish(name: str, **payload: Any) -> PublishResult:
    """Call every handler of *name* with *payload*; never raises.

    Handlers run in registration order.  An exception in one handler is logged
    (with traceback) and the remaining handlers still run.  Handlers added or
    removed while publishing do not affect the current delivery.
    """
    name = _check_name(name)
    with _lock:
        targets = list(_subscribers.get(name, ()))
    delivered = failed = 0
    for fn in targets:
        try:
            _call(fn, payload)
        except Exception:  # noqa: BLE001 - isolation is the whole point
            failed += 1
            logger.exception(
                "eventbus: subscriber %s failed for %r",
                getattr(fn, "__qualname__", repr(fn)),
                name,
            )
        else:
            delivered += 1
    return PublishResult(delivered=delivered, failed=failed)
