"""Whole numbers as Persian words ("مبلغ به حروف"), for printed invoices and pay slips.

``to_words(1870000)`` -> ``'یک میلیون و هشتصد و هفتاد هزار'``.  Pure Python, integers only.
"""

from __future__ import annotations

import numbers

__all__ = ["to_words", "toman_in_words"]

_ONES = ("", "یک", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه")
_TEENS = ("ده", "یازده", "دوازده", "سیزده", "چهارده", "پانزده", "شانزده", "هفده", "هجده", "نوزده")
_TENS = ("", "", "بیست", "سی", "چهل", "پنجاه", "شصت", "هفتاد", "هشتاد", "نود")
_HUNDREDS = ("", "صد", "دویست", "سیصد", "چهارصد", "پانصد", "ششصد", "هفتصد", "هشتصد", "نهصد")
_SCALES = ("", "هزار", "میلیون", "میلیارد", "تریلیون", "کوادریلیون")
_AND = " و "


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_HUNDREDS[n // 100])
    rest = n % 100
    if rest >= 20:
        parts.append(_TENS[rest // 10])
        if rest % 10:
            parts.append(_ONES[rest % 10])
    elif rest >= 10:
        parts.append(_TEENS[rest - 10])
    elif rest:
        parts.append(_ONES[rest])
    return _AND.join(parts)


def to_words(value: int) -> str:
    """The Persian words of an integer (``0`` -> ``صفر``, negatives get ``منفی``)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError(f"expected an integer, got {value!r}")
    number = int(value)
    if number == 0:
        return "صفر"
    sign = "منفی " if number < 0 else ""
    number = abs(number)
    groups = []
    while number:
        groups.append(number % 1000)
        number //= 1000
    if len(groups) > len(_SCALES):
        raise ValueError("number is too large")
    words = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            continue
        text = _below_thousand(group)
        if index == 1 and group == 1:  # "هزار" (not "یک هزار") is the customary spelling of 1,000
            text = ""
        words.append(f"{text} {_SCALES[index]}".strip())
    return sign + _AND.join(words)


def toman_in_words(value: int) -> str:
    """``'یک میلیون و ... تومان'`` - the form printed under an amount."""
    return f"{to_words(value)} تومان"
