"""Navigation registry: how modules put themselves into the sidebar and the dashboard.

Every module registers its menu entries in its ``__init__.py`` (at import time)::

    from app.services import nav
    from app.services.constants import Role

    nav.register(Role.ATELIER_MANAGER, "تقویم مراسم‌ها", "ateliers.calendar", icon="calendar", order=20)
    nav.register((Role.ATELIER_MANAGER, Role.ADMIN), "انبار", "inventory.index", icon="box", order=60,
                 badge=lambda user: low_stock_count(user))          # '' / 0 / None hides the badge
    nav.register_home(Role.ATELIER_MANAGER, "ateliers.dashboard")    # where /dashboard sends this role
    nav.register_onboarding(Role.ATELIER_MANAGER, "ateliers.setup",  # "complete your profile" step
                            is_complete=lambda user: has_atelier(user))

The app shell turns the registry into the ``nav_items`` list that ``base.html``
renders (:func:`menu_for`), filtered by ``current_user.role`` and with exactly
one item marked ``active`` (longest matching URL prefix, or an endpoint listed
in ``active=``).

Order ranges (so menus read the same for every role): 10 home / dashboard,
20-60 daily work (calendar, events, staff, accounting), 70-80 marketplace and
inventory, 90+ settings and profile.

The registry is process-global like the event bus.  Registering the same
``(endpoint, roles)`` twice replaces the entry, so re-importing is harmless.
Tests use :func:`isolated` to start from an empty registry.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional
from urllib.parse import urlsplit

from flask import url_for
from werkzeug.routing import BuildError

from app.services.constants import Role
from app.services.money import to_persian_digits

logger = logging.getLogger(__name__)

RolesArg = Any  # Role | str | Iterable[Role | str] | None


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NavItem:
    """One menu entry.  ``roles=None`` means every authenticated user."""

    label: str
    endpoint: str
    roles: Optional[frozenset[str]] = None
    icon: str = "list"
    order: int = 100
    badge: Optional[Callable[[Any], Any]] = None
    values: tuple[tuple[str, Any], ...] = ()
    active: tuple[str, ...] = ()  # extra endpoints (or "prefix.*") that light this item up

    def url(self) -> str:
        return url_for(self.endpoint, **dict(self.values))

    def allows(self, role: Any) -> bool:
        if self.roles is None:
            return True
        return _role_value(role) in self.roles


@dataclass(frozen=True)
class Onboarding:
    roles: frozenset[str]
    endpoint: str
    is_complete: Callable[[Any], bool]
    values: tuple[tuple[str, Any], ...] = ()


@dataclass
class _Registry:
    items: dict[tuple[str, Optional[frozenset[str]]], NavItem] = field(default_factory=dict)
    homes: dict[str, tuple[str, tuple[tuple[str, Any], ...]]] = field(default_factory=dict)
    onboarding: list[Onboarding] = field(default_factory=list)


_registry = _Registry()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _role_value(role: Any) -> str:
    return Role.coerce(role).value


def _roles(roles: RolesArg) -> Optional[frozenset[str]]:
    if roles is None:
        return None
    if isinstance(roles, (str, Role)):
        roles = [roles]
    values = frozenset(_role_value(role) for role in roles)
    if not values:
        raise ValueError("roles must not be empty (use None for every authenticated user)")
    return values


def _values(values: Optional[Mapping[str, Any]]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((values or {}).items()))


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def register(
    roles: RolesArg,
    label: str,
    endpoint: str,
    icon: str = "list",
    order: int = 100,
    badge: Optional[Callable[[Any], Any]] = None,
    *,
    values: Optional[Mapping[str, Any]] = None,
    active: Iterable[str] = (),
) -> NavItem:
    """Add a menu entry visible to *roles* (a role, several, or None for everyone)."""
    if not label or not endpoint:
        raise ValueError("label and endpoint are required")
    item = NavItem(
        label=label,
        endpoint=endpoint,
        roles=_roles(roles),
        icon=icon,
        order=order,
        badge=badge,
        values=_values(values),
        active=tuple(active),
    )
    _registry.items[(endpoint, item.roles)] = item
    return item


def register_home(roles: RolesArg, endpoint: str, **values: Any) -> None:
    """Where ``/dashboard`` sends users of *roles* (first registration per role wins)."""
    for role in _roles(roles) or {r.value for r in Role}:
        _registry.homes.setdefault(role, (endpoint, _values(values)))


def register_onboarding(
    roles: RolesArg,
    endpoint: str,
    is_complete: Callable[[Any], bool],
    **values: Any,
) -> None:
    """Declare the "complete your profile" page of *roles* and how to tell it is done."""
    role_set = _roles(roles) or frozenset(r.value for r in Role)
    _registry.onboarding = [
        o for o in _registry.onboarding if not (o.endpoint == endpoint and o.roles == role_set)
    ]
    _registry.onboarding.append(Onboarding(role_set, endpoint, is_complete, _values(values)))


def unregister(endpoint: str) -> int:
    """Remove every entry for *endpoint*; returns how many were removed."""
    keys = [key for key in _registry.items if key[0] == endpoint]
    for key in keys:
        del _registry.items[key]
    return len(keys)


def clear() -> None:
    """Empty the registry (tests; prefer :func:`isolated`)."""
    _registry.items.clear()
    _registry.homes.clear()
    _registry.onboarding.clear()


@contextmanager
def isolated() -> Iterator[None]:
    """Run a block with an empty registry, then restore the previous one."""
    saved = (dict(_registry.items), dict(_registry.homes), list(_registry.onboarding))
    clear()
    try:
        yield
    finally:
        _registry.items.clear()
        _registry.items.update(saved[0])
        _registry.homes.clear()
        _registry.homes.update(saved[1])
        _registry.onboarding[:] = saved[2]


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------


def items_for(user: Any) -> list[NavItem]:
    """Registered items visible to *user*'s role, in menu order."""
    role = getattr(user, "role", None)
    if role is None or not getattr(user, "is_authenticated", False):
        return []
    visible = [item for item in _registry.items.values() if item.allows(role)]
    return sorted(visible, key=lambda item: (item.order, item.label, item.endpoint))


def _path_of(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def _endpoint_matches(patterns: Iterable[str], endpoint: Optional[str]) -> bool:
    if not endpoint:
        return False
    for pattern in patterns:
        if pattern.endswith("*"):
            if endpoint.startswith(pattern[:-1]):
                return True
        elif endpoint == pattern:
            return True
    return False


def _badge_text(item: NavItem, user: Any) -> Optional[str]:
    if item.badge is None:
        return None
    try:
        value = item.badge(user)
    except Exception:  # a failing badge must never break page rendering
        logger.exception("nav badge for %s failed", item.endpoint)
        return None
    if value in (None, "", 0, False):
        return None
    if isinstance(value, int):
        return to_persian_digits("99+" if value > 99 else str(value))
    return str(value)


def menu_for(
    user: Any,
    *,
    endpoint: Optional[str] = None,
    path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """The ``nav_items`` list for ``base.html``: dicts with label/url/icon/active/badge.

    *endpoint* and *path* describe the current request.  Exactly one item is
    ``active``: an item whose endpoint (or ``active=`` pattern) is the current
    endpoint wins, otherwise the item with the longest URL path that is a
    prefix of *path*.  Items whose URL cannot be built (module not loaded,
    typo) are skipped with a warning instead of raising.
    """
    built: list[tuple[NavItem, str]] = []
    for item in items_for(user):
        try:
            built.append((item, item.url()))
        except BuildError:
            logger.warning("nav item %r points to unknown endpoint %r", item.label, item.endpoint)
    current_path = _path_of(path) if path else None

    best_index = -1
    best_score = -1
    for index, (item, url) in enumerate(built):
        score = -1
        if endpoint and (item.endpoint == endpoint or _endpoint_matches(item.active, endpoint)):
            score = 10_000 + len(_path_of(url))
        elif current_path:
            item_path = _path_of(url)
            if item_path == current_path or (
                item_path != "/" and current_path.startswith(item_path + "/")
            ):
                score = len(item_path)
        if score > best_score:
            best_index, best_score = index, score

    return [
        {
            "label": item.label,
            "url": url,
            "icon": item.icon,
            "active": index == best_index,
            "badge": _badge_text(item, user),
            "endpoint": item.endpoint,
        }
        for index, (item, url) in enumerate(built)
    ]


def home_url(user: Any) -> Optional[str]:
    """URL of the role's registered home page, or None."""
    role = getattr(user, "role", None)
    if role is None:
        return None
    entry = _registry.homes.get(_role_value(role))
    if entry is None:
        return None
    endpoint, values = entry
    try:
        return url_for(endpoint, **dict(values))
    except BuildError:
        logger.warning("home endpoint %r is not registered", endpoint)
        return None


def onboarding_url(user: Any) -> Optional[str]:
    """URL of the first unfinished onboarding step for *user*, or None when complete."""
    role = getattr(user, "role", None)
    if role is None:
        return None
    role_value = _role_value(role)
    for step in _registry.onboarding:
        if role_value not in step.roles:
            continue
        try:
            done = bool(step.is_complete(user))
        except Exception:
            logger.exception("onboarding check %s failed", step.endpoint)
            continue
        if done:
            continue
        try:
            return url_for(step.endpoint, **dict(step.values))
        except BuildError:
            logger.warning("onboarding endpoint %r is not registered", step.endpoint)
    return None


def has_onboarding(user: Any) -> bool:
    """Is any onboarding step registered for the user's role (complete or not)?"""
    role = getattr(user, "role", None)
    return role is not None and any(_role_value(role) in step.roles for step in _registry.onboarding)
