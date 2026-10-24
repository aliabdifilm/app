"""Tiny form reader for plain-HTML forms (no WTForms classes).

Every module parses ``request.form`` the same way, so Persian / Arabic / Latin
digits, Toman amounts, Jalali and ISO dates and Persian error messages behave
identically everywhere::

    form = Form(request.form)
    name = form.text("name", "نام تالار", required=True, max_len=150)
    rent = form.money("rent", "اجارهٔ پیش‌فرض")
    day = form.date("date", "تاریخ مراسم", required=True)
    kind = form.enum("ceremony_type", "نوع مراسم", CeremonyType, default=CeremonyType.WEDDING)
    if form.ok:
        ...save...
        return redirect(...)
    return render_template("x.html", form=form.values, errors=form.errors), 422

* ``form.values``  raw strings (digits normalised) to redisplay after an error
* ``form.errors``  ``{field: Persian message}`` for ``form_field(error=errors.get(...))``
* every getter returns a clean Python value (``None`` for an empty optional
  field) - after an error it returns ``None`` / the default and records the
  message, so you can keep reading the other fields and show all errors at once.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Iterable, Mapping, Optional

from app.models.base import normalize_mobile
from app.services import jalali
from app.services.money import MAX_TOMAN, format_toman, normalize_digits, parse_toman, to_persian_digits

# Invisible characters to drop: zero-width space, bidi marks / embeddings / isolates, BOM.  The
# zero-width NON-joiner U+200C (نیم‌فاصله) is part of correct Persian spelling and is kept.
_INVISIBLE = re.compile("[\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_TRUE = {"1", "true", "on", "yes", "y", "t"}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_MISSING: Any = object()


def clean_text(value: Any) -> str:
    """Strip, drop invisible bidi marks (keeping ZWNJ) and normalise Arabic yeh/kaf to Persian letters."""
    if value is None:
        return ""
    text = _INVISIBLE.sub("", str(value)).strip()
    return text.replace("ي", "ی").replace("ك", "ک")


class Form:
    """Reads and validates one submission; collect errors, then check :attr:`ok`."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        if data is None:
            from flask import request

            data = request.form
        self.data = data
        self.values: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    # -- bookkeeping ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, name: str, message: str) -> None:
        """Record *message* for *name* (the first message for a field wins)."""
        self.errors.setdefault(name, message)

    def raw(self, name: str) -> str:
        """The submitted string, trimmed (``''`` when missing)."""
        return clean_text(self.data.get(name))

    def _take(self, name: str) -> str:
        text = self.raw(name)
        self.values[name] = text
        return text

    def _required(self, name: str, label: Optional[str]) -> None:
        self.error(name, f"{label or name} را وارد کنید.")

    # -- text ------------------------------------------------------------------

    def text(
        self,
        name: str,
        label: Optional[str] = None,
        *,
        required: bool = False,
        min_len: int = 0,
        max_len: Optional[int] = None,
        default: Optional[str] = None,
        digits: bool = False,
        multiline: bool = False,
    ) -> Optional[str]:
        """Trimmed text.  ``digits=True`` converts Persian/Arabic digits to Latin.

        Returns *default* (``None`` unless given) for an empty optional field.
        """
        text = self._take(name)
        if digits:
            text = normalize_digits(text)
            self.values[name] = text
        if not multiline and ("\n" in text or "\r" in text):
            text = " ".join(text.split())
            self.values[name] = text
        if not text:
            if required:
                self._required(name, label)
            return default
        if len(text) < min_len:
            self.error(name, f"{label or name} باید دست‌کم {to_persian_digits(min_len)} نویسه باشد.")
            return default
        if max_len is not None and len(text) > max_len:
            self.error(name, f"{label or name} نباید بیش از {to_persian_digits(max_len)} نویسه باشد.")
            return default
        return text

    # -- numbers ---------------------------------------------------------------

    def integer(
        self,
        name: str,
        label: Optional[str] = None,
        *,
        required: bool = False,
        min: Optional[int] = None,
        max: Optional[int] = None,
        default: Optional[int] = None,
    ) -> Optional[int]:
        text = normalize_digits(self._take(name)).replace(",", "").replace("٬", "")
        self.values[name] = text
        if not text:
            if required:
                self._required(name, label)
            return default
        if not re.fullmatch(r"[+-]?\d+", text):
            self.error(name, f"{label or name} باید یک عدد صحیح باشد.")
            return default
        number = int(text)
        if min is not None and number < min:
            self.error(name, f"{label or name} نباید کمتر از {to_persian_digits(min)} باشد.")
            return default
        if max is not None and number > max:
            self.error(name, f"{label or name} نباید بیشتر از {to_persian_digits(max)} باشد.")
            return default
        return number

    def money(
        self,
        name: str,
        label: Optional[str] = None,
        *,
        required: bool = False,
        min: int = 0,
        max: int = MAX_TOMAN,
        default: Optional[int] = None,
    ) -> Optional[int]:
        """Integer Toman (accepts ``۱٬۸۷۰٬۰۰۰``, ``1,870,000 تومان``, ``1870000``; never a float)."""
        text = self._take(name)
        if not text:
            if required:
                self._required(name, label)
            return default
        try:
            amount = parse_toman(text, allow_blank=False, allow_negative=min < 0)
        except ValueError:
            self.error(name, f"{label or name} نامعتبر است؛ فقط عدد (به تومان) وارد کنید.")
            return default
        if amount < min or amount > max:
            low, high = format_toman(min, persian_digits=True), format_toman(max, persian_digits=True)
            self.error(name, f"{label or name} باید بین {low} و {high} تومان باشد.")
            return default
        self.values[name] = amount
        return amount

    # -- dates and times ------------------------------------------------------

    def date(
        self,
        name: str,
        label: Optional[str] = None,
        *,
        required: bool = False,
        min: Optional[dt.date] = None,
        max: Optional[dt.date] = None,
        default: Optional[dt.date] = None,
    ) -> Optional[dt.date]:
        """A Gregorian date from ISO ``2025-07-17`` (what ``jdate_input`` posts) or Jalali ``۱۴۰۴/۰۴/۲۶``."""
        text = normalize_digits(self._take(name))
        self.values[name] = text
        if not text:
            if required:
                self._required(name, label)
            return default
        value: Optional[dt.date] = None
        try:
            if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
                y, m, d = (int(part) for part in text.split("-"))
                value = dt.date(y, m, d) if y > 1700 else dt.date(*jalali.jalali_to_gregorian(y, m, d))
            else:
                value = jalali.parse_jalali(text)
        except (ValueError, TypeError, OverflowError):
            value = None
        if value is None:
            self.error(name, f"{label or name} نامعتبر است.")
            return default
        if min is not None and value < min:
            self.error(name, f"{label or name} نباید قبل از {jalali.to_jalali_str(min, persian_digits=True)} باشد.")
            return default
        if max is not None and value > max:
            self.error(name, f"{label or name} نباید بعد از {jalali.to_jalali_str(max, persian_digits=True)} باشد.")
            return default
        self.values[name] = value.isoformat()
        return value

    def time(
        self, name: str, label: Optional[str] = None, *, required: bool = False
    ) -> Optional[dt.time]:
        """``HH:MM`` (24h, any digits)."""
        text = normalize_digits(self._take(name))
        if not text:
            if required:
                self._required(name, label)
            return None
        match = _TIME_RE.match(text)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            self.error(name, f"{label or name} نامعتبر است (مثلاً ۱۸:۳۰).")
            return None
        return dt.time(int(match.group(1)), int(match.group(2)))

    # -- choices ---------------------------------------------------------------

    def choice(
        self,
        name: str,
        label: Optional[str],
        options: Iterable[Any],
        *,
        required: bool = False,
        default: Any = None,
    ) -> Any:
        """One of *options* (compared as strings); returns the original option object."""
        text = self._take(name)
        if not text:
            if required:
                self._required(name, label)
            return default
        for option in options:
            if str(option) == text:
                return option
        self.error(name, f"{label or name} نامعتبر است.")
        return default

    def enum(
        self,
        name: str,
        label: Optional[str],
        enum_cls: type,
        *,
        required: bool = False,
        default: Any = None,
    ) -> Any:
        """A member of a ``constants`` enum (by its string value)."""
        return self.choice(name, label, list(enum_cls), required=required, default=default)

    def boolean(self, name: str) -> bool:
        """Checkbox semantics: present and truthy (``1``/``on``/``true``)."""
        value = self.raw(name).lower() in _TRUE
        self.values[name] = value
        return value

    def mobile(
        self, name: str = "mobile", label: str = "شمارهٔ موبایل", *, required: bool = False
    ) -> Optional[str]:
        """Normalised ``09xxxxxxxxx``."""
        text = self._take(name)
        if not text:
            if required:
                self._required(name, label)
            return None
        try:
            return normalize_mobile(text)
        except ValueError:
            self.error(name, f"{label} نامعتبر است؛ مثلاً ۰۹۱۲۳۴۵۶۷۸۹")
            return None

    def getlist(self, name: str, convert: Callable[[str], Any] = str) -> list[Any]:
        """All non-empty values of a repeated field (``name[]`` rows, multi-selects)."""
        getter = getattr(self.data, "getlist", None)
        raw = getter(name) if getter else (self.data.get(name) or [])
        if isinstance(raw, str):
            raw = [raw]
        items: list[Any] = []
        for item in raw:
            text = clean_text(item)
            if text:
                items.append(convert(text))
        return items

    def int_list(self, name: str) -> list[int]:
        """Repeated integer ids (junk entries are skipped)."""
        out: list[int] = []
        for text in self.getlist(name):
            digits = normalize_digits(text)
            if re.fullmatch(r"\d+", digits):
                out.append(int(digits))
        return out

    # -- convenience -----------------------------------------------------------

    def context(self) -> dict[str, Any]:
        """``{"form": values, "errors": errors}`` for ``render_template(**form.context())``."""
        return {"form": self.values, "errors": self.errors}
