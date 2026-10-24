"""The accounting page: event cards, filters, summary, the hidden invoice accordion, navigation,
a sweep of every GET route, query counts and the static-asset rules (no inline code, tokens only)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event

from app.extensions import db
from app.models import Invoice
from app.modules.accounting import queries
from app.services import ledger, nav
from app.services.constants import Role
from test_accounting_support import (  # noqa: F401
    CREW_ROLES,
    form,
    rival,
    studio,
    vector_form,
)

INDEX = "/accounting/?y=1404&m=4"
ROOT = Path(__file__).resolve().parent.parent


def get(studio, path=INDEX):
    response = studio.client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------
# the cards
# --------------------------------------------------------------------------


def test_a_card_shows_what_agents_md_asks_for(studio):
    html = get(studio)
    assert studio.atelier.name in html
    assert "پنجشنبه ۲۶ تیر ۱۴۰۴" in html  # Jalali date + weekday of 2025-07-17
    assert studio.hall.name in html
    assert 'class="badge badge--accent">قطعی</span>' in html
    for assignment in studio.crew:
        assert assignment.user.full_name in html
    for label in ("پرزنتور", "عکاس", "فروشنده", "ادیتور"):
        assert f'<span class="acc-crew__role">{label}</span>' in html
    assert "ثبت فاکتور" in html and "ویرایش فاکتور" not in html
    assert "data-invoice-open" in html and f'href="/accounting/events/{studio.event.id}/invoice"' in html
    assert 'id="invoice-modal"' in html and 'data-invoice-host' in html
    assert "accounting.js" in html and "accounting.css" in html


def test_crew_is_grouped_by_role_and_unanswered_invitations_are_marked(studio, make_user, make_assignment):
    pending = make_user(Role.STAFF, full_name="منتظر پاسخ")
    declined = make_user(Role.STAFF, full_name="ردکننده")
    make_assignment(studio.event, "seller", user=pending, status="invited")
    make_assignment(studio.event, "seller", user=declined, status="declined")
    html = get(studio)
    assert "منتظر پاسخ" in html and "(در انتظار پاسخ)" in html
    assert "ردکننده" not in html


def test_the_invoice_hangs_under_the_card_closed_by_default(studio):
    studio.post(vector_form())
    html = get(studio)
    invoice = studio.invoice()
    assert "ویرایش فاکتور" in html and "فاکتور شده" in html
    assert f'data-accordion-toggle="inv-{studio.event.id}" aria-expanded="false"' in html  # the «فاکتور» button
    assert re.search(rf'<div class="accordion" id="inv-{studio.event.id}"', html)  # not .is-open
    assert re.search(rf'<div class="collapse" id="inv-{studio.event.id}-panel"', html)
    # what the accordion shows: products, payments, expenses, payroll, net result, print + CSV
    for text in ("محصولات فروش‌رفته", "دریافتی‌ها", "هزینه‌ها", "حقوق نیروها", "سود خالص", "جمع واحدهای پورسانت"):
        assert text in html
    assert f"/accounting/invoices/{invoice.id}/print" in html and f"/accounting/invoices/{invoice.id}/export.csv" in html
    assert "۱٬۸۷۰٬۰۰۰" in html


def test_cancelled_events_get_no_invoice_button(studio, make_event):
    cancelled = make_event(atelier=studio.atelier, status="cancelled", title="لغو شده")
    html = get(studio)
    assert "لغو شده" in html
    assert studio.client.get(f"/accounting/events/{cancelled.id}/invoice").status_code == 409
    assert html.count("data-invoice-open") == 1


def test_a_future_event_is_marked(studio, clock):
    clock.set("2025-07-01 08:00:00")
    assert "این مراسم هنوز برگزار نشده است" in get(studio)


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------


@pytest.fixture
def season(studio, make_event, make_hall, make_assignment, make_roster_member):
    """Four more events: another month, another hall, a done one, one with a roster member."""
    other_hall = make_hall(name="باغ ارم")
    august = make_event(hall=other_hall, atelier=studio.atelier, date=dt.date(2025, 8, 20), status="confirmed", title="نامزدی کریمی")
    done = make_event(atelier=studio.atelier, date=dt.date(2025, 7, 19), status="done", title="روز بعد")
    member = make_roster_member(studio.atelier, display_name="خانم ثابت")
    make_assignment(done, "editor", roster_member=member)
    return {"august": august, "done": done, "other_hall": other_hall, "member": member}


def test_month_and_year_filters(studio, season):
    assert get(studio, INDEX).count("<article") == 2  # the 17th and the 19th of Tir
    assert get(studio, "/accounting/?y=1404&m=5").count("<article") == 1  # Mordad
    assert get(studio, "/accounting/?y=1404&m=0").count("<article") == 3  # the whole year
    assert "نامزدی کریمی" not in get(studio, INDEX)
    # junk falls back to the current month instead of failing
    assert get(studio, "/accounting/?y=abc&m=99").count("<article") == 0
    assert "خلاصهٔ" in get(studio, "/accounting/?y=%D9%A1%D9%A4%D9%A0%D9%A4&m=%D9%A5")  # Persian digits are accepted


def test_status_filter(studio, season):
    assert get(studio, INDEX + "&status=done").count("<article") == 1
    assert get(studio, INDEX + "&status=confirmed").count("<article") == 1
    assert get(studio, INDEX + "&status=nonsense").count("<article") == 2  # ignored


def test_search_finds_halls_people_roster_and_titles(studio, season):
    assert get(studio, "/accounting/?y=1404&m=0&q=باغ").count("<article") == 1
    assert get(studio, "/accounting/?y=1404&m=0&q=" + studio.crew[1].user.full_name).count("<article") == 1
    assert get(studio, "/accounting/?y=1404&m=0&q=ثابت").count("<article") == 1  # a roster member
    assert get(studio, "/accounting/?y=1404&m=0&q=کریمی").count("<article") == 1  # the title
    assert get(studio, "/accounting/?y=1404&m=0&q=%25").count("<article") == 0  # LIKE wildcards are escaped
    html = get(studio, "/accounting/?y=1404&m=0&q=هیچ‌چیز")
    assert "مراسمی با این فیلتر پیدا نشد" in html and "پاک کردن فیلتر" in html


def test_awaiting_filter_and_counter(studio, season, clock):
    clock.set("2025-07-30 08:00:00")  # the 17th and 19th are over, the 20th of Mordad is not
    html = get(studio)
    assert "۲ مراسم هنوز فاکتور ندارد" in html
    assert get(studio, INDEX + "&status=awaiting").count("<article") == 2
    studio.post(vector_form())
    assert get(studio, INDEX + "&status=awaiting").count("<article") == 1
    assert queries.period_summary(studio.atelier.id, queries.make_period(1404, 4), dt.date(2025, 7, 30))["awaiting"] == 1


def test_the_month_summary(studio, make_event, make_assignment):
    second = make_event(atelier=studio.atelier, status="confirmed")
    make_assignment(second, "photographer", base_pay=0, commission_per_unit=1_000, bonus_tiers=[])
    studio.post(vector_form())
    studio.client.post(f"/accounting/events/{second.id}/invoice", data=form(qty={"print__16x21": 10}, pay={"cash": 5_000_000}))
    summary = queries.period_summary(studio.atelier.id, queries.make_period(1404, 4), dt.date(2025, 8, 1))
    first = db.session.scalar(sa.select(Invoice).where(Invoice.event_id == studio.event.id))
    second_invoice = db.session.scalar(sa.select(Invoice).where(Invoice.event_id == second.id))
    assert summary["income"] == 25_000_000 == first.income_total + second_invoice.income_total
    assert summary["expenses"] == first.expense_total + second_invoice.expense_total
    assert summary["net"] == first.net_profit + second_invoice.net_profit
    assert summary["units"] == 90 and summary["invoices"] == 2 and summary["events"] == 2
    html = get(studio)
    for text in ("۲۵٬۰۰۰٬۰۰۰", "درآمد", "هزینه‌ها", "سود خالص", "محصولات فروش‌رفته"):
        assert text in html
    # a reopened (draft) invoice is not counted
    ledger.reopen_invoice(first, studio.manager, "اصلاح")
    db.session.commit()
    assert queries.period_summary(studio.atelier.id, queries.make_period(1404, 4), dt.date(2025, 8, 1))["invoices"] == 1


def test_cards_are_paginated(studio, make_event):
    for _ in range(13):
        make_event(atelier=studio.atelier, date=dt.date(2025, 7, 20), status="confirmed")
    page_one = get(studio)
    assert page_one.count("<article") == 12 and "صفحهٔ" in page_one
    assert get(studio, INDEX + "&page=2").count("<article") == 2  # 14 events in total
    assert get(studio, INDEX + "&page=99").count("<article") == 0  # past the end is just empty


def test_other_ateliers_events_and_people_never_show(studio, rival):
    html = get(studio)
    assert rival.hall.name not in html and rival.crew[0].user.full_name not in html and rival.event.title not in html
    assert get(studio, INDEX + "&q=" + rival.crew[0].user.full_name).count("<article") == 0


def test_an_empty_atelier_gets_guidance(client, login_as, make_atelier):
    atelier = make_atelier()
    login_as(client, atelier.manager)
    html = client.get(INDEX).get_data(as_text=True)
    assert "در این بازه مراسمی ثبت نشده" in html and "<article" not in html


def test_a_manager_with_two_ateliers_can_switch(client, login_as, make_atelier, make_event, make_user):
    manager = make_user(Role.ATELIER_MANAGER)
    first, second = make_atelier(manager=manager, name="اتلیه اول"), make_atelier(manager=manager, name="اتلیه دوم")
    make_event(atelier=first, title="مراسم اول")
    make_event(atelier=second, title="مراسم دوم")
    login_as(client, manager)
    page = client.get(INDEX).get_data(as_text=True)
    assert "مراسم اول" in page and "مراسم دوم" not in page and 'name="atelier"' in page
    switched = client.get(INDEX + f"&atelier={second.id}").get_data(as_text=True)
    assert "مراسم دوم" in switched and "مراسم اول" not in switched
    assert "مراسم دوم" in client.get(INDEX).get_data(as_text=True)  # remembered in the session


def test_a_manager_without_an_atelier_is_sent_to_onboarding(client, login_as, make_user):
    login_as(client, make_user(Role.ATELIER_MANAGER))
    response = client.get("/accounting/")
    assert response.status_code in (302, 404)  # onboarding redirect (or a message when no module offers one)


# --------------------------------------------------------------------------
# navigation
# --------------------------------------------------------------------------


def test_the_menu_entry_and_its_badge(app, studio, make_event, clock):
    clock.set("2025-07-30 08:00:00")
    make_event(atelier=studio.atelier, status="confirmed")
    with app.test_request_context("/accounting/"):
        items = nav.menu_for(studio.manager, endpoint="accounting.index", path="/accounting/")
    entry = next(item for item in items if item["endpoint"] == "accounting.index")
    assert entry["label"] == "حسابداری" and entry["active"] and entry["icon"] == "coins"
    assert entry["badge"] == "۲"  # two past events still need an invoice
    with app.test_request_context("/accounting/"):
        for other in (Role.STAFF, Role.HALL_MANAGER):
            assert not [i for i in nav.items_for(type("U", (), {"role": other, "is_authenticated": True})()) if i.endpoint == "accounting.index"]


# --------------------------------------------------------------------------
# every GET route, query counts
# --------------------------------------------------------------------------


def test_every_get_route_answers_without_a_server_error(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    values = {"event_id": studio.event.id, "invoice_id": invoice.id}
    app = studio.client.application
    seen = []
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith("accounting.") or "GET" not in rule.methods:
            continue
        path = rule.rule
        for name, value in values.items():
            path = path.replace(f"<int:{name}>", str(value))
        response = studio.client.get(path)
        assert response.status_code == 200, (rule.endpoint, path, response.status_code)
        seen.append(rule.endpoint)
    assert {"accounting.index", "accounting.invoice_form", "accounting.invoice_print", "accounting.invoice_csv",
            "accounting.payroll_index", "accounting.payroll_csv", "accounting.reports_index", "accounting.reports_csv"} <= set(seen)
    for query in ("?y=1404&m=4", "?y=1404&m=0", "?y=1404&m=4&status=invoiced&q=x", "?page=3"):
        for path in ("/accounting/", "/accounting/payroll", "/accounting/reports"):
            assert studio.client.get(path + query).status_code == 200


def test_every_post_route_is_protected(studio):
    """Each POST route is role-gated and CSRF-protected (the 403 / 404 / 400 cases are tested per route)."""
    app = studio.client.application
    posts = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("accounting.") and "POST" in r.methods]
    assert {r.endpoint for r in posts} == {
        "accounting.invoice_save", "accounting.invoice_preview", "accounting.invoice_reopen",
        "accounting.invoice_void", "accounting.payroll_pay", "accounting.payroll_unpay",
    }
    for rule in posts:
        assert getattr(app.view_functions[rule.endpoint], "__atelier_roles__", None) == frozenset({"atelier_manager"})


def _count_selects(client, url) -> int:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sa_event.listen(db.engine, "before_cursor_execute", record)
    try:
        assert client.get(url).status_code == 200
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", record)
    return len(statements)


def test_the_card_list_has_no_n_plus_one(studio, make_event, make_assignment):
    def invoiced_event(day):
        event = make_event(atelier=studio.atelier, date=dt.date(2025, 7, day), status="confirmed")
        for role in CREW_ROLES:
            make_assignment(event, role)
        ledger.save_invoice(event, ledger.InvoiceData(lines=[{"category": "print", "size": "16x21", "qty": 5}], payments={"cash": 1000}), studio.manager)
        db.session.commit()

    invoiced_event(20)
    invoiced_event(21)
    year = "/accounting/?y=1404&m=0"
    few = _count_selects(studio.client, year)
    for day in range(22, 30):
        invoiced_event(day)
    many = _count_selects(studio.client, year)
    assert many <= few + 1, f"{few} queries for 2 cards, {many} for 10 cards"
    for path in ("/accounting/payroll?y=1404&m=4", "/accounting/reports?y=1404"):
        assert _count_selects(studio.client, path) < 40


# --------------------------------------------------------------------------
# assets and markup rules
# --------------------------------------------------------------------------

TEMPLATES = sorted((ROOT / "app" / "templates" / "accounting").glob("*.html"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_templates_ship_no_inline_code_or_external_assets(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", text), "inline <script>"
    assert 'style="' not in text and "<style" not in text, "inline style"
    assert not re.search(r"\son[a-z]+\s*=", text), "inline event handler"
    assert not re.search(r"(?:src|href)=\"https?://", text), "external asset"
    assert "|safe" not in text and "Markup(" not in text, "user text must never be marked safe"


def test_rendered_pages_ship_no_inline_script_or_style(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    for path in (INDEX, studio.url, studio.url + "?fragment=1", "/accounting/payroll?y=1404&m=4", "/accounting/reports?y=1404",
                 f"/accounting/invoices/{invoice.id}/print"):
        html = get(studio, path)
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert " style=" not in html and "<style" not in html, path
        assert not re.search(r"\son[a-z]+=\"", html), path
        assert "cdn." not in html and "googleapis" not in html, path


def test_css_uses_tokens_only():
    css = (ROOT / "app" / "static" / "css" / "accounting.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "hard-coded colour"
    assert not re.search(r"\b(?:left|right)\s*:", css), "use logical properties (inset-inline-*)"
    assert not re.search(r"(?:margin|padding)-(?:left|right)", css)


def test_javascript_displays_numbers_but_never_computes_pay():
    js = (ROOT / "app" / "static" / "js" / "accounting.js").read_text(encoding="utf-8")
    for forbidden in ("commission_per_unit", "bonus_tiers", "base_pay", "parseFloat", "toLocaleString", "?.", "??", "async "):
        assert forbidden not in js, forbidden
    assert "data-preview-url" in js  # the live summary asks the server


def test_no_field_is_named_like_a_form_property(studio):
    """A control called ``method`` / ``action`` / ``name`` ... shadows the property on its <form> (``form.method`` becomes the
    <select>), which breaks scripts that read it - core.js's double-submit lock does.  Keep such names out of every form."""
    shadowing = {"method", "action", "name", "id", "target", "elements", "length", "submit", "reset", "enctype", "encoding", "acceptcharset"}
    studio.post(vector_form())
    pages = [INDEX, studio.url, studio.url + "?fragment=1", "/accounting/payroll?y=1404&m=4", "/accounting/reports?y=1404"]
    for path in pages:
        html = get(studio, path)
        names = set(re.findall(r'<(?:input|select|textarea|button)\b[^>]*\bname="([^"]+)"', html))
        assert not (names & shadowing), (path, names & shadowing)
