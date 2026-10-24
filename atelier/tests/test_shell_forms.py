"""app.services.forms.Form and app.services.http helpers."""

from __future__ import annotations

import datetime as dt

import pytest
from werkzeug.datastructures import MultiDict

from app.services.constants import CeremonyType, Skill
from app.services.forms import Form, clean_text
from app.services.http import json_error, json_ok, page_args, wants_json


def form_of(**fields):
    return Form(MultiDict(fields))


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------


def test_text_basic():
    form = form_of(name="  تالار گل‌های سپید ‏ ")
    assert form.text("name", "نام", required=True, max_len=50) == "تالار گل‌های سپید"
    assert form.ok and form.values["name"] == "تالار گل‌های سپید"


def test_text_normalises_arabic_letters():
    form = form_of(name="علي كريمي")  # Arabic yeh / kaf
    assert form.text("name") == "علی کریمی"


def test_text_required_and_limits():
    form = form_of(a="", b="x", c="y" * 11)
    assert form.text("a", "نام", required=True) is None
    assert form.text("b", "عنوان", min_len=3) is None
    assert form.text("c", "شهر", max_len=10) is None
    assert form.errors == {
        "a": "نام را وارد کنید.",
        "b": "عنوان باید دست‌کم ۳ نویسه باشد.",
        "c": "شهر نباید بیش از ۱۰ نویسه باشد.",
    }
    assert not form.ok


def test_text_optional_default():
    form = form_of(a="")
    assert form.text("a", default="—") == "—" and form.ok
    assert form_of().text("missing") is None


def test_text_single_line_collapses_newlines_multiline_keeps_them():
    form = form_of(a="یک\nدو\r\nسه", b="یک\nدو")
    assert form.text("a") == "یک دو سه"
    assert form.text("b", multiline=True) == "یک\nدو"


def test_text_digits():
    assert form_of(a="۰۹۱۲۳").text("a", digits=True) == "09123"


def test_first_error_wins():
    form = form_of(a="")
    form.text("a", "الف", required=True)
    form.error("a", "پیام دیگر")
    assert form.errors["a"] == "الف را وارد کنید."


def test_clean_text_handles_none():
    assert clean_text(None) == "" and clean_text(12) == "12"


# --------------------------------------------------------------------------
# numbers and money
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("5", 5), ("۵", 5), ("٥", 5), ("1,234", 1234), ("۱٬۲۳۴", 1234), ("-3", -3), ("+4", 4)])
def test_integer(raw, expected):
    form = form_of(n=raw)
    assert form.integer("n", "تعداد") == expected and form.ok


@pytest.mark.parametrize("raw", ["1.5", "abc", "1e3", "۱.۵", "--2"])
def test_integer_rejects_non_integers(raw):
    form = form_of(n=raw)
    assert form.integer("n", "تعداد") is None
    assert "عدد صحیح" in form.errors["n"]


def test_integer_bounds_and_default():
    form = form_of(low="0", high="99", empty="")
    assert form.integer("low", "تعداد", min=1) is None
    assert form.integer("high", "ظرفیت", max=50) is None
    assert form.integer("empty", "x", default=7) == 7
    assert form.integer("empty", "y", required=True) is None
    assert set(form.errors) == {"low", "high", "empty"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1870000", 1_870_000),
        ("۱٬۸۷۰٬۰۰۰", 1_870_000),
        ("1,870,000", 1_870_000),
        ("۱,۸۷۰,۰۰۰ تومان", 1_870_000),
        ("0", 0),
        (" 9000 ", 9000),
    ],
)
def test_money(raw, expected):
    form = form_of(m=raw)
    value = form.money("m", "مبلغ")
    assert value == expected and isinstance(value, int) and form.ok
    assert form.values["m"] == expected


@pytest.mark.parametrize("raw", ["12.5", "1,87,000", "abc", "-5", "10000000000000000"])
def test_money_rejects_floats_and_junk(raw):
    form = form_of(m=raw)
    assert form.money("m", "مبلغ") is None
    assert "m" in form.errors


def test_money_optional_required_default():
    form = form_of(m="")
    assert form.money("m", "مبلغ") is None and form.ok
    assert form.money("m", "مبلغ", default=0) == 0
    assert form.money("m", "مبلغ", required=True) is None and "m" in form.errors


def test_money_bounds():
    form = form_of(m="500")
    assert form.money("m", "مبلغ", min=1000) is None
    assert "باید بین" in form.errors["m"] and "۱٬۰۰۰" in form.errors["m"]


# --------------------------------------------------------------------------
# dates, times
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["2025-07-17", "۲۰۲۵-۰۷-۱۷", "1404/04/26", "۱۴۰۴/۰۴/۲۶", "1404-04-26", "۱۴۰۴/۴/۲۶"],
)
def test_date_accepts_iso_and_jalali_in_any_digits(raw):
    form = form_of(d=raw)
    assert form.date("d", "تاریخ") == dt.date(2025, 7, 17) and form.ok
    assert form.values["d"] == "2025-07-17"  # redisplay value is ISO


@pytest.mark.parametrize("raw", ["2025-13-01", "1404/13/01", "1404/12/31", "hello", "2025-02-30", "99/99/99"])
def test_date_rejects_invalid(raw):
    form = form_of(d=raw)
    assert form.date("d", "تاریخ") is None
    assert "نامعتبر" in form.errors["d"]


def test_date_range_and_required():
    form = form_of(d="2025-07-17", e="")
    assert form.date("d", "تاریخ", min=dt.date(2025, 8, 1)) is None
    assert "۱۴۰۴/۰۵/۱۰" in form.errors["d"]
    assert form.date("e", "پایان", required=True) is None
    form2 = form_of(d="2025-07-17")
    assert form2.date("d", "تاریخ", max=dt.date(2025, 7, 1)) is None
    assert form_of(d="2025-07-17").date("d", min=dt.date(2025, 7, 17), max=dt.date(2025, 7, 17)) == dt.date(2025, 7, 17)


def test_date_leap_year_day():
    assert form_of(d="1403/12/30").date("d") == dt.date(2025, 3, 20)
    assert form_of(d="1404/12/30").date("d") is None  # 1404 is not a leap year


@pytest.mark.parametrize("raw,expected", [("18:30", dt.time(18, 30)), ("۰۹:۰۵", dt.time(9, 5)), ("9:05", dt.time(9, 5)), ("00:00", dt.time(0, 0))])
def test_time(raw, expected):
    assert form_of(t=raw).time("t") == expected


@pytest.mark.parametrize("raw", ["24:00", "12:60", "1830", "ab:cd", "12:5"])
def test_time_invalid(raw):
    form = form_of(t=raw)
    assert form.time("t", "ساعت") is None and "ساعت" in form.errors["t"]


# --------------------------------------------------------------------------
# choices, booleans, mobile, lists
# --------------------------------------------------------------------------


def test_choice_and_enum():
    form = form_of(role="photographer", kind="wedding", bad="wizard", empty="")
    assert form.enum("role", "نقش", Skill) is Skill.PHOTOGRAPHER
    assert form.enum("kind", "نوع", CeremonyType) is CeremonyType.WEDDING
    assert form.enum("bad", "نقش", Skill) is None and "نامعتبر" in form.errors["bad"]
    assert form.enum("empty", "نقش", Skill, default=Skill.EDITOR) is Skill.EDITOR
    assert form.choice("role", "نقش", ["editor"], required=True) is None
    assert form.choice("x", "ایکس", [1, 2], required=True) is None and "x" in form.errors


def test_choice_returns_the_original_option_object():
    assert form_of(n="2").choice("n", "تعداد", [1, 2, 3]) == 2


@pytest.mark.parametrize("raw,expected", [("1", True), ("on", True), ("true", True), ("YES", True), ("", False), ("0", False), ("off", False), ("no", False)])
def test_boolean(raw, expected):
    assert form_of(c=raw).boolean("c") is expected
    assert form_of().boolean("c") is False


def test_mobile():
    assert form_of(m="۰۹۱۲۱۲۳۴۵۶۷").mobile("m") == "09121234567"
    assert form_of(m="+98 912 123 4567").mobile("m") == "09121234567"
    form = form_of(m="123")
    assert form.mobile("m") is None and "نامعتبر" in form.errors["m"]
    assert form_of().mobile("m", required=True) is None


def test_getlist_and_int_list():
    form = Form(MultiDict([("ids", "1"), ("ids", "۲"), ("ids", "x"), ("ids", ""), ("names", " الف "), ("names", "ب")]))
    assert form.int_list("ids") == [1, 2]
    assert form.getlist("names") == ["الف", "ب"]
    assert form.getlist("nothing") == []
    assert Form({"a": "1"}).getlist("a") == ["1"]  # plain dicts work too


def test_context_for_templates():
    form = form_of(name="", rent="5")
    form.text("name", "نام", required=True)
    form.money("rent", "اجاره")
    context = form.context()
    assert set(context) == {"form", "errors"} and context["errors"] == {"name": "نام را وارد کنید."}
    assert context["form"]["rent"] == 5


def test_form_reads_the_current_request(app):
    with app.test_request_context("/", method="POST", data={"name": "علی"}):
        assert Form().text("name") == "علی"


# --------------------------------------------------------------------------
# http helpers
# --------------------------------------------------------------------------


def test_json_ok_and_error_shapes(app):
    with app.test_request_context("/"):
        ok = json_ok({"id": 5}, status=201, extra=True)
        assert ok.status_code == 201 and ok.get_json() == {"ok": True, "data": {"id": 5}, "extra": True}
        assert json_ok().get_json() == {"ok": True, "data": None}
        err = json_error("نام الزامی است.", 422, fields={"name": "الزامی"})
        assert err.status_code == 422
        assert err.get_json() == {"ok": False, "error": "نام الزامی است.", "code": "validation_error", "fields": {"name": "الزامی"}}
        assert json_error("x", 418).get_json()["code"] == "error"
        assert json_error("x", 503).get_json()["code"] == "server_error"
        assert json_error("x", 400, code="custom").get_json()["code"] == "custom"


def test_json_errors_use_a_plain_string_so_fetchjson_shows_it(app):
    """core.js reads body.message || body.error and ignores objects: `error` must be a string."""
    with app.test_request_context("/"):
        assert isinstance(json_error("پیام").get_json()["error"], str)


@pytest.mark.parametrize(
    "headers,path,expected",
    [
        ({}, "/x", False),
        ({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, "/x", False),
        ({"Accept": "*/*"}, "/x", False),
        ({"Accept": "application/json"}, "/x", True),
        ({"Accept": "application/json, text/plain, */*"}, "/x", True),
        ({"X-Requested-With": "XMLHttpRequest"}, "/x", True),
        ({}, "/api/things", True),
        ({"Content-Type": "application/json"}, "/x", True),
    ],
)
def test_wants_json(app, headers, path, expected):
    with app.test_request_context(path, headers=headers):
        assert wants_json() is expected


def test_page_args(app):
    cases = [
        ("", (1, 20)),
        ("?page=3", (3, 20)),
        ("?page=0", (1, 20)),
        ("?page=-2", (1, 20)),
        ("?page=abc", (1, 20)),
        ("?per_page=50", (1, 50)),
        ("?per_page=5000", (1, 100)),
        ("?per_page=0", (1, 1)),
        ("?per_page=x&page=2", (2, 20)),
    ]
    for query, expected in cases:
        with app.test_request_context("/" + query):
            assert page_args() == expected, query


def test_paginate_helper(app, make_user):
    import sqlalchemy as sa

    from app.models import User
    from app.services.http import paginate

    for _ in range(25):
        make_user("staff")
    stmt = sa.select(User).order_by(User.id)
    with app.test_request_context("/?page=2&per_page=10"):
        page = paginate(stmt)
        assert page.page == 2 and len(page.items) == 10 and page.total == 25 and page.pages == 3
    with app.test_request_context("/?page=99"):
        assert paginate(stmt).items == []  # beyond the end: empty, not a 404
