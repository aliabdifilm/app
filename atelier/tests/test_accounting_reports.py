"""Reports (trend chart, profitability tables, leaderboard), number-to-words, CSV helpers and form parsing."""

from __future__ import annotations

import datetime as dt

import pytest

from app.extensions import db
from app.modules.accounting import exports, forms, queries, reports
from app.modules.accounting.words import to_words, toman_in_words
from app.services import ledger
from app.services.constants import Role
from test_accounting_support import csv_rows, form, rival, studio, vector_form  # noqa: F401

REPORTS = "/accounting/reports?y=1404"


@pytest.fixture
def books(studio, make_event, make_hall, make_assignment, make_user):
    """Invoices in three Jalali months, two halls and with different crews.

    events: 17 Tir (hall A), 20 Tir (hall B), 5 Mordad (hall A), 2 Farvardin (hall B).
    """
    hall_b = studio.event.hall.__class__  # placeholder to keep the namespace explicit
    hall_b = make_hall(name="باغ ارم")
    star = make_user(Role.STAFF, full_name="ستارهٔ فروش")
    def book(date, hall, units, cash, people):
        event = make_event(hall=hall, atelier=studio.atelier, date=date, status="confirmed")
        for role, user in people:
            make_assignment(event, role, user=user)
        ledger.save_invoice(
            event,
            ledger.InvoiceData(lines=[{"category": "print", "size": "16x21", "qty": units}], payments={"cash": cash}, venue_rent=1_000_000),
            studio.manager,
        )
        db.session.commit()
        return event

    first = book(dt.date(2025, 7, 20), hall_b, 30, 8_000_000, [("photographer", star)])
    third = book(dt.date(2025, 7, 27), studio.hall, 100, 30_000_000, [("photographer", star), ("seller", None)])
    fourth = book(dt.date(2025, 3, 22), hall_b, 5, 500_000, [("editor", None)])
    return {"star": star, "hall_b": hall_b, "events": (first, third, fourth)}


def rows_of(studio, jy=1404, jm=0):
    period = queries.make_period(jy, jm)
    return queries.invoice_rows(studio.atelier.id, period.start, period.end)


# --------------------------------------------------------------------------
# the report page
# --------------------------------------------------------------------------


def test_year_totals_and_monthly_buckets(studio, books):
    rows = rows_of(studio)
    assert len(rows) == 3
    months = reports.month_buckets(rows, 1404)
    assert [m["events"] for m in months] == [1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0]  # Farvardin, Tir, Mordad
    assert months[3]["income"] == 8_000_000 and months[4]["income"] == 30_000_000
    assert sum(m["net"] for m in months) == sum(r.net for r in rows)
    html = studio.client.get(REPORTS).get_data(as_text=True)
    assert "روند ماهانهٔ سال ۱۴۰۴" in html and '<figure class="acc-chart">' in html and 'role="img"' in html
    assert html.count("acc-chart__bar--income") == 3 and 'class="acc-chart__net"' in html
    assert "جدول داده‌های نمودار" in html
    total_income = "۳۸٬۵۰۰٬۰۰۰"
    assert total_income in html


def test_only_final_invoices_of_my_atelier_count(studio, books, rival):
    from app.services.testing import login_client

    owner = studio.client.application.test_client()
    login_client(owner, rival.atelier.manager)
    owner.post(rival.url, data=vector_form())
    assert len(rows_of(studio)) == 3
    event = books["events"][0]
    invoice = event.invoice
    ledger.reopen_invoice(invoice, studio.manager, "اصلاح")
    db.session.commit()
    assert len(rows_of(studio)) == 2  # a reopened draft is out of the numbers until it is submitted again
    ledger.save_invoice(event, ledger.InvoiceData(lines=[], payments={}), studio.manager)
    db.session.commit()
    ledger.void_invoice(event.invoice, studio.manager, "باطل")
    db.session.commit()
    assert len(rows_of(studio)) == 2


def test_event_table_margin_and_month_filter(studio, books):
    rows = rows_of(studio, 1404, 4)
    assert [r.hall for r in rows] == ["باغ ارم"]
    only = rows[0]
    assert only.income == 8_000_000 and only.margin == only.net * 100 // only.income  # whole percent, integers only
    html = studio.client.get(REPORTS + "&m=4").get_data(as_text=True)
    assert "باغ ارم" in html and "٪" in html
    empty = studio.client.get("/accounting/reports?y=1404&m=9").get_data(as_text=True)
    assert "در این بازه فاکتوری ثبت نشده است" in empty


def test_hall_table_and_staff_leaderboard(studio, books):
    rows = rows_of(studio)
    halls = reports.hall_table(rows)
    assert {h["hall"] for h in halls} == {"باغ ارم", studio.hall.name}
    assert halls == sorted(halls, key=lambda h: (-h["net"], h["hall"]))
    assert sum(h["events"] for h in halls) == 3
    leaders = reports.leaderboard(queries.staff_rows(studio.atelier.id, dt.date(2025, 3, 20), dt.date(2026, 3, 20)))
    assert leaders[0]["name"] == "ستارهٔ فروش" and leaders[0]["units"] == 130 and leaders[0]["events"] == 2
    assert [p["units"] for p in leaders] == sorted((p["units"] for p in leaders), reverse=True)
    html = studio.client.get(REPORTS).get_data(as_text=True)
    assert "نیروهای برتر بر اساس فروش" in html and "ستارهٔ فروش" in html and "سودآوری به تفکیک تالار" in html


def test_leaderboard_counts_a_persons_units_once_per_event(studio, make_user, make_assignment, make_event):
    both = make_user(Role.STAFF, full_name="دو نقشه")
    event = make_event(atelier=studio.atelier, status="confirmed")
    make_assignment(event, "photographer", user=both)
    make_assignment(event, "seller", user=both)
    ledger.save_invoice(event, ledger.InvoiceData(lines=[{"category": "print", "size": "16x21", "qty": 7}], payments={}), studio.manager)
    db.session.commit()
    ranked = reports.leaderboard(queries.staff_rows(studio.atelier.id, dt.date(2025, 1, 1), dt.date(2025, 12, 31)))
    assert ranked[0]["units"] == 7 and ranked[0]["events"] == 1 and ranked[0]["earned"] == 2 * (1_000_000 + 7 * 9_000)


def test_event_rows_are_paginated(studio, make_event):
    for _ in range(17):
        event = make_event(atelier=studio.atelier, date=dt.date(2025, 7, 18), status="confirmed")
        ledger.save_invoice(event, ledger.InvoiceData(lines=[], payments={"cash": 1}), studio.manager)
    db.session.commit()
    first = studio.client.get(REPORTS).get_data(as_text=True)
    second = studio.client.get(REPORTS + "&page=2").get_data(as_text=True)
    assert first.count("<tr>") - second.count("<tr>") >= 10  # 15 rows on page one, 2 on page two
    assert "صفحهٔ" in first


def test_events_csv(studio, books):
    response = studio.client.get("/accounting/reports/export.csv?y=1404")
    assert response.status_code == 200 and "events-1404.csv" in response.headers["Content-Disposition"]
    rows = csv_rows(response)
    assert rows[0][0] == "تاریخ (میلادی)" and len(rows) == 4
    assert rows[1][:2] == ["2025-07-27", "1404/05/05"]
    assert [int(r[7]) for r in rows[1:]] == [r.net for r in rows_of(studio)]
    month = csv_rows(studio.client.get("/accounting/reports/export.csv?y=1404&m=4"))
    assert len(month) == 2 and "events-1404-04.csv" in studio.client.get("/accounting/reports/export.csv?y=1404&m=4").headers["Content-Disposition"]


def test_reports_are_empty_but_fine_for_a_new_atelier(client, login_as, make_atelier):
    atelier = make_atelier()
    login_as(client, atelier.manager)
    html = client.get(REPORTS).get_data(as_text=True)
    assert "هنوز فاکتوری برای این سال ثبت نشده" in html and "acc-chart__bar" not in html
    assert client.get("/accounting/reports/export.csv").status_code == 200


# --------------------------------------------------------------------------
# chart geometry and helpers
# --------------------------------------------------------------------------


def bucket(jm, income=0, expenses=0, events=1):
    return {"jm": jm, "label": f"m{jm}", "income": income, "expenses": expenses, "net": income - expenses, "units": 0, "events": events}


def test_trend_chart_geometry():
    months = [bucket(1, 4_000_000, 1_000_000), bucket(2, 2_000_000, 3_000_000)] + [bucket(i, events=0) for i in range(3, 13)]
    chart = reports.trend_chart(months)
    assert chart["has_data"] and chart["unit"] == "میلیون تومان"
    income = [b for b in chart["bars"] if b["kind"] == "income"]
    assert len(income) == 2 and {b["month"] for b in chart["bars"]} == {"m1", "m2"}
    tallest = max(chart["bars"], key=lambda b: b["h"])
    assert tallest["value"] == 4_000_000
    # months run right to left: month 1 is the right-most label
    xs = {label["index"]: label["x"] for label in chart["labels"]}
    assert xs[1] > xs[2] > xs[12]
    # everything is an integer inside the viewBox, and a negative month sits below the zero line
    for bar in chart["bars"]:
        assert all(isinstance(bar[k], int) for k in ("x", "y", "w", "h"))
        assert 0 <= bar["x"] and bar["x"] + bar["w"] <= chart["width"] and bar["y"] >= 0
    below = [d for d in chart["dots"] if d["value"] < 0]
    assert below and below[0]["y"] > chart["zero_y"]
    assert chart["net_line"].count(",") == 2  # two months with events


def test_trend_chart_without_data_and_small_amounts():
    empty = reports.trend_chart([bucket(i, events=0) for i in range(1, 13)])
    assert empty["has_data"] is False and empty["bars"] == []
    small = reports.trend_chart([bucket(1, 5_000, 2_000)] + [bucket(i, events=0) for i in range(2, 13)])
    assert small["unit"] == "هزار تومان"


@pytest.mark.parametrize("top, step", [(1, 1), (9, 5), (4_000_000, 1_000_000), (4_100_000, 2_000_000), (30_000_000, 10_000_000)])
def test_nice_steps(top, step):
    assert reports._nice_step(top, 4) == step


def test_list_page_matches_the_pagination_macro_contract():
    page = queries.ListPage(list(range(23)), 2, 10)
    assert (page.page, page.pages, page.total, page.has_prev, page.has_next, page.prev_num, page.next_num) == (2, 3, 23, True, True, 1, 3)
    assert page.items == list(range(10, 20))
    assert list(page.iter_pages()) == [1, 2, 3]
    last = queries.ListPage(list(range(23)), 99, 10)
    assert last.page == 3 and last.items == [20, 21, 22] and not last.has_next
    empty = queries.ListPage([], 1, 10)
    assert empty.pages == 1 and empty.items == [] and list(empty.iter_pages()) == [1]


def test_period_helpers():
    today = dt.date(2025, 7, 17)  # 26 Tir 1404
    assert queries.resolve_period({}, today).args == {"y": 1404, "m": 4}
    assert queries.resolve_period({"y": "1403", "m": "12"}, today).args == {"y": 1403, "m": 12}
    assert queries.resolve_period({"y": "1403"}, today).is_year
    assert queries.resolve_period({"y": "۱۴۰۳", "m": "۱۲"}, today).args == {"y": 1403, "m": 12}
    assert queries.resolve_period({"y": "junk", "m": "13"}, today).args == {"y": 1404, "m": 4}
    month = queries.make_period(1404, 12)
    assert month.shifted(1).args == {"y": 1405, "m": 1} and queries.make_period(1404, 1).shifted(-1).args == {"y": 1403, "m": 12}
    assert queries.make_period(1404, 0).shifted(1).args == {"y": 1405, "m": 0}
    assert month.start == dt.date(2026, 2, 20) and month.end == dt.date(2026, 3, 20)


# --------------------------------------------------------------------------
# words, csv cells, form parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number, words",
    [
        (0, "صفر"),
        (7, "هفت"),
        (11, "یازده"),
        (20, "بیست"),
        (21, "بیست و یک"),
        (100, "صد"),
        (999, "نهصد و نود و نه"),
        (1000, "هزار"),
        (1001, "هزار و یک"),
        (2000, "دو هزار"),
        (1_870_000, "یک میلیون و هشتصد و هفتاد هزار"),
        (12_345_678, "دوازده میلیون و سیصد و چهل و پنج هزار و ششصد و هفتاد و هشت"),
        (1_000_000_000, "یک میلیارد"),
        (-250, "منفی دویست و پنجاه"),
    ],
)
def test_amounts_in_words(number, words):
    assert to_words(number) == words


def test_words_reject_junk_and_add_the_currency():
    assert toman_in_words(1_870_000) == "یک میلیون و هشتصد و هفتاد هزار تومان"
    for bad in (1.5, "12", None, True):
        with pytest.raises(ValueError):
            to_words(bad)
    with pytest.raises(ValueError):
        to_words(10**30)


def test_csv_cells_are_neutralised_and_digits_stay_ascii():
    response = exports.csv_response("x.csv", [["a", "=1+1", "+x", "-y", "@z", "\t=cmd", "نام", 12, -5, None, True]])
    row = csv_rows(response)[0]
    assert row == ["a", "'=1+1", "'+x", "'-y", "'@z", "'=cmd", "نام", "12", "-5", "", "1"]
    assert exports.jalali_ascii(dt.date(2025, 7, 17)) == "1404/04/26" and exports.jalali_ascii(None) == ""
    # a timestamp is shown as its Iran-time day (21:00 UTC is already the next day in Tehran)
    assert exports.jalali_ascii(dt.datetime(2025, 7, 17, 21, 0)) == "1404/04/27"
    assert exports.gregorian(dt.datetime(2025, 7, 17, 21, 0)) == "2025-07-18"


def test_form_parsing_collects_every_problem_and_keeps_the_input(studio):
    suggestions = ledger.suggest_expenses(studio.event)
    data = form(
        qty={"print__16x21": "x", "frame__30x40": "-1"},
        pay={"cash": "abc"},
        **{"custom__chassis__4__size": "9", "custom__chassis__4__qty": "2", "extra__1__amount": "5", "venue_rent": "-4"},
    )
    parsed = forms.parse(data, suggestions)
    assert not parsed.ok and parsed.data is None
    assert {"qty__print__16x21", "qty__frame__30x40", "pay__cash", "custom__chassis__4__size", "extra__1__label", "venue_rent"} <= set(parsed.errors)
    assert parsed.state["qty"]["print__16x21"] == "x"  # redisplay what was typed
    assert parsed.state["custom"]["chassis"][0]["idx"] == 4 and parsed.state["custom"]["chassis"][0]["size"] == "9"
    assert parsed.state["extras"][0]["amount"] == "5" and parsed.state["pay"]["cash"] == "abc"
    assert parsed.nonce == data["save_token"] and parsed.base_revision == 0


def test_form_parsing_happy_path_and_defaults(studio):
    suggestions = ledger.suggest_expenses(studio.event)
    parsed = forms.parse(form(qty={"print__16x21": "۱۲"}, pay={"cash": "۱٬۲۰۰"}, notes="  سلام  "), suggestions)
    assert parsed.ok
    assert [(l.category.value, l.size_label, l.qty) for l in parsed.data.lines] == [("print", "16x21", 12)]
    assert parsed.data.payments["cash"] == 1200 and parsed.data.venue_rent is None and parsed.data.notes == "سلام"
    state = forms.blank_state(suggestions)
    assert state["venue_rent"] == 3_000_000 and state["base_revision"] == 0 and len(state["nonce"]) >= 8
    assert forms.parse({}, suggestions).nonce == ""  # a missing / malformed token is simply not a token
