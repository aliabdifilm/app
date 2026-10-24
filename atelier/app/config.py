"""Configuration for Atelier Sayyar.

Three environments, picked by ``ATELIER_ENV`` (or ``FLASK_ENV``):
``development`` (default), ``testing`` and ``production``.  Everything that
deployers need to change is read from environment variables, so there is no
config file to edit on a VPS (twelve-factor style):

==============================  ==================================================
``ATELIER_ENV``                 ``development`` | ``testing`` | ``production``
``SECRET_KEY``                  **required in production** (>= 32 random chars);
                                development generates and remembers one in ``instance/``
``DATABASE_URL``                SQLAlchemy URL; default ``sqlite:///instance/atelier.db``.
                                ``postgres://`` is rewritten to ``postgresql://``
``UPLOAD_DIR``                  where user images are stored (default ``instance/uploads``,
                                never inside ``static/``)
``MAX_CONTENT_LENGTH``          request size cap in bytes (default 8 MiB)
``MAX_UPLOAD_BYTES``            per-image cap in bytes (default 5 MiB)
``SMS_PROVIDER``                ``console`` (default; logs the message) | ``kavenegar``
``KAVENEGAR_API_KEY``           required for the kavenegar provider
``KAVENEGAR_OTP_TEMPLATE``      Kavenegar "verify lookup" template name (preferred)
``KAVENEGAR_SENDER``            sender line, used when no template is configured
``OTP_DEBUG_SHOW``              show the OTP on the verify page. **Development only**;
                                production refuses to start with it enabled
``REGISTRATION_ROLES``          comma list of self-service roles
                                (default ``staff,hall_manager,atelier_manager``)
``REQUIRE_APPROVAL_ROLES``      roles whose new accounts stay ``pending`` until an
                                admin activates them (default: none)
``TRUST_PROXY_HOPS``            number of reverse proxies in front (enables ProxyFix)
``SESSION_COOKIE_SECURE``       ``1``/``0``; default on in production
``SITE_DOMAIN``                 optional, e.g. ``atelier.example.ir`` (Web OTP line in SMS)
``DISABLED_MODULES``            comma list of ``app/modules/<name>`` packages to skip
``LOG_LEVEL``                   default ``INFO`` (``DEBUG`` in development)
==============================  ==================================================

Use :func:`load_config` (called by :func:`app.create_app`) rather than reading
the classes directly: it applies the environment, validates production
settings and fills in the instance-relative defaults.
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "BaseConfig",
    "DevelopmentConfig",
    "TestingConfig",
    "ProductionConfig",
    "ConfigError",
    "CONFIG_BY_NAME",
    "resolve_config_class",
    "load_config",
]

MIB = 1024 * 1024

_PLACEHOLDER_SECRETS = frozenset(
    {"", "change-me", "changeme", "secret", "dev", "development", "test", "testing", "password"}
)


class ConfigError(RuntimeError):
    """The configuration is unusable (raised at startup, never at request time)."""


# --------------------------------------------------------------------------
# environment helpers
# --------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _fix_database_url(url: str) -> str:
    """Heroku/Render style ``postgres://`` -> the dialect SQLAlchemy understands."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


# --------------------------------------------------------------------------
# config classes (plain defaults; the environment is applied by load_config)
# --------------------------------------------------------------------------


class BaseConfig:
    """Defaults shared by every environment."""

    ENV_NAME = "base"
    DEBUG = False
    TESTING = False

    # identity
    PLATFORM_NAME = "اتلیه سیار"
    SITE_DOMAIN: Optional[str] = None
    ASSET_VERSION = "1"  # bump per release; appended as ?v= to CSS/JS

    # keys / database
    SECRET_KEY: Optional[str] = None
    SQLALCHEMY_DATABASE_URI: Optional[str] = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {"pool_pre_ping": True}
    AUTO_CREATE_DB = False  # db.create_all() at startup (development / testing only)

    # requests and uploads
    MAX_CONTENT_LENGTH = 8 * MIB
    MAX_UPLOAD_BYTES = 5 * MIB
    MAX_IMAGE_PIXELS = 64_000_000
    UPLOAD_DIR: Optional[str] = None
    SEND_FILE_MAX_AGE_DEFAULT = timedelta(hours=1)

    # sessions, cookies, CSRF
    SESSION_COOKIE_NAME = "atelier_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    REMEMBER_COOKIE_NAME = "atelier_remember"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = False
    SESSION_PROTECTION = "strong"
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 8 * 60 * 60  # seconds; a form left open overnight must still post
    TRUST_PROXY_HOPS = 0
    HSTS_SECONDS = 0  # production: one year

    # one-time passwords (assumption A6)
    SMS_PROVIDER = "console"
    KAVENEGAR_API_KEY: Optional[str] = None
    KAVENEGAR_OTP_TEMPLATE: Optional[str] = None
    KAVENEGAR_SENDER: Optional[str] = None
    SMS_TIMEOUT_SECONDS = 5
    OTP_DEBUG_SHOW = False
    OTP_LENGTH = 6
    OTP_TTL_SECONDS = 120
    OTP_MAX_ATTEMPTS = 5
    OTP_RESEND_COOLDOWN_SECONDS = 60
    OTP_MAX_PER_MOBILE_PER_HOUR = 5
    OTP_MAX_PER_IP_PER_HOUR = 20

    # password login (optional, assumption A6) - in-process limits per (ip, mobile) and per ip
    PASSWORD_LOGIN_ENABLED = True
    PASSWORD_MAX_FAILURES = 5
    PASSWORD_MAX_FAILURES_PER_IP = 30
    PASSWORD_FAILURE_WINDOW_SECONDS = 15 * 60

    # registration
    REGISTRATION_ROLES: tuple[str, ...] = ("staff", "hall_manager", "atelier_manager")
    REQUIRE_APPROVAL_ROLES: tuple[str, ...] = ()

    # modules
    DISABLED_MODULES: tuple[str, ...] = ()
    MODULES_STRICT = True  # a module that fails to import aborts startup

    LOG_LEVEL = "INFO"


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    AUTO_CREATE_DB = True
    OTP_DEBUG_SHOW = True
    TEMPLATES_AUTO_RELOAD = True  # `flask run` resets DEBUG from FLASK_DEBUG; edits to templates must still show
    SEND_FILE_MAX_AGE_DEFAULT = None
    LOG_LEVEL = "DEBUG"


class TestingConfig(BaseConfig):
    """Fast, hermetic: in-memory SQLite, CSRF off, console SMS, no rate-limit surprises."""

    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    SECRET_KEY = "test-secret-key-not-for-production-0123456789"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    AUTO_CREATE_DB = True
    WTF_CSRF_ENABLED = False
    MODULES_STRICT = False  # one broken module must not take every test file down
    SESSION_PROTECTION = "strong"
    LOG_LEVEL = "WARNING"
    SEND_FILE_MAX_AGE_DEFAULT = None


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    HSTS_SECONDS = 365 * 24 * 60 * 60
    SEND_FILE_MAX_AGE_DEFAULT = timedelta(days=1)


CONFIG_BY_NAME: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "dev": DevelopmentConfig,
    "testing": TestingConfig,
    "test": TestingConfig,
    "production": ProductionConfig,
    "prod": ProductionConfig,
}


def resolve_config_class(config: Any = None, environ: Optional[Mapping[str, str]] = None) -> type[BaseConfig]:
    """Pick the config class for *config* (None / name / class / instance)."""
    environ = os.environ if environ is None else environ
    if isinstance(config, type) and issubclass(config, BaseConfig):
        return config
    if isinstance(config, BaseConfig):
        return type(config)
    name = config if isinstance(config, str) else (
        environ.get("ATELIER_ENV") or environ.get("FLASK_ENV") or "development"
    )
    try:
        return CONFIG_BY_NAME[name.strip().lower()]
    except KeyError:
        raise ConfigError(
            f"unknown environment {name!r}; use one of: development, testing, production"
        ) from None


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _class_settings(cls: type[BaseConfig]) -> dict[str, Any]:
    """Upper-case attributes of *cls* (inherited ones included)."""
    settings: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for key, value in vars(klass).items():
            if key.isupper():
                settings[key] = value
    return settings


def _dev_secret_key(instance_path: Path) -> str:
    """A stable random key for development, kept in ``instance/secret_key``."""
    path = instance_path / "secret_key"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        instance_path.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass  # read-only checkout: a per-process key still works
    return key


def load_config(
    config: Any = None,
    overrides: Optional[Mapping[str, Any]] = None,
    *,
    instance_path: str | os.PathLike[str],
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Build the final settings dict for one application.

    Order (later wins): class defaults, environment variables, *overrides*.
    ``config`` may be None / a name / a :class:`BaseConfig` class or instance;
    a plain mapping given as ``config`` is treated as *overrides* on top of the
    environment-selected class.  Raises :class:`ConfigError` for unsafe
    production settings.
    """
    environ = os.environ if environ is None else environ
    merged: dict[str, Any] = dict(overrides or {})
    if isinstance(config, Mapping):
        merged = {**config, **merged}
        config = None
    cls = resolve_config_class(config, environ)
    settings = _class_settings(cls)
    instance = Path(instance_path)
    is_testing = cls.TESTING or bool(merged.get("TESTING"))

    def env(name: str) -> Optional[str]:
        # The test environment is hermetic: ambient env vars never leak into it.
        if is_testing and "ATELIER_TEST_USE_ENV" not in environ:
            return None
        value = environ.get(name)
        return value if value not in (None, "") else None

    # --- plain strings / ints ---------------------------------------------
    for key in (
        "SECRET_KEY",
        "UPLOAD_DIR",
        "SMS_PROVIDER",
        "KAVENEGAR_API_KEY",
        "KAVENEGAR_OTP_TEMPLATE",
        "KAVENEGAR_SENDER",
        "SITE_DOMAIN",
        "LOG_LEVEL",
        "ASSET_VERSION",
        "PLATFORM_NAME",
    ):
        value = env(key)
        if value is not None:
            settings[key] = value
    for key in (
        "MAX_CONTENT_LENGTH",
        "MAX_UPLOAD_BYTES",
        "TRUST_PROXY_HOPS",
        "OTP_TTL_SECONDS",
        "OTP_RESEND_COOLDOWN_SECONDS",
        "OTP_MAX_PER_MOBILE_PER_HOUR",
        "OTP_MAX_PER_IP_PER_HOUR",
    ):
        value = env(key)
        if value is not None:
            try:
                settings[key] = int(value)
            except ValueError:
                raise ConfigError(f"{key} must be an integer, got {value!r}") from None
    for key in ("OTP_DEBUG_SHOW", "SESSION_COOKIE_SECURE", "PASSWORD_LOGIN_ENABLED"):
        value = env(key)
        if value is not None:
            settings[key] = _truthy(value)
    if env("SESSION_COOKIE_SECURE") is not None:
        settings["REMEMBER_COOKIE_SECURE"] = settings["SESSION_COOKIE_SECURE"]
    for key in ("REGISTRATION_ROLES", "REQUIRE_APPROVAL_ROLES", "DISABLED_MODULES"):
        value = env(key)
        if value is not None:
            settings[key] = _csv(value)
    url = env("DATABASE_URL")
    if url is not None:
        settings["SQLALCHEMY_DATABASE_URI"] = _fix_database_url(url)
    if env("ATELIER_MODULES_STRICT") is not None:
        settings["MODULES_STRICT"] = _truthy(env("ATELIER_MODULES_STRICT"))

    settings.update(merged)
    settings["ENV_NAME"] = cls.ENV_NAME
    for key in ("REGISTRATION_ROLES", "REQUIRE_APPROVAL_ROLES", "DISABLED_MODULES"):
        settings[key] = _csv(settings.get(key))

    # --- instance-relative defaults ---------------------------------------
    if not settings.get("SQLALCHEMY_DATABASE_URI"):
        settings["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + (instance / "atelier.db").as_posix()
    settings["SQLALCHEMY_DATABASE_URI"] = _fix_database_url(str(settings["SQLALCHEMY_DATABASE_URI"]))
    if not settings.get("UPLOAD_DIR"):
        settings["UPLOAD_DIR"] = str(instance / "uploads")
    settings["UPLOAD_DIR"] = str(Path(str(settings["UPLOAD_DIR"])).expanduser().resolve())

    # --- secrets and safety checks ----------------------------------------
    secret = settings.get("SECRET_KEY")
    if cls.ENV_NAME == "production":
        if not secret or str(secret).strip().lower() in _PLACEHOLDER_SECRETS or len(str(secret)) < 32:
            raise ConfigError(
                "SECRET_KEY is required in production and must be at least 32 random characters. "
                'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        if settings.get("OTP_DEBUG_SHOW"):
            raise ConfigError("OTP_DEBUG_SHOW must be off in production (it would leak login codes).")
        # SMS_PROVIDER=console in production is not fatal (a fresh install must come up so the
        # admin can be created, and password login still works); create_app logs a warning.
    elif not secret:
        settings["SECRET_KEY"] = _dev_secret_key(instance)

    provider = str(settings.get("SMS_PROVIDER", "console")).strip().lower()
    if provider not in {"console", "kavenegar"}:
        raise ConfigError(f"SMS_PROVIDER must be 'console' or 'kavenegar', got {provider!r}")
    settings["SMS_PROVIDER"] = provider
    if provider == "kavenegar" and not settings.get("KAVENEGAR_API_KEY"):
        raise ConfigError("SMS_PROVIDER=kavenegar needs KAVENEGAR_API_KEY")

    if settings["SQLALCHEMY_DATABASE_URI"].startswith("sqlite") and ":memory:" not in settings[
        "SQLALCHEMY_DATABASE_URI"
    ] and settings["SQLALCHEMY_DATABASE_URI"] != "sqlite://":
        options = dict(settings.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        connect_args = dict(options.get("connect_args") or {})
        connect_args.setdefault("timeout", 15)  # wait for a writer instead of "database is locked"
        options["connect_args"] = connect_args
        settings["SQLALCHEMY_ENGINE_OPTIONS"] = options
    return settings
