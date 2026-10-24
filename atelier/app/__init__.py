"""Atelier Sayyar (اتلیه سیار) - application package.

``create_app()`` builds the Flask application:

1. configuration from the environment (:mod:`app.config`),
2. extensions (SQLAlchemy, Flask-Login, CSRF), models, SMS provider, rate limiter,
3. Jinja filters / globals and the shell context processor (menu, unread count),
4. security headers,
5. every package in :mod:`app.modules` (``main``, ``auth``, ``halls`` ...),
6. ``db.create_all()`` for development and tests (production uses ``flask init-db``;
   Alembic migrations are a documented future step, see docs/ARCHITECTURE.md),
7. the ``flask`` CLI commands (:mod:`app.cli`).

Usage::

    from app import create_app
    app = create_app()                       # environment from ATELIER_ENV (default: development)
    app = create_app("production")
    app = create_app({"WTF_CSRF_ENABLED": True}, )   # overrides on top of the env-selected config
    app = create_app("testing", SQLALCHEMY_DATABASE_URI="sqlite://")
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from flask import Flask, flash, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import event as sa_event
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BuildError

from app.config import load_config
from app.extensions import csrf, db, get_state, login_manager

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

LOGIN_MESSAGE = "برای دسترسی به این صفحه وارد شوید."


# --------------------------------------------------------------------------
# Jinja
# --------------------------------------------------------------------------


def _register_template_helpers(app: Flask) -> None:
    from app.services import jalali, money
    from app.services.constants import ALL_ENUMS, label_fa
    from app.services.uploads import media_url

    env = app.jinja_env

    def toman(value: Any, suffix: bool = False) -> str:
        """``1870000`` -> ``۱٬۸۷۰٬۰۰۰`` (``suffix=True`` appends «تومان»); ``None`` shows ۰."""
        return money.format_toman(value, persian_digits=True, suffix=suffix)

    def fa(value: Any) -> str:
        """Any value with its digits turned into Persian digits (``None`` -> ``''``)."""
        return money.to_persian_digits("" if value is None else str(value))

    def jdate(value: Any, fmt: str = "numeric") -> str:
        """Jalali date, e.g. ``۱۴۰۴/۰۴/۲۶``; fmt: numeric | short | long | weekday_long | month_year | weekday.

        A naive ``datetime`` is a UTC timestamp and is converted to Iran time first.
        """
        return jalali.to_jalali_str(
            value, fmt, persian_digits=True, assume_utc=isinstance(value, dt.datetime)
        )

    def jdatetime(value: Any, fmt: str = "numeric", seconds: bool = False) -> str:
        """Jalali date and Iran time of a UTC timestamp, e.g. ``۱۴۰۴/۰۴/۲۶ ۱۸:۳۰``."""
        return jalali.to_jalali_datetime_str(value, fmt, persian_digits=True, seconds=seconds)

    def jweekday(value: Any) -> str:
        """Persian weekday name (``جمعه``) of a date / datetime / ISO string."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""
        return jalali.weekday_name(value)

    env.filters.update(toman=toman, fa=fa, jdate=jdate, jdatetime=jdatetime, jweekday=jweekday)
    env.filters["label_fa"] = lambda value, group: label_fa(group, value)
    env.globals["media_url"] = media_url
    env.globals["label_fa"] = label_fa
    for enum_cls in ALL_ENUMS:  # Role.choices(), Skill.choices(), EventStatus.DONE ... in any template
        env.globals.setdefault(enum_cls.__name__, enum_cls)


def _safe_url(endpoint: str, fallback: str, **values: Any) -> str:
    try:
        return url_for(endpoint, **values)
    except BuildError:
        return fallback


def _register_shell_context(app: Flask) -> None:
    """Variables every template gets (``base.html`` reads most of them; see docs/UI_GUIDE.md)."""
    from app.services import nav, notifications, security

    @app.context_processor
    def inject_shell() -> dict[str, Any]:
        context: dict[str, Any] = {
            "platform_name": app.config.get("PLATFORM_NAME", "اتلیه سیار"),
            "asset_version": app.config.get("ASSET_VERSION", "1"),
            "home_url": _safe_url("main.landing", "/"),
            "login_url": _safe_url("auth.login", "/auth/login"),
            "register_url": _safe_url("auth.register", "/auth/register"),
            "logout_url": _safe_url("auth.logout", "/auth/logout"),
            "notifications_url": _safe_url("main.notifications", "/notifications"),
            "dashboard_url": _safe_url("main.dashboard", "/dashboard"),
            "sw_url": _safe_url("main.service_worker", "/static/js/sw.js"),
            "nav_items": [],
            "unread_count": 0,
        }
        from flask import g

        if g.get("minimal_shell"):  # the 500 page must not touch the database
            return context
        try:
            context["today"] = security.today()
            if current_user.is_authenticated:
                user = current_user._get_current_object()
                context["nav_items"] = nav.menu_for(user, endpoint=request.endpoint, path=request.path)
                context["unread_count"] = notifications.unread_count(user)
        except Exception:  # a failing menu or database hiccup must not break rendering the error page
            logger.exception("could not build the page shell")
            try:
                db.session.rollback()
            except Exception:  # pragma: no cover
                pass
        return context

    @app.shell_context_processor
    def _shell() -> dict[str, Any]:
        from app import models

        names = {name: getattr(models, name) for name in models.__all__ if name[:1].isupper()}
        return {"db": db, **names}


# --------------------------------------------------------------------------
# login manager
# --------------------------------------------------------------------------


def _unauthorized():
    """Anonymous visitor hit a protected page: JSON gets 401, browsers go to the login page."""
    from app.services.http import json_error, referrer_path, wants_json
    from app.services.security import is_safe_next

    if wants_json():
        return json_error("نشست شما منقضی شده است؛ دوباره وارد شوید.", 401, "unauthorized")
    flash(LOGIN_MESSAGE, "info")
    target: Optional[str] = None
    if request.method in ("GET", "HEAD"):
        target = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    else:
        target = referrer_path()
    values = {"next": target} if target and is_safe_next(target) else {}
    try:
        return redirect(url_for("auth.login", **values))
    except BuildError:
        from flask import abort

        abort(401)


def _init_login(app: Flask) -> None:
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = LOGIN_MESSAGE
    login_manager.login_message_category = "info"
    login_manager.session_protection = app.config.get("SESSION_PROTECTION", "strong")
    login_manager.unauthorized_handler(_unauthorized)


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------


def _tune_sqlite(app: Flask) -> None:
    """File-based SQLite: WAL + busy timeout so concurrent workers do not trip over each other."""
    url = str(app.config["SQLALCHEMY_DATABASE_URI"])
    if not url.startswith("sqlite") or url == "sqlite://" or ":memory:" in url:
        return
    with app.app_context():
        engine = db.engine

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
        finally:
            cursor.close()


def _prepare_instance(app: Flask) -> None:
    """Create the folder of a file-based SQLite database (nothing for memory / server databases).

    The upload folder is created lazily by the first upload, so tests leave no directories behind.
    """
    import os

    from sqlalchemy.engine import make_url

    url = make_url(str(app.config["SQLALCHEMY_DATABASE_URI"]))
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        try:
            os.makedirs(os.path.dirname(os.path.abspath(url.database)), exist_ok=True)
        except OSError:
            logger.warning("could not create the database folder for %s", url.database)


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def create_app(config: Any = None, **overrides: Any) -> Flask:
    """Build a configured application.

    *config*: ``None`` (environment decides), an environment name
    (``"development"``, ``"testing"``, ``"production"``), a config class, or a
    mapping of overrides.  Keyword arguments are overrides as well.
    """
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(load_config(config, overrides, instance_path=app.instance_path))
    app.url_map.strict_slashes = False
    app.json.ensure_ascii = False  # readable Persian in JSON responses
    logging.getLogger("app").setLevel(app.config.get("LOG_LEVEL", "INFO"))

    hops = int(app.config.get("TRUST_PROXY_HOPS") or 0)
    if hops > 0:  # behind nginx / a platform router: trust X-Forwarded-For/Proto/Host
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)  # type: ignore[method-assign]

    _prepare_instance(app)

    # extensions + models (importing app.models registers every table and the user loader)
    db.init_app(app)
    csrf.init_app(app)
    _init_login(app)
    from app import models  # noqa: F401

    _tune_sqlite(app)

    # per-app runtime state: SMS provider, rate limiter, clock
    from app.services import security, sms

    state = get_state(app)
    state.rate_limiter = security.RateLimiter()
    sms.init_app(app)
    if app.config["ENV_NAME"] == "production" and app.config["SMS_PROVIDER"] == "console":
        logger.warning("SMS_PROVIDER=console in production: login codes are only written to the log")

    _register_template_helpers(app)
    _register_shell_context(app)
    security.init_security_headers(app)

    from app.modules import register_modules

    register_modules(app)

    if app.config.get("AUTO_CREATE_DB"):
        # Development / tests only.  Production creates the schema once with `flask init-db`;
        # versioned Alembic migrations are a documented future step (docs/ARCHITECTURE.md).
        with app.app_context():
            db.create_all()

    from app import cli

    cli.init_app(app)
    return app
