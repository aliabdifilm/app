"""The monthly payroll sheet: totals per person, marking lines paid (single / bulk), undo, CSV, scoping."""

from __future__ import annotations

import datetime as dt
import re

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import AuditLog, Invoice, PayrollLine
from app.modules.accounting import queries
from app.services import ledger
from app.services.constants import InvoiceStatus, PayoutMethod, Role
from test_accounting_support import (  # noqa: F401
    csv_rows,
    flashes,
    rival,
    studio,
    titles,
    vector_form,
)

PAYROLL = "/accounting/payroll?y=1404&m=4"
PAY = "/accounting/payroll/pay"


@pytest.fixture
def invoiced(studio):
    assert studio.post(vector_form()).status_code == 302
    return studio


def pay_form(lines, **kw):
    data = {"line_id": [str(l.id) for l in lines], "paid_on": "2025-07-20", "pay_method": "cash", "note": "", "y": "1404", "m": "4"}
    data.update(kw)
    return data


def line_ids(studio):
    return [l.id for l in studio.lines()]


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------


def test_the_sheet_lists_every_person_with_totals(invoiced):
    html = invoiced.client.get(PAYROLL).get_data(as_text=True)
    for assignment in invoiced.crew:
        assert assignment.user.full_name in html
    assert "۷٬۴۸۰٬۰۰۰" in html  # 4 x 1,870,000 payable
    assert html.count("پرداخت نشده") >= 4
    assert "<ul class=\"pay-lines\"" in html and 'name="line_id"' in html
    assert 'data-pay-bar hidden' in html  # the action bar appears once something is selected


def test_empty_states(studio):
    html = studio.client.get(PAYROLL).get_data(as_text=True)
    assert "در این بازه حقوقی ثبت نشده" in html
    studio.post(vector_form())
    other_month = studio.client.get("/accounting/payroll?y=1404&m=9").get_data(as_text=True)
    assert "در این بازه حقوقی ثبت نشده" in other_month
    nothing = studio.client.get(PAYROLL + "&q=هیچکس").get_data(as_text=True)
    assert "حقوقی با این فیلتر پیدا نشد" in nothing


def test_filters_by_state_and_name(invoiced):
    first = invoiced.lines()[0]
    ledger.mark_paid([first], invoiced.manager, paid_on=dt.date(2025, 7, 20), method="cash")
    db.session.commit()
    name = invoiced.crew[0].user.full_name
    unpaid = invoiced.client.get(PAYROLL + "&state=unpaid").get_data(as_text=True)
    paid = invoiced.client.get(PAYROLL + "&state=paid").get_data(as_text=True)
    assert name not in unpaid and name in paid
    only = invoiced.client.get(PAYROLL + "&q=" + invoiced.crew[2].user.full_name).get_data(as_text=True)
    assert invoiced.crew[2].user.full_name in only and invoiced.crew[3].user.full_name not in only


def test_a_person_with_two_roles_is_one_row_and_units_count_once(studio, make_user, make_assignment):
    both = make_user(Role.STAFF, full_name="همه‌کاره")
    make_assignment(studio.event, "photographer", user=both)
    make_assignment(studio.event, "seller", user=both)
    studio.post(vector_form())
    sheet = queries.payroll_sheet(studio.atelier.id, queries.make_period(1404, 4))
    mine = next(p for p in sheet if p.name == "همه‌کاره")
    assert len(mine.lines) == 2 and mine.events == 1 and mine.units == 80
    assert mine.total == 2 * 1_870_000
    assert len(sheet) == 5  # four of the crew + this person, not six
    assert [r.label for r in mine.roles] == ["عکاس", "فروشنده"]


def test_roster_entry_and_account_of_the_same_person_are_merged(studio, make_user, make_assignment, make_roster_member, make_event):
    user = make_user(Role.STAFF, full_name="علی ثابت")
    member = make_roster_member(studio.atelier, user=user, display_name="علی ثابت")
    second = make_event(atelier=studio.atelier, status="confirmed")
    make_assignment(studio.event, "editor", user=user)
    make_assignment(second, "editor", roster_member=member)
    studio.post(vector_form())
    studio.client.post(f"/accounting/events/{second.id}/invoice", data=vector_form())
    sheet = queries.payroll_sheet(studio.atelier.id, queries.make_period(1404, 4))
    merged = [p for p in sheet if p.name == "علی ثابت"]
    assert len(merged) == 1 and merged[0].events == 2


def test_people_are_paginated(studio, make_user, make_assignment):
    for i in range(12):
        make_assignment(studio.event, "photographer", user=make_user(Role.STAFF, full_name=f"نیروی {i}"))
    studio.post(vector_form())
    first = studio.client.get(PAYROLL).get_data(as_text=True)
    assert first.count('class="card pay-person"') == queries.ListPage([], 1, 10).per_page
    assert "صفحهٔ" in first
    second = studio.client.get(PAYROLL + "&page=2").get_data(as_text=True)
    assert second.count('class="card pay-person"') == 6  # 16 people, 10 per page


# --------------------------------------------------------------------------
# marking paid
# --------------------------------------------------------------------------


def test_mark_one_line_paid(invoiced):
    target = invoiced.lines()[1]
    response = invoiced.client.post(PAY, data=pay_form([target], note="  ۱۲۳۴  "))
    assert response.status_code == 302
    line = db.session.get(PayrollLine, target.id)
    db.session.refresh(line)
    assert line.is_paid and line.paid_method == PayoutMethod.CASH and line.paid_note == "۱۲۳۴"
    assert line.paid_at == ledger.paid_at_from_date(dt.date(2025, 7, 20))
    audit = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "payroll.paid"))
    assert audit.entity_id == target.id and audit.data["amount"] == 1_870_000 and audit.data["method"] == "cash"
    assert "حقوق شما پرداخت شد" in titles(invoiced.crew[1].user)
    assert not any("حقوق شما پرداخت شد" in titles(a.user) for a in invoiced.crew if a is not invoiced.crew[1])
    html = invoiced.client.get(PAYROLL).get_data(as_text=True)
    assert "پرداخت شد" in html and "۱۴۰۴/۰۴/۲۹" in html and "نقد" in html
    assert any("پرداخت ۱ ردیف" in m for m in flashes(html))


def test_bulk_payment_settles_the_invoice(invoiced):
    lines = invoiced.lines()
    response = invoiced.client.post(PAY, data=pay_form(lines, pay_method="card_to_card", note="شمارهٔ پیگیری ۹۸۷"))
    assert response.status_code == 302
    assert all(l.is_paid and l.paid_method == PayoutMethod.CARD_TO_CARD for l in invoiced.lines())
    assert invoiced.invoice().status == InvoiceStatus.SETTLED
    again = invoiced.client.post(PAY, data=pay_form(lines))  # nothing left to pay
    assert again.status_code == 302
    page = invoiced.client.get(PAYROLL).get_data(as_text=True)
    assert any("از قبل پرداخت‌شده بود" in m for m in flashes(page))
    assert db.session.scalar(sa.select(sa.func.count(AuditLog.id)).where(AuditLog.action == "payroll.paid")) == 4


def test_the_form_remembers_the_return_page(invoiced):
    lines = invoiced.lines()
    response = invoiced.client.post(PAY, data=pay_form(lines[:1], next="/accounting/payroll?y=1404&m=4&state=unpaid"))
    assert response.headers["Location"] == "/accounting/payroll?y=1404&m=4&state=unpaid"
    evil = invoiced.client.post(PAY, data=pay_form(lines[1:2], next="https://evil.example/steal"))
    assert evil.headers["Location"].startswith("/accounting/payroll")


@pytest.mark.parametrize(
    "change, message",
    [
        ({"line_id": []}, "دست‌کم یک ردیف"),
        ({"pay_method": "bitcoin"}, "روش پرداخت"),
        ({"pay_method": ""}, "روش پرداخت"),
        ({"paid_on": ""}, "تاریخ پرداخت"),
        ({"paid_on": "nonsense"}, "تاریخ پرداخت"),
        ({"paid_on": "2999-01-01"}, "تاریخ پرداخت"),
        ({"note": "ی" * 301}, "یادداشت"),
    ],
)
def test_invalid_payment_forms_are_a_422_and_change_nothing(invoiced, change, message):
    data = pay_form(invoiced.lines()[:2])
    data.update(change)
    response = invoiced.client.post(PAY, data=data)
    assert response.status_code == 422
    html = response.get_data(as_text=True)
    assert message in html and "پرداخت ثبت نشد" in html and "data-reopen" in html
    assert not any(l.is_paid for l in invoiced.lines())


def test_a_422_keeps_the_selection_and_typed_values(invoiced):
    lines = invoiced.lines()
    response = invoiced.client.post(PAY, data=pay_form(lines[:2], pay_method="bitcoin", note="یادداشت من"))
    html = response.get_data(as_text=True)
    assert html.count("data-pay-line") >= 4
    assert f'value="{lines[0].id}" data-pay-line data-amount="1870000" checked' in html
    assert "یادداشت من" in html


def test_unmark_paid_reopens_the_settlement(invoiced):
    lines = invoiced.lines()
    invoiced.client.post(PAY, data=pay_form(lines))
    assert invoiced.invoice().status == InvoiceStatus.SETTLED
    response = invoiced.client.post("/accounting/payroll/unpay", data={"unpay_id": str(lines[0].id)})
    assert response.status_code == 302
    line = db.session.get(PayrollLine, lines[0].id)
    db.session.refresh(line)
    assert not line.is_paid and line.paid_method is None and line.paid_note is None
    assert invoiced.invoice().status == InvoiceStatus.SUBMITTED
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "payroll.unpaid")) is not None
    assert invoiced.client.post("/accounting/payroll/unpay", data={}).status_code == 404  # exactly one line
    assert invoiced.client.post("/accounting/payroll/unpay", data={"unpay_id": ["1", "2"]}).status_code == 404


def test_paid_lines_of_a_reopened_invoice_cannot_be_paid_again(invoiced):
    invoice = invoiced.invoice()
    ledger.reopen_invoice(invoice, invoiced.manager, "اصلاح")
    db.session.commit()
    response = invoiced.client.post(PAY, data=pay_form(invoiced.lines()[:1]))
    assert response.status_code == 422
    assert not any(l.is_paid for l in invoiced.lines())
    assert "حقوقی ثبت نشده" in invoiced.client.get(PAYROLL).get_data(as_text=True)  # a draft is not in the sheet


# --------------------------------------------------------------------------
# scoping
# --------------------------------------------------------------------------


def test_another_ateliers_lines_are_a_404_for_the_whole_request(invoiced, rival):
    from app.services.testing import login_client

    owner = invoiced.client.application.test_client()
    login_client(owner, rival.atelier.manager)
    assert owner.post(rival.url, data=vector_form()).status_code == 302
    foreign = db.session.scalar(sa.select(PayrollLine).join(Invoice).where(Invoice.atelier_id == rival.atelier.id))
    mine = invoiced.lines()[0]
    mixed = invoiced.client.post(PAY, data=pay_form([mine, foreign]))
    assert mixed.status_code == 404
    assert not any(l.is_paid for l in invoiced.lines()) and not db.session.get(PayrollLine, foreign.id).is_paid
    assert invoiced.client.post("/accounting/payroll/unpay", data={"unpay_id": str(foreign.id)}).status_code == 404
    assert invoiced.client.post(PAY, data=pay_form([], line_id=["999999"])).status_code == 404
    html = invoiced.client.get(PAYROLL).get_data(as_text=True)
    assert rival.crew[0].user.full_name not in html
    csv = invoiced.client.get("/accounting/payroll/export.csv?y=1404&m=4").get_data(as_text=True)
    assert rival.crew[0].user.full_name not in csv


def test_too_many_ids_at_once_is_refused(invoiced):
    ids = [str(i) for i in range(1, 400)]
    assert invoiced.client.post(PAY, data={"line_id": ids, "paid_on": "2025-07-20", "pay_method": "cash"}).status_code == 404


def test_the_atelier_switch_is_checked(invoiced, rival):
    assert invoiced.client.get(PAYROLL + f"&atelier={rival.atelier.id}").status_code == 404
    assert invoiced.client.get(PAYROLL + f"&atelier={invoiced.atelier.id}").status_code == 200


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def test_payroll_csv(invoiced):
    ledger.mark_paid(invoiced.lines()[:1], invoiced.manager, paid_on=dt.date(2025, 7, 20), method="bank_transfer", note="=1+1")
    db.session.commit()
    response = invoiced.client.get("/accounting/payroll/export.csv?y=1404&m=4")
    assert response.status_code == 200
    assert "payroll-1404-04.csv" in response.headers["Content-Disposition"]
    rows = csv_rows(response)
    text = response.get_data(as_text=True)
    assert not re.search("[۰-۹٠-٩]", text)
    assert rows[0][:3] == ["نام", "نقش", "تاریخ مراسم (میلادی)"]
    assert len(rows) == 5
    first = rows[1]
    assert first[2:5] == ["2025-07-17", "1404/04/26", invoiced.hall.name]
    assert first[5:10] == ["80", "1000000", "720000", "150000", "1870000"]
    paid = next(r for r in rows[1:] if r[10] == "پرداخت شده")
    assert paid[11:15] == ["2025-07-20", "1404/04/29", "حواله بانکی", "'=1+1"]  # formula-looking note is neutralised
    assert sum(1 for r in rows[1:] if r[10] == "پرداخت نشده") == 3
    only_unpaid = csv_rows(invoiced.client.get("/accounting/payroll/export.csv?y=1404&m=4&state=unpaid"))
    assert len(only_unpaid) == 4
