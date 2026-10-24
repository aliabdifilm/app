"""Parsing and validation of the marketplace forms (plain HTML forms, built on ``services.forms.Form``).

Field names of the pay-terms editor (the same names the afish pages of the atelier panel use, so a
prefilled link from there maps one to one):

* ``base``                    money, Toman
* ``commission``              money, Toman per sold photo
* ``tier_threshold[]``        repeated, integer "N photos and above"
* ``tier_amount[]``           repeated, money

Only the *shape* is checked here; the arithmetic of pay is ``app.services.payroll``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Mapping, Optional, Sequence

from app.extensions import db
from app.models import Atelier, Event, JobPost
from app.services.constants import CLOSED_EVENT_STATUSES, Skill
from app.services.forms import Form, clean_text
from app.services.money import normalize_digits, parse_toman

MAX_TIERS = 8
MAX_TIER_THRESHOLD = 100_000
MAX_SLOTS = 50
MAX_DESCRIPTION = 2000
MAX_MESSAGE = 500
MAX_REVIEW_COMMENT = 1000


# --------------------------------------------------------------------------
# pay terms
# --------------------------------------------------------------------------


def _raw_list(form: Form, name: str) -> list[str]:
    getter = getattr(form.data, "getlist", None)
    if getter is None:
        value = form.data.get(name) or []
        return [value] if isinstance(value, str) else list(value)
    return list(getter(name))


def read_tiers(form: Form) -> Optional[list[dict[str, int]]]:
    """Bonus tiers from the repeated rows; ``None`` (and an error on ``tiers``) when a row is invalid.

    Rows where both fields are blank are ignored (the editor always shows a spare row).
    ``form.values['tiers']`` receives the raw rows so a failed submit shows what was typed.
    """
    thresholds = _raw_list(form, "tier_threshold[]")
    amounts = _raw_list(form, "tier_amount[]")
    rows: list[dict[str, str]] = []
    tiers: list[dict[str, int]] = []
    problem: Optional[str] = None
    seen: set[int] = set()
    for raw_threshold, raw_amount in zip_longest(thresholds, amounts, fillvalue=""):
        threshold_text = normalize_digits(clean_text(raw_threshold)).replace(",", "")
        amount_text = clean_text(raw_amount)
        if not threshold_text and not amount_text:
            continue
        rows.append({"threshold": threshold_text, "amount": amount_text})
        if not re.fullmatch(r"\d{1,6}", threshold_text) or not 1 <= int(threshold_text) <= MAX_TIER_THRESHOLD:
            problem = problem or "در هر پلهٔ پاداش، تعداد عکس را به‌صورت عدد صحیح (حداقل ۱) وارد کنید."
            continue
        try:
            amount = parse_toman(amount_text, allow_blank=False)
        except ValueError:
            problem = problem or "مبلغ هر پلهٔ پاداش را به تومان و به‌صورت عدد وارد کنید."
            continue
        threshold = int(threshold_text)
        if threshold in seen:
            problem = problem or "دو پلهٔ پاداش نباید تعداد عکس یکسان داشته باشند."
            continue
        seen.add(threshold)
        tiers.append({"threshold": threshold, "amount": amount})
    form.values["tiers"] = rows
    if len(rows) > MAX_TIERS:
        problem = problem or f"حداکثر {MAX_TIERS} پلهٔ پاداش مجاز است."
    if problem:
        form.error("tiers", problem)
        return None
    return sorted(tiers, key=lambda tier: tier["threshold"])


def read_terms(form: Form) -> Optional[dict[str, Any]]:
    """``{base, commission_per_unit, bonus_tiers}`` or ``None`` with the errors recorded."""
    base = form.money("base", "حقوق پایه", default=0)
    commission = form.money("commission", "پورسانت هر عکس", default=0)
    tiers = read_tiers(form)
    if tiers is None or "base" in form.errors or "commission" in form.errors:
        return None
    return {"base": base or 0, "commission_per_unit": commission or 0, "bonus_tiers": tiers}


def terms_to_form(terms: Optional[Mapping[str, Any]], *, blank_zero: bool = False) -> dict[str, Any]:
    """Form values (what the templates read) for a terms dict."""
    terms = terms or {}
    base = int(terms.get("base") or 0)
    commission = int(terms.get("commission_per_unit") or 0)
    return {
        "base": "" if blank_zero and not base else base,
        "commission": "" if blank_zero and not commission else commission,
        "tiers": [{"threshold": t["threshold"], "amount": t["amount"]} for t in (terms.get("bonus_tiers") or [])],
    }


# --------------------------------------------------------------------------
# the job post form
# --------------------------------------------------------------------------


@dataclass
class JobInput:
    """A validated post form."""

    atelier: Atelier
    title: str
    role: Skill
    date: dt.date
    city: str
    slots: int
    description: Optional[str]
    event: Optional[Event]
    terms: dict[str, Any] = field(default_factory=dict)


def read_job(
    form: Form,
    *,
    ateliers: Sequence[Atelier],
    today: dt.date,
    post: Optional[JobPost] = None,
    locked: bool = False,
    accepted: int = 0,
) -> Optional[JobInput]:
    """Validate the create / edit form of a post.

    * ``ateliers``  the manager's own ateliers: the chosen one must be among them (a foreign id in the
      body is just an unknown choice);
    * ``post``      the post being edited (``None`` when creating);
    * ``locked``    applications already exist: role, date, linked event and pay terms are kept as they
      are (what the applicants saw is what they get), whatever the form says;
    * ``accepted``  how many applications are accepted: ``slots`` cannot go below that.
    """
    by_id = {atelier.id: atelier for atelier in ateliers}
    atelier: Optional[Atelier] = None
    if post is not None:
        atelier = by_id.get(post.atelier_id)
    elif len(ateliers) == 1:
        atelier = ateliers[0]
    else:
        chosen = form.choice("atelier_id", "اتلیه", [str(a.id) for a in ateliers], required=True)
        atelier = by_id.get(int(chosen)) if chosen else None
    if atelier is None and "atelier_id" not in form.errors:
        form.error("atelier_id", "اتلیه نامعتبر است.")

    title = form.text("title", "عنوان آگهی", required=True, max_len=150)
    description = form.text("description", "توضیحات", max_len=MAX_DESCRIPTION, multiline=True)
    slots = form.integer("slots", "تعداد نیروی مورد نیاز", required=True, min=1, max=MAX_SLOTS)
    if slots is not None and slots < accepted:
        form.error("slots", f"تعداد نیرو نمی‌تواند از تعداد پذیرفته‌شده‌ها ({accepted}) کمتر باشد.")

    if post is not None and locked:
        role = post.role
        day: Optional[dt.date] = post.date
        event = post.event
        terms: Optional[dict[str, Any]] = post.terms_dict()
        form.values.update({"role": role.value, "date": post.date.isoformat(), "event": post.event_id or ""})
        form.values.update(terms_to_form(terms))
    else:
        role = form.enum("role", "نقش", Skill, required=True)
        day = form.date("date", "تاریخ همکاری", required=True)
        if day is not None and day < today and (post is None or day != post.date):
            form.error("date", "تاریخ همکاری نباید گذشته باشد.")
        event = None
        raw_event = normalize_digits(form.raw("event"))
        form.values["event"] = raw_event
        if raw_event:
            event = db.session.get(Event, int(raw_event)) if re.fullmatch(r"\d{1,12}", raw_event) else None
            unchanged = post is not None and event is not None and event.id == post.event_id
            valid = (
                event is not None
                and atelier is not None
                and event.atelier_id == atelier.id
                and (unchanged or event.status not in CLOSED_EVENT_STATUSES)
            )
            if not valid:
                form.error("event", "مراسم انتخاب‌شده معتبر نیست یا متعلق به این اتلیه نیست.")
                event = None
            elif day is not None and day != event.date:
                form.error("date", "تاریخ آگهی باید با تاریخ مراسمِ انتخاب‌شده یکی باشد.")
        terms = read_terms(form)

    city = form.text("city", "شهر", max_len=80)
    if not city and "city" not in form.errors:
        # left blank: the hall's city, else the atelier's
        fallback = (event.hall.city if event is not None and event.hall is not None else "") or (
            atelier.city if atelier is not None else ""
        )
        if fallback:
            city = fallback
            form.values["city"] = city
        else:
            form.error("city", "شهر را وارد کنید.")

    if not form.ok or atelier is None or title is None or role is None or day is None or slots is None or terms is None:
        return None
    return JobInput(
        atelier=atelier,
        title=title,
        role=Skill.coerce(role),
        date=day,
        city=city,
        slots=slots,
        description=description,
        event=event,
        terms=terms,
    )


# --------------------------------------------------------------------------
# small forms
# --------------------------------------------------------------------------


def read_message(form: Form, name: str = "message", label: str = "پیام", *, max_len: int = MAX_MESSAGE) -> Optional[str]:
    return form.text(name, label, max_len=max_len, multiline=True)


@dataclass
class ReviewInput:
    rating: int
    comment: Optional[str]


def read_review(form: Form) -> Optional[ReviewInput]:
    rating = form.integer("rating", "امتیاز", required=True, min=1, max=5)
    if "rating" in form.errors and not form.raw("rating"):
        form.errors["rating"] = "امتیاز را (از ۱ تا ۵ ستاره) انتخاب کنید."
    comment = form.text("comment", "نظر", max_len=MAX_REVIEW_COMMENT, multiline=True)
    if not form.ok or rating is None:
        return None
    return ReviewInput(rating=rating, comment=comment)


# --------------------------------------------------------------------------
# GET filters
# --------------------------------------------------------------------------


@dataclass
class BoardFilters:
    city: str = ""
    role: Optional[Skill] = None
    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    min_pay: Optional[int] = None
    sort: str = "date"
    errors: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.city or self.role or self.date_from or self.date_to or self.min_pay)


BOARD_SORTS = ("date", "pay", "new")


def read_board_filters(args: Mapping[str, Any]) -> BoardFilters:
    """Filters of ``/jobs`` from the query string; an invalid value is reported and ignored."""
    form = Form(args)
    city = form.text("city", "شهر", max_len=80) or ""
    role = form.enum("role", "نقش", Skill)
    date_from = form.date("date_from", "از تاریخ")
    date_to = form.date("date_to", "تا تاریخ")
    min_pay = form.money("min_pay", "حداقل مبلغ")
    if date_from and date_to and date_to < date_from:
        form.error("date_to", "«تا تاریخ» نباید قبل از «از تاریخ» باشد.")
        date_to = None
    sort = form.raw("sort")
    if sort not in BOARD_SORTS:
        sort = "date"
    errors = dict(form.errors)
    return BoardFilters(
        city=city if "city" not in errors else "",
        role=role if "role" not in errors else None,
        date_from=date_from if "date_from" not in errors else None,
        date_to=date_to if "date_to" not in errors else None,
        min_pay=min_pay if "min_pay" not in errors else None,
        sort=sort,
        errors=errors,
        values=dict(form.values),
    )


@dataclass
class TalentFilters:
    skill: Optional[Skill] = None
    city: str = ""
    min_rating: Optional[int] = None
    on_date: Optional[dt.date] = None
    name: str = ""
    sort: str = "rating"
    errors: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.skill or self.city or self.min_rating or self.on_date or self.name)


def read_talent_filters(args: Mapping[str, Any]) -> TalentFilters:
    form = Form(args)
    skill = form.enum("skill", "مهارت", Skill)
    city = form.text("city", "شهر", max_len=80) or ""
    min_rating = form.integer("min_rating", "حداقل امتیاز", min=1, max=5)
    on_date = form.date("on_date", "تاریخ")
    name = form.text("name", "نام", max_len=80) or ""
    sort = form.raw("sort")
    if sort not in ("rating", "experience", "new"):
        sort = "rating"
    errors = dict(form.errors)
    return TalentFilters(
        skill=skill if "skill" not in errors else None,
        city=city if "city" not in errors else "",
        min_rating=min_rating if "min_rating" not in errors else None,
        on_date=on_date if "on_date" not in errors else None,
        name=name if "name" not in errors else "",
        sort=sort,
        errors=errors,
        values=dict(form.values),
    )
