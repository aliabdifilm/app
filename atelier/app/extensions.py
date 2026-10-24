"""Flask extension singletons for Atelier Sayyar.

This module is intentionally tiny and import-cycle free: every other module
(models, services, blueprints) imports the singletons from here, and the
application factory wires them with ``init_app``.

Exports (stable): ``db``, ``login_manager``, ``csrf``.

``db`` is built on a typed SQLAlchemy 2.0 ``DeclarativeBase`` whose metadata
carries a *naming convention*.  Deterministic constraint names matter for
PostgreSQL portability (identifiers are capped at 63 characters) and for
writing migrations later without guessing auto-generated names.
"""

from typing import Any, Callable, Optional

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every model (``db.Model`` derives from it)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
csrf = CSRFProtect()


class AtelierState:
    """Per-application runtime state, kept in ``app.extensions["atelier"]``.

    Created by :func:`app.create_app`.  Services reach it through
    :func:`get_state` instead of module globals so that two apps (tests, the
    ``routes-matrix`` command) never share counters, clocks or providers.

    * ``clock``          zero-argument callable returning "now" (UTC); tests freeze it
    * ``sms``            the configured :class:`app.services.sms.SmsProvider`
    * ``rate_limiter``   in-process sliding-window limiter (:mod:`app.services.security`)
    * ``modules``        names of the modules that were registered
    * ``module_errors``  ``{module: traceback text}`` for modules that failed to load
    * ``media_kinds``    upload kinds served by ``/media`` (:mod:`app.services.uploads`)
    """

    def __init__(self) -> None:
        self.clock: Optional[Callable[[], Any]] = None
        self.sms: Any = None
        self.rate_limiter: Any = None
        self.modules: list[str] = []
        self.module_errors: dict[str, str] = {}
        self.media_kinds: dict[str, Any] = {}


def get_state(app: Any = None) -> AtelierState:
    """The :class:`AtelierState` of *app* (default: ``current_app``)."""
    if app is None:
        from flask import current_app

        app = current_app
    state = app.extensions.get("atelier")
    if state is None:
        state = app.extensions["atelier"] = AtelierState()
    return state


__all__ = ["db", "login_manager", "csrf", "AtelierState", "get_state"]
