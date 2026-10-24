"""Shared model plumbing: column helpers, validators, base classes.

Conventions implemented here (see docs/DATA_MODEL.md):

* Timestamps are **naive UTC** in Python and in the database.  Use
  :func:`utcnow`.  Aware datetimes are accepted on write and converted.
* Money is an integer number of Toman, stored as ``BIGINT`` (never float);
  :func:`check_money` normalises Persian/Arabic digits and rejects floats and
  negatives at assignment time, ``CHECK (col >= 0)`` backs it up in the DB.
* Enumerated columns are ``VARCHAR`` + ``CHECK`` (portable, no native enums),
  built by :func:`choice` from the enums in ``app.services.constants``.
* Every foreign key is indexed; use :func:`fk`.
* SQLite enforces foreign keys (PRAGMA) for every engine in this process.
"""

import datetime as dt
import re
import sqlite3
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.extensions import db

# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def utcnow() -> dt.datetime:
    """Current time as a *naive* UTC datetime (the storage convention)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Convert an aware datetime to naive UTC; naive values are assumed UTC."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


class UTCDateTime(TypeDecorator):
    """``DateTime`` that normalises to naive UTC on write.

    An aware datetime (e.g. Asia/Tehran) is converted to UTC instead of having
    its offset silently dropped; reads return naive UTC.
    """

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        return to_naive_utc(value)


# --------------------------------------------------------------------------
# Digits, mobile numbers, integers
# --------------------------------------------------------------------------

# Persian (U+06F0..) and Arabic-Indic (U+0660..) digits -> latin; Arabic
# thousands/decimal separators -> ASCII.  Kept local on purpose: the models
# layer must not depend on utility modules that live above it.
_TO_LATIN = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬،٫",
    "01234567890123456789,,.",
)
_INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_MOBILE_RE = re.compile(r"^09\d{9}$")


def to_latin_digits(value: Any) -> str:
    """``'۰۹۱۲'`` -> ``'0912'``; also strips invisible bidi/ZWNJ marks."""
    if value is None:
        return ""
    return _INVISIBLE.sub("", str(value)).translate(_TO_LATIN)


def normalize_mobile(value: Any) -> str:
    """Canonical Iranian mobile number: ``09xxxxxxxxx`` (11 latin digits).

    Accepts Persian/Arabic digits, spaces/dashes and the ``+98`` / ``0098`` /
    ``98`` / missing-leading-zero forms.  Raises ``ValueError`` otherwise.
    """
    text = to_latin_digits(value)
    text = re.sub(r"[\s\-()]", "", text)
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("0098"):
        text = "0" + text[4:]
    elif text.startswith("98") and len(text) == 12:
        text = "0" + text[2:]
    elif len(text) == 10 and text.startswith("9"):
        text = "0" + text
    if not _MOBILE_RE.match(text):
        raise ValueError(f"invalid mobile number: {value!r}")
    return text


def clean_phone(value: Any) -> str | None:
    """Landline/mobile text with latin digits and no invisible marks (or None)."""
    if value is None:
        return None
    text = to_latin_digits(value).strip()
    return text or None


def coerce_int(value: Any, name: str = "value") -> int:
    """Strict integer coercion for money/counters.

    Accepts ``int`` and digit strings (Persian/Arabic/latin digits, ``,``
    ``٬`` ``،`` separators, surrounding spaces).  Rejects ``bool``, ``float``
    and anything else with ``TypeError``/``ValueError`` - money is never a
    float in this project.
    """
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, not bool")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        text = to_latin_digits(value).replace(",", "").replace(" ", "").strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        raise ValueError(f"{name} must be an integer, got {value!r}")
    raise TypeError(f"{name} must be an integer, got {type(value).__name__}")


def check_money(value: Any, name: str = "amount") -> int | None:
    """Validate a Toman amount: integer >= 0 (``None`` passes through)."""
    if value is None:
        return None
    amount = coerce_int(value, name)
    if amount < 0:
        raise ValueError(f"{name} must be >= 0, got {amount}")
    return amount


def check_int(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Validate a plain integer with optional bounds (``None`` passes through)."""
    if value is None:
        return None
    number = coerce_int(value, name)
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {number}")
    return number


# --------------------------------------------------------------------------
# Column factories
# --------------------------------------------------------------------------


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def choice(enum_cls: type[Enum], column: str) -> sa.Enum:
    """Portable enum column type: ``VARCHAR`` + named ``CHECK (col IN (...))``.

    Loads as the enum member (a ``str``), accepts members or plain strings on
    write, and rejects unknown strings at flush time on every database.
    """
    return sa.Enum(
        enum_cls,
        name=f"{column}_valid",
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
        length=max(len(member.value) for member in enum_cls),
    )


def fk(
    target: str,
    *,
    ondelete: str | None = None,
    nullable: bool = False,
    index: bool = True,
    unique: bool = False,
    **kwargs: Any,
):
    """Integer foreign-key column, indexed by default."""
    return mapped_column(
        sa.Integer,
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
        unique=unique,
        **kwargs,
    )


def money(*, nullable: bool = False, default: int | None = 0, **kwargs: Any):
    """Money column: integer Toman as ``BIGINT`` (default 0, non-null)."""
    return mapped_column(sa.BigInteger, nullable=nullable, default=default, **kwargs)


def money_checks(*columns: str) -> tuple[sa.CheckConstraint, ...]:
    """``CHECK (col >= 0)`` for each column (named ``<col>_nonneg``)."""
    return tuple(sa.CheckConstraint(f"{c} >= 0", name=f"{c}_nonneg") for c in columns)


def json_col(default=dict, *, nullable: bool = False, **kwargs: Any):
    """Portable JSON column (SQL NULL for ``None``, not the JSON ``null``)."""
    return mapped_column(
        sa.JSON(none_as_null=True),
        nullable=nullable,
        default=default,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Base classes
# --------------------------------------------------------------------------


def _apply_python_defaults(obj: Any, given: dict[str, Any]) -> None:
    """Fill column ``default=`` values at construction time.

    SQLAlchemy only applies Python-side defaults on flush, so a fresh
    ``Event()`` would have ``status is None`` until then.  Applying them in
    ``__init__`` makes unflushed objects behave like persisted ones.
    """
    mapper = sa.inspect(type(obj))
    for attr in mapper.column_attrs:
        key = attr.key
        if key in given:
            continue
        column = attr.columns[0]
        default = column.default
        if default is None or getattr(obj, key, None) is not None:
            continue
        if default.is_scalar:
            setattr(obj, key, default.arg)
        elif default.is_callable:
            setattr(obj, key, default.arg(None))


class BaseModel(db.Model):
    """Abstract base of all models: eager defaults + a readable ``repr``."""

    __abstract__ = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        _apply_python_defaults(self, kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        ident = getattr(self, "id", None)
        return f"<{type(self).__name__} id={ident}>"


def _make_enum_coercer(enum_cls: type[Enum]):
    def coerce(target: Any, value: Any, oldvalue: Any, initiator: Any) -> Any:
        if value is None or isinstance(value, enum_cls):
            return value
        try:
            return enum_cls(value)
        except ValueError:
            allowed = ", ".join(member.value for member in enum_cls)
            raise ValueError(
                f"{value!r} is not a valid {enum_cls.__name__} (expected one of: {allowed})"
            ) from None

    return coerce


@event.listens_for(BaseModel, "mapper_configured", propagate=True)
def _coerce_enum_attributes(mapper: Any, cls: type) -> None:
    """Make every enum column hold enum *members* in memory, not raw strings.

    ``event.status = "confirmed"`` becomes ``EventStatus.CONFIRMED`` at once
    (so ``.value`` / ``.label`` always work, also before the first flush), and
    an unknown value fails immediately with ``ValueError`` instead of at flush.
    """
    for attr in mapper.column_attrs:
        column_type = attr.columns[0].type
        enum_cls = getattr(column_type, "enum_class", None) if isinstance(column_type, sa.Enum) else None
        if enum_cls is not None:
            event.listen(
                getattr(cls, attr.key), "set", _make_enum_coercer(enum_cls), retval=True
            )


class TimestampMixin:
    """``created_at`` / ``updated_at`` (naive UTC)."""

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# SQLite: enforce foreign keys everywhere
# --------------------------------------------------------------------------


@event.listens_for(Engine, "connect")
def _sqlite_enable_foreign_keys(dbapi_connection, connection_record) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
