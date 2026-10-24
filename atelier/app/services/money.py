"""Money and digit helpers (pure Python, no web dependencies).

Rules (see AGENTS.md):

* Money is an **integer number of Toman** everywhere. Floats never enter the
  system: :func:`parse_toman` rejects them and :func:`format_toman` refuses to
  format them.
* User input may contain Persian (``۰-۹``), Arabic-Indic (``٠-٩``) or Latin
  digits and Persian/Arabic/Latin thousands separators; everything is
  normalised to Latin digits before parsing.
"""
from __future__ import annotations

import re
from decimal import Decimal

__all__ = [
    "MAX_TOMAN",
    "TOMAN_WORD",
    "normalize_digits",
    "to_persian_digits",
    "parse_toman",
    "format_toman",
]

#: Sanity ceiling for a single money amount (10^15 Toman).  It keeps every
#: value far inside a signed 64-bit BIGINT (SQLite and PostgreSQL) even after
#: summing thousands of rows, and rejects obviously fat-fingered input.
MAX_TOMAN = 10**15

#: The currency word appended by ``format_toman(..., suffix=True)``.
TOMAN_WORD = "تومان"

_PERSIAN_ZERO = 0x06F0  # ۰
_ARABIC_INDIC_ZERO = 0x0660  # ٠
_FULLWIDTH_ZERO = 0xFF10  # ０

# Any digit system -> Latin digits.  Separators are mapped to their ASCII
# counterparts so that "1٬870٬000", "1،870،000" and "1,870,000" all agree.
_TO_LATIN: dict[int, str] = {}
for _i in range(10):
    _TO_LATIN[_PERSIAN_ZERO + _i] = str(_i)
    _TO_LATIN[_ARABIC_INDIC_ZERO + _i] = str(_i)
    _TO_LATIN[_FULLWIDTH_ZERO + _i] = str(_i)
_TO_LATIN[0x066C] = ","  # ٬ ARABIC THOUSANDS SEPARATOR
_TO_LATIN[0x060C] = ","  # ، ARABIC COMMA
_TO_LATIN[0x066B] = "."  # ٫ ARABIC DECIMAL SEPARATOR

# Latin / Arabic-Indic digits -> Persian digits.
_TO_PERSIAN: dict[int, str] = {}
for _i in range(10):
    _TO_PERSIAN[ord(str(_i))] = chr(_PERSIAN_ZERO + _i)
    _TO_PERSIAN[_ARABIC_INDIC_ZERO + _i] = chr(_PERSIAN_ZERO + _i)
    _TO_PERSIAN[_FULLWIDTH_ZERO + _i] = chr(_PERSIAN_ZERO + _i)

# Zero-width / bidi control characters people paste along with numbers.
_INVISIBLE_RE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_MINUS_CHARS = "-−‐‑‒–"  # hyphen-minus, minus sign, dashes
_UNIT_SUFFIX_RE = re.compile(r"(?:تومان|تومن|tomans?)$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_PLAIN_RE = re.compile(r"^\d+$")
_GROUPED_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_MAX_DIGITS = len(str(MAX_TOMAN))


def normalize_digits(s) -> str:
    """Return *s* with every Persian / Arabic-Indic / full-width digit as a Latin digit.

    The Arabic thousands separator ``٬`` and Arabic comma ``،`` become ``,``;
    the Arabic decimal separator ``٫`` becomes ``.``.  Nothing else changes.
    ``None`` becomes the empty string and non-strings are passed through
    ``str()``.
    """
    if s is None:
        return ""
    return str(s).translate(_TO_LATIN)


def to_persian_digits(s) -> str:
    """Return ``str(s)`` with every Latin / Arabic-Indic digit as a Persian digit (۰-۹).

    Only digits are converted; separators and letters are left alone.
    ``None`` becomes the empty string.
    """
    if s is None:
        return ""
    return str(s).translate(_TO_PERSIAN)


def _is_blank(text: str) -> bool:
    return text == ""


def parse_toman(value, allow_blank: bool = True, allow_negative: bool = False) -> int:
    """Parse a Toman amount into an ``int``; raise ``ValueError`` on anything else.

    Accepted:

    * ``int`` (not ``bool``) and integral :class:`~decimal.Decimal` (PostgreSQL
      returns ``Decimal`` for ``SUM`` of integers);
    * digit strings in any digit system, optionally grouped with ``,`` ``٬``
      ``،`` or spaces in proper groups of three (``"1,870,000"``,
      ``"۱٬۸۷۰٬۰۰۰"``, ``"1 870 000"``), optionally followed by ``تومان`` /
      ``toman`` so that :func:`format_toman` output round-trips;
    * blank values (``None``, ``""``, whitespace) which give ``0`` when
      *allow_blank* is true.

    Rejected with ``ValueError``: floats (even ``5.0``), non-integral decimals,
    decimal strings (``"1.5"``), exponents, malformed grouping (``"1,87,000"``),
    letters, ``bool``, negatives unless *allow_negative*, and magnitudes above
    :data:`MAX_TOMAN`.
    """
    if value is None:
        return _blank(allow_blank)
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid Toman amount")
    if isinstance(value, float):
        raise ValueError("Floats are not allowed for money; use an integer number of Toman")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("Toman amounts must be whole numbers")
        return _check_range(int(value), allow_negative)
    if isinstance(value, int):
        return _check_range(value, allow_negative)
    if not isinstance(value, str):
        raise ValueError(f"Cannot parse {type(value).__name__} as Toman")

    text = _INVISIBLE_RE.sub("", normalize_digits(value)).strip()
    text = _UNIT_SUFFIX_RE.sub("", text).strip()
    if _is_blank(text):
        return _blank(allow_blank)

    negative = False
    if text[0] in _MINUS_CHARS:
        negative = True
        text = text[1:].strip()
    if text == "":
        raise ValueError("Invalid Toman amount: sign without digits")
    if negative and not allow_negative:
        raise ValueError("Negative Toman amounts are not allowed here")

    # Spaces are accepted as thousands separators, but only in groups of three.
    text = _WHITESPACE_RE.sub(",", text)
    if _PLAIN_RE.match(text):
        digits = text
    elif _GROUPED_RE.match(text):
        digits = text.replace(",", "")
    else:
        raise ValueError(f"Invalid Toman amount: {value!r}")

    digits = digits.lstrip("0") or "0"
    if len(digits) > _MAX_DIGITS:
        raise ValueError(f"Toman amount is too large (maximum {MAX_TOMAN})")
    number = int(digits)
    return _check_range(-number if negative else number, allow_negative)


def _blank(allow_blank: bool) -> int:
    if allow_blank:
        return 0
    raise ValueError("A Toman amount is required")


def _check_range(number: int, allow_negative: bool) -> int:
    if number < 0 and not allow_negative:
        raise ValueError("Negative Toman amounts are not allowed here")
    if abs(number) > MAX_TOMAN:
        raise ValueError(f"Toman amount is too large (maximum {MAX_TOMAN})")
    return number


def format_toman(n, persian_digits: bool = False, suffix: bool = False) -> str:
    """Format an integer Toman amount with thousands separators.

    ``format_toman(1870000)`` -> ``'1,870,000'``;
    ``format_toman(1870000, persian_digits=True, suffix=True)`` ->
    ``'۱٬۸۷۰٬۰۰۰ تومان'`` (Persian digits use the Arabic thousands separator
    ``٬``).  ``None`` is shown as ``0``.  Negative amounts get a leading ``-``.
    Floats and booleans raise ``ValueError``; numeric strings are parsed with
    :func:`parse_toman`.
    """
    if isinstance(n, int) and not isinstance(n, bool):
        number = n
    else:  # None, numeric strings and integral Decimals; rejects bool/float
        number = parse_toman(n, allow_blank=True, allow_negative=True)
    text = f"{abs(number):,}"
    if persian_digits:
        text = to_persian_digits(text).replace(",", "٬")
    if number < 0:
        text = "-" + text
    if suffix:
        text = f"{text} {TOMAN_WORD}"
    return text
