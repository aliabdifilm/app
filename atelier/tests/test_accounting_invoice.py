"""The invoice popup through the HTTP layer: save, edit, validation, lifecycle, CSV, security.

The AGENTS.md vector (80 units => 1,870,000 for each of four people) is asserted end to end,
together with the 69/70 bonus boundary, "other size" weights, ``counts_for_commission = false``,
edit recomputation, paid-line protection, tenant isolation, CSRF and the rent override.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import (
    AtelierHall,
    AuditLog,
    Equipment,
    ExpenseLine,
    Invoice,
    PayrollLine,
)
from app.services import ledger, staffing
from app.services.constants import (
    EquipmentKind,
    EquipmentOwnership,
    EventStatus,
    ExpenseKind,
    InvoiceStatus,
    Role,
)
from test_accounting_support import (  # noqa: F401
    CREW_ROLES,
    csv_rows,
    flashes,
    form,
    rival,
    studio,
    titles,
    vector_form,
)

INDEX = "/accounting/?y=1404&m=4"


def by_assignment(invoice):
    return {line.assignment_id: line for line in invoice.payroll_lines}


# --------------------------------------------------------------------------
# the vector, boundaries, weights
# --------------------------------------------------------------------------


def test_agents_md_vector_end_to_end(studio, bus):
    response = studio.post(vector_form())
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/accounting/?y=1404&m=4#event-")

    invoice = studio.invoice()
    assert invoice.status == InvoiceStatus.SUBMITTED and invoice.revision == 1
    assert invoice.units_total == 80
    assert len(invoice.payroll_lines) == 4
    for line in invoice.payroll_lines:
        assert (line.units, line.base, line.commission, line.bonus, line.total) == (80, 1_000_000, 720_000, 150_000, 1_870_000)
        assert line.applied_tier == {"threshold": 70, "amount": 150_000}
    assert invoice.income_total == 20_000_000
    assert invoice.expense_total == 3_000_000 + 4 * 1_870_000
    assert invoice.net_profit == 20_000_000 - invoice.expense_total
    db.session.refresh(studio.event)
    assert studio.event.status == EventStatus.INVOICED

    # event bus + audit + staff notifications
    saved = [payload for name, payload in bus if name == "invoice.saved"]
    assert len(saved) == 1
    assert saved[0]["invoice_id"] == invoice.id and saved[0]["event_id"] == studio.event.id
    assert saved[0]["atelier_id"] == studio.atelier.id and saved[0]["created"] is True
    audit = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "invoice.submitted"))
    assert audit.actor_id == studio.manager.id and audit.entity_id == invoice.id
    for assignment in studio.crew:
        assert "حقوق شما ثبت شد" in titles(assignment.user)
    assert any("فاکتور ثبت شد" in m for m in flashes(studio.client.get(INDEX).get_data(as_text=True)))


def test_each_person_is_paid_with_their_own_terms(studio):
    terms = [
        (2_000_000, 10_000, [{"threshold": 50, "amount": 100_000}, {"threshold": 90, "amount": 400_000}]),
        (500_000, 0, []),
        (1_500_000, 5_000, [{"threshold": 80, "amount": 1}]),
        (0, 20_000, []),
    ]
    for assignment, (base, rate, tiers) in zip(studio.crew, terms):
        assignment.base_pay, assignment.commission_per_unit, assignment.bonus_tiers = base, rate, tiers
    db.session.commit()
    assert studio.post(vector_form()).status_code == 302
    lines = by_assignment(studio.invoice())
    assert [lines[a.id].total for a in studio.crew] == [
        2_000_000 + 800_000 + 100_000,  # the highest reached tier (50), not 50 + 90
        500_000,
        1_500_000 + 400_000 + 1,
        1_600_000,
    ]


@pytest.mark.parametrize("units, bonus", [(69, 0), (70, 150_000), (71, 150_000)])
def test_bonus_boundary_69_70(studio, units, bonus):
    studio.post(form(qty={"print__16x21": units}))
    for line in studio.invoice().payroll_lines:
        assert line.bonus == bonus
        assert line.total == 1_000_000 + units * 9_000 + bonus


def test_other_size_rows_use_their_weight_and_any_digits(studio):
    data = vector_form()
    data.update(
        {
            "qty__print__16x21": "10",
            "qty__print__20x30": "0",
            "qty__raw_file": "0",
            "custom__print__0__size": "۷۰ × ۵۰",
            "custom__print__0__qty": "۵",
            "custom__print__0__weight": "۳",
            "custom__frame__2__size": "100x40",
            "custom__frame__2__qty": "2",
        }
    )
    assert studio.post(data).status_code == 302
    invoice = studio.invoice()
    assert invoice.units_total == 10 + 15 + 2  # weight 3 on the print row, default 1 on the frame row
    custom = {(l.category.value, l.size_label): l for l in invoice.lines if l.is_custom_size}
    assert custom[("print", "50x70")].commission_weight == 3 and custom[("print", "50x70")].qty == 5
    assert custom[("frame", "40x100")].commission_weight == 1
    assert {l.total for l in invoice.payroll_lines} == {1_000_000 + 27 * 9_000}


def test_a_product_can_be_left_out_of_the_commission(studio):
    data = form(qty={"print__16x21": 10, "frame__20x30": 100}, nocomm__frame__20x30="1")
    studio.post(data)
    invoice = studio.invoice()
    assert invoice.units_total == 10
    frame = next(l for l in invoice.lines if l.category.value == "frame")
    assert frame.qty == 100 and frame.counts_for_commission is False
    assert {l.units for l in invoice.payroll_lines} == {10}
    # the row stays visible and keeps its flag when the invoice is edited again
    page = studio.client.get(studio.url).get_data(as_text=True)
    assert re.search(r'name="nocomm__frame__20x30" value="1" checked', page)


def test_a_file_row_never_takes_a_size(studio):
    data = vector_form()
    data["custom__print__0__size"] = ""
    data["custom__print__0__qty"] = ""  # an untouched blank row is simply ignored
    assert studio.post(data).status_code == 302
    assert not [l for l in studio.invoice().lines if l.is_custom_size]


# --------------------------------------------------------------------------
# edit, replay, conflicts, paid lines
# --------------------------------------------------------------------------


def test_editing_recomputes_everything_in_place(studio):
    studio.post(vector_form())
    before = studio.invoice()
    ids = sorted(l.id for l in before.payroll_lines)
    expense_count = db.session.scalar(sa.select(sa.func.count(ExpenseLine.id)))
    response = studio.post(form(qty={"print__16x21": 60}, pay={"cash": 1_000_000}, revision=1))
    assert response.status_code == 302
    invoice = studio.invoice()
    assert invoice.revision == 2 and invoice.units_total == 60 and invoice.income_total == 1_000_000
    assert sorted(l.id for l in invoice.payroll_lines) == ids
    assert {l.total for l in invoice.payroll_lines} == {1_000_000 + 60 * 9_000}  # below the 70 tier now
    assert db.session.scalar(sa.select(sa.func.count(ExpenseLine.id))) == expense_count
    assert [(l.category.value, l.size_label, l.qty) for l in invoice.lines] == [("print", "16x21", 60)]
    assert invoice.net_profit == 1_000_000 - invoice.expense_total
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "invoice.updated")) is not None
    assert any("تغییرات فاکتور ذخیره شد" in m for m in flashes(studio.client.get(INDEX).get_data(as_text=True)))


def test_double_submit_saves_once(studio, bus):
    data = vector_form(token="one-click-token")
    first = studio.post(data)
    second = studio.post(data)  # the user tapped twice / the request was retried
    assert first.status_code == second.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 1
    assert studio.invoice().revision == 1
    assert len([p for n, p in bus if n == "invoice.saved"]) == 1
    assert "قبلاً ثبت شده بود" in " ".join(flashes(studio.client.get(INDEX).get_data(as_text=True)))


def test_a_stale_form_is_a_conflict_not_a_silent_overwrite(studio):
    studio.post(vector_form())
    response = studio.post(form(qty={"print__16x21": 5}, revision=0))  # another tab, older form
    assert response.status_code == 409
    assert studio.invoice().units_total == 80
    ajax = studio.post(form(qty={"print__16x21": 5}, revision=0), headers={"X-Requested-With": "XMLHttpRequest"})
    assert ajax.status_code == 409 and ajax.get_json()["ok"] is False


def test_paid_payroll_lines_survive_an_edit_and_raise_a_warning(studio):
    studio.post(vector_form())
    first = studio.invoice().payroll_lines[0]
    ledger.mark_paid([first], studio.manager, paid_on=dt.date(2025, 7, 20), method="card_to_card", note="۱۲۳۴")
    db.session.commit()
    response = studio.post(form(qty={"print__16x21": 60}, revision=1))
    assert response.status_code == 302
    line = db.session.get(PayrollLine, first.id)
    db.session.refresh(line)
    assert line.is_paid and line.paid_note == "۱۲۳۴"  # the paid marker is preserved
    assert line.total == 1_540_000  # while the figure follows the formula
    messages = flashes(studio.client.get(INDEX).get_data(as_text=True))
    assert any("پیش‌تر پرداخت شده بود" in m and "۱٬۵۴۰٬۰۰۰" in m and "۱٬۸۷۰٬۰۰۰" in m for m in messages)
    # the payroll sheet flags the difference
    sheet = studio.client.get("/accounting/payroll?y=1404&m=4").get_data(as_text=True)
    assert "مغایرت" in sheet


def test_events_that_cannot_be_invoiced_answer_409(client, login_as, make_atelier, make_event):
    atelier = make_atelier()
    draft = make_event(atelier=atelier, status="draft")
    login_as(client, atelier.manager)
    url = f"/accounting/events/{draft.id}/invoice"
    assert client.get(url).status_code == 409
    assert client.post(url, data=vector_form()).status_code == 409
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 0


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

BAD_INPUT = [
    ({"qty__print__16x21": "-3"}, "-3"),
    ({"qty__print__16x21": "1.5"}, "1.5"),
    ({"qty__print__16x21": "abc"}, "abc"),
    ({"qty__print__16x21": "999999"}, "999999"),
    ({"custom__print__0__size": "abc", "custom__print__0__qty": "2"}, "abc"),
    ({"custom__print__0__size": "", "custom__print__0__qty": "2"}, None),
    ({"custom__frame__0__size": "12.5x18", "custom__frame__0__qty": "1"}, "12.5x18"),
    ({"custom__print__0__size": "50x70", "custom__print__0__qty": "2", "custom__print__0__weight": "99"}, "99"),
    ({"pay__cash": "-5"}, "-5"),
    ({"pay__cash": "1.5"}, "1.5"),
    ({"pay__cash": "12abc"}, "12abc"),
    ({"pay__card_reader": str(10**12)}, str(10**12)),
    ({"venue_rent": "-1"}, "-1"),
    ({"extra__0__label": "", "extra__0__amount": "5000"}, None),
    ({"extra__0__label": "کاغذ", "extra__0__amount": "-5"}, "کاغذ"),
    ({"extra__0__label": "کاغذ", "extra__0__amount": "5", "extra__0__kind": "staff_pay"}, "کاغذ"),
    ({"notes": "ی" * 1001}, None),
]


@pytest.mark.parametrize("bad, typed", BAD_INPUT)
def test_invalid_input_is_a_422_that_keeps_what_was_typed(studio, bad, typed):
    data = vector_form()
    data.update(bad)
    response = studio.post(data)
    assert response.status_code == 422
    html = response.get_data(as_text=True)
    assert "فاکتور ذخیره نشد" in html and "field__error" in html
    assert 'data-invoice-form' in html
    if typed is not None:
        assert typed in html  # the typed value is shown again
    assert studio.invoice() is None
    db.session.refresh(studio.event)
    assert studio.event.status == EventStatus.CONFIRMED  # nothing happened


def test_validation_over_ajax_returns_fields_and_the_form(studio):
    data = vector_form()
    data["qty__print__16x21"] = "-3"
    response = studio.post(data, headers={"X-Requested-With": "XMLHttpRequest"})
    body = response.get_json()
    assert response.status_code == 422 and body["ok"] is False and body["code"] == "validation_error"
    assert "qty__print__16x21" in body["fields"]
    assert "data-invoice-form" in body["html"] and "-3" in body["html"]


def test_too_many_rows_are_refused(studio):
    data = vector_form()
    for i in range(16):
        data[f"custom__print__{i}__size"] = f"{20 + i}x{30 + i}"
        data[f"custom__print__{i}__qty"] = "1"
    assert studio.post(data).status_code == 422


def test_a_huge_rate_cannot_overflow_the_ledger(studio):
    studio.crew[0].commission_per_unit = 10**15
    db.session.commit()
    response = studio.post(form(qty={"print__16x21": 5_000}))
    assert response.status_code == 422 and "از حد مجاز" in response.get_data(as_text=True)
    assert studio.invoice() is None


# --------------------------------------------------------------------------
# rent, equipment, notes
# --------------------------------------------------------------------------


def test_rent_defaults_to_the_atelier_setting_and_can_be_overridden(studio):
    page = studio.client.get(studio.url).get_data(as_text=True)
    assert 'name="venue_rent"' in page and 'value="3000000"' in page
    assert 'data-restore-default="f-venue_rent" data-default="3000000" hidden' in page  # equal: no "restore" link
    studio.post(vector_form(venue=4_200_000))
    invoice = studio.invoice()
    rent = next(e for e in invoice.expenses if e.kind == ExpenseKind.VENUE_RENT)
    assert rent.amount == 4_200_000 and rent.is_auto is False
    assert invoice.expense_total == 4_200_000 + 4 * 1_870_000
    again = studio.client.get(studio.url).get_data(as_text=True)
    assert 'value="4200000"' in again  # the saved value wins over the default ...
    assert 'data-default="3000000">' in again and "بازگشت به مقدار پیش‌فرض" in again  # ... and the link restores it


def test_a_hall_specific_rent_beats_the_atelier_default(studio):
    db.session.add(AtelierHall(atelier=studio.atelier, hall=studio.hall, rent_amount=2_500_000))
    db.session.commit()
    studio.post(vector_form())  # venue_rent not posted at all: the suggestion is used
    rent = next(e for e in studio.invoice().expenses if e.kind == ExpenseKind.VENUE_RENT)
    assert rent.amount == 2_500_000 and rent.is_auto is True


def test_a_cleared_rent_field_means_no_rent(studio):
    studio.post(vector_form(venue=""))
    rent = next(e for e in studio.invoice().expenses if e.kind == ExpenseKind.VENUE_RENT)
    assert rent.amount == 0


def test_equipment_suggestions_extra_expenses_and_notes(studio):
    gear = Equipment(atelier=studio.atelier, name="فلاش اجاره‌ای", kind=EquipmentKind.FLASH,
                     ownership=EquipmentOwnership.RENTED, rent_cost_per_event=400_000)
    db.session.add(gear)
    db.session.commit()
    usage = staffing.attach_equipment(studio.event, gear, studio.manager)
    db.session.commit()
    page = studio.client.get(studio.url).get_data(as_text=True)
    assert f'name="equip__{usage.id}__amount"' in page and "فلاش اجاره‌ای" in page
    data = vector_form(**{
        f"equip__{usage.id}__amount": "350000",
        "extra__0__label": "کاغذ و جوهر", "extra__0__amount": "۹۰۰٬۰۰۰", "extra__0__kind": "material",
        "extra__3__label": "ایاب و ذهاب", "extra__3__amount": "120000",
        "notes": "  همه چیز خوب بود  ",
    })
    assert studio.post(data).status_code == 302
    invoice = studio.invoice()
    expenses = {(e.kind, e.label): e.amount for e in invoice.expenses if e.kind != ExpenseKind.STAFF_PAY}
    assert expenses[(ExpenseKind.EQUIPMENT_RENTAL, "اجاره: فلاش اجاره‌ای")] == 350_000
    assert expenses[(ExpenseKind.MATERIAL, "کاغذ و جوهر")] == 900_000
    assert expenses[(ExpenseKind.OTHER, "ایاب و ذهاب")] == 120_000
    assert invoice.notes == "همه چیز خوب بود"
    # edit: all three come back as typed
    again = studio.client.get(studio.url).get_data(as_text=True)
    assert 'value="کاغذ و جوهر"' in again and 'value="350000"' in again and "همه چیز خوب بود" in again


def test_an_equipment_id_of_another_event_is_ignored_not_trusted(studio, rival):
    gear = Equipment(atelier=rival.atelier, name="دوربین دیگری", kind=EquipmentKind.CAMERA,
                     ownership=EquipmentOwnership.RENTED, rent_cost_per_event=900_000)
    db.session.add(gear)
    db.session.commit()
    foreign = staffing.attach_equipment(rival.event, gear, rival.manager)
    db.session.commit()
    studio.post(vector_form(**{f"equip__{foreign.id}__amount": "1"}))
    assert not [e for e in studio.invoice().expenses if e.kind == ExpenseKind.EQUIPMENT_RENTAL]


# --------------------------------------------------------------------------
# preview endpoint
# --------------------------------------------------------------------------


def test_preview_is_computed_by_the_server_and_writes_nothing(studio):
    response = studio.client.post(f"{studio.url}/preview", data=vector_form())
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["units"] == 80
    assert data["income"] == {"card_reader": 0, "card_to_card": 0, "cash": 20_000_000, "total": 20_000_000}
    assert data["expenses"]["venue_rent"] == 3_000_000 and data["expenses"]["staff_pay"] == 4 * 1_870_000
    assert data["net_profit"] == 20_000_000 - 3_000_000 - 4 * 1_870_000
    assert [p["total"] for p in data["people"]] == [1_870_000] * 4
    assert data["people"][0]["applied_tier"] == {"threshold": 70, "amount": 150_000}
    assert [p["role"] for p in data["people"]] == list(CREW_ROLES)
    assert studio.invoice() is None
    db.session.refresh(studio.event)
    assert studio.event.status == EventStatus.CONFIRMED


def test_preview_equals_what_gets_saved(studio):
    payload = vector_form(venue=5_000_000, **{"custom__chassis__0__size": "60x90", "custom__chassis__0__qty": "4", "custom__chassis__0__weight": "2"})
    preview = studio.client.post(f"{studio.url}/preview", data=payload).get_json()["data"]
    studio.post(payload)
    invoice = studio.invoice()
    assert preview["units"] == invoice.units_total
    assert preview["net_profit"] == invoice.net_profit
    assert preview["expenses"]["total"] == invoice.expense_total
    assert sorted(p["total"] for p in preview["people"]) == sorted(l.total for l in invoice.payroll_lines)


def test_preview_accepts_json_and_reports_invalid_fields(studio):
    ok = studio.client.post(f"{studio.url}/preview", json={"qty__print__16x21": 70})
    assert ok.status_code == 200 and ok.get_json()["data"]["people"][0]["bonus"] == 150_000
    bad = studio.client.post(f"{studio.url}/preview", data=form(qty={"print__16x21": "x"}))
    assert bad.status_code == 422 and "qty__print__16x21" in bad.get_json()["fields"]


def test_preview_warns_about_paid_lines_that_would_change(studio):
    studio.post(vector_form())
    ledger.mark_paid(studio.invoice().payroll_lines[:1], studio.manager, paid_on=dt.date(2025, 7, 20), method="cash")
    db.session.commit()
    data = studio.client.post(f"{studio.url}/preview", data=form(qty={"print__16x21": 10}, revision=1)).get_json()["data"]
    assert any("پرداخت شده بود" in w for w in data["warnings"])
    assert [p["paid"] for p in data["people"]].count(True) == 1


# --------------------------------------------------------------------------
# lifecycle: reopen / void
# --------------------------------------------------------------------------


def test_reopen_unlocks_the_terms_and_a_new_save_locks_them_again(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    assert all(a.is_terms_locked for a in studio.crew)
    url = f"/accounting/invoices/{invoice.id}/reopen"
    assert studio.client.post(url, data={"reason": ""}).status_code == 302  # refused: a reason is required
    assert studio.invoice().status == InvoiceStatus.SUBMITTED
    assert studio.client.post(url, data={"reason": "اشتباه در پایهٔ حقوق"}).status_code == 302
    invoice = studio.invoice()
    db.session.refresh(studio.event)
    assert invoice.status == InvoiceStatus.DRAFT and studio.event.status == EventStatus.DONE
    assert not any(a.is_terms_locked for a in studio.crew)
    staffing.set_terms(studio.crew[0], {"base": 2_000_000, "commission_per_unit": 9_000, "bonus_tiers": []}, studio.manager)
    db.session.commit()
    assert studio.post(vector_form(revision=invoice.revision)).status_code == 302
    invoice = studio.invoice()
    assert invoice.status == InvoiceStatus.SUBMITTED and all(a.is_terms_locked for a in studio.crew)
    assert by_assignment(invoice)[studio.crew[0].id].total == 2_000_000 + 720_000
    reopened = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "invoice.reopened"))
    assert reopened.data["reason"] == "اشتباه در پایهٔ حقوق"


def test_void_needs_a_reason_and_blocks_further_edits(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    url = f"/accounting/invoices/{invoice.id}/void"
    studio.client.post(url, data={"reason": "  "})
    assert studio.invoice().status == InvoiceStatus.SUBMITTED
    assert studio.client.post(url, data={"reason": "مراسم برگزار نشد"}).status_code == 302
    assert studio.invoice().status == InvoiceStatus.VOID
    db.session.refresh(studio.event)
    assert studio.event.status == EventStatus.DONE
    assert studio.client.get(studio.url).status_code == 409
    assert studio.post(vector_form()).status_code == 409
    # a void invoice is out of payroll and the reports
    assert "حقوقی ثبت نشده" in studio.client.get("/accounting/payroll?y=1404&m=4").get_data(as_text=True)


def test_void_is_refused_while_payroll_is_paid(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    ledger.mark_paid(invoice.payroll_lines[:1], studio.manager, paid_on=dt.date(2025, 7, 20), method="cash")
    db.session.commit()
    studio.client.post(f"/accounting/invoices/{invoice.id}/void", data={"reason": "اشتباه"})
    assert studio.invoice().status == InvoiceStatus.SUBMITTED
    assert any("پرداخت‌شده" in m for m in flashes(studio.client.get(INDEX).get_data(as_text=True)))


# --------------------------------------------------------------------------
# CSV and the printable sheet
# --------------------------------------------------------------------------


def test_invoice_csv_is_excel_safe_and_complete(studio, make_user):
    hostile = make_user(Role.STAFF, full_name="=HYPERLINK(\"http://evil\",\"x\")")
    from app.models import Assignment as A  # a fifth person whose name looks like a formula

    db.session.add(A(event=studio.event, role=studio.crew[1].role, user=hostile, status=studio.crew[1].status,
                     base_pay=0, commission_per_unit=0, bonus_tiers=[]))
    db.session.commit()
    studio.post(vector_form(notes="+SUM(1;1)"))
    invoice = studio.invoice()
    response = studio.client.get(f"/accounting/invoices/{invoice.id}/export.csv")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv") and "attachment" in response.headers["Content-Disposition"]
    rows = csv_rows(response)
    text = response.get_data(as_text=True)
    assert not re.search("[۰-۹٠-٩]", text), "numbers and dates must be ASCII digits"
    flat = {tuple(r) for r in rows if r}
    assert ("تاریخ مراسم (میلادی)", "2025-07-17") in flat
    assert ("تاریخ مراسم (شمسی)", "1404/04/26") in flat
    assert ("جمع واحدها", "", "", "", "", "80") in flat
    assert ("چاپ", "16x21", "50", "1", "بله", "50") in flat
    assert ("نقد", "20000000") in flat and ("جمع دریافتی", "20000000") in flat
    assert ("سود خالص (تومان)", str(invoice.net_profit)) in flat
    payroll = [r for r in rows if len(r) == 10 and r[6] == "1870000"]
    assert len(payroll) == 4 and payroll[0][3:7] == ["1000000", "720000", "150000", "1870000"]
    assert ("یادداشت", "'+SUM(1;1)") in flat  # a leading + is neutralised
    assert any(r and r[0].startswith("'=HYPERLINK") for r in rows)  # so is a name that starts with =


def test_the_printable_sheet_has_the_amounts_in_words(studio):
    studio.post(vector_form())
    invoice = studio.invoice()
    html = studio.client.get(f"/accounting/invoices/{invoice.id}/print").get_data(as_text=True)
    assert "بیست میلیون تومان" in html and "فاکتور مراسم" in html and "data-print" in html
    assert "امضای مدیر اتلیه" in html


# --------------------------------------------------------------------------
# security
# --------------------------------------------------------------------------


def test_other_ateliers_invoices_are_invisible(studio, rival):
    foreign_url = f"/accounting/events/{rival.event.id}/invoice"
    assert studio.client.get(foreign_url).status_code == 404
    assert studio.client.get(foreign_url + "?fragment=1").status_code == 404
    assert studio.client.post(foreign_url, data=vector_form()).status_code == 404
    assert studio.client.post(foreign_url + "/preview", data=vector_form()).status_code == 404
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 0
    # a real foreign invoice: create it as its owner, then probe every route as the studio manager
    from app.services.testing import login_client

    owner = studio.client.application.test_client()
    login_client(owner, rival.atelier.manager)
    assert owner.post(foreign_url, data=vector_form()).status_code == 302
    foreign = db.session.scalar(sa.select(Invoice).where(Invoice.event_id == rival.event.id))
    for path in ("print", "export.csv"):
        assert studio.client.get(f"/accounting/invoices/{foreign.id}/{path}").status_code == 404
    for action in ("reopen", "void"):
        assert studio.client.post(f"/accounting/invoices/{foreign.id}/{action}", data={"reason": "xxx"}).status_code == 404
    db.session.expire_all()
    assert db.session.get(Invoice, foreign.id).status == InvoiceStatus.SUBMITTED
    assert owner.get(f"/accounting/invoices/{foreign.id}/print").status_code == 200  # the owner still can


def test_ids_in_the_form_body_are_never_trusted(studio, rival):
    """The event is chosen by the URL (and checked); nothing in the body can redirect the write."""
    data = vector_form(event_id=rival.event.id, atelier_id=rival.atelier.id, invoice_id=999)
    assert studio.post(data).status_code == 302
    assert studio.invoice() is not None
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id)).where(Invoice.event_id == rival.event.id)) == 0


@pytest.mark.parametrize("role", [Role.STAFF, Role.HALL_MANAGER, Role.ADMIN])
def test_other_roles_get_403_on_every_route(studio, make_user, client_for, role):
    other = client_for(make_user(role))
    invoice_id = 1
    for method, path in (
        ("get", "/accounting/"),
        ("get", studio.url),
        ("post", studio.url),
        ("post", studio.url + "/preview"),
        ("get", f"/accounting/invoices/{invoice_id}/print"),
        ("get", f"/accounting/invoices/{invoice_id}/export.csv"),
        ("post", f"/accounting/invoices/{invoice_id}/reopen"),
        ("post", f"/accounting/invoices/{invoice_id}/void"),
        ("get", "/accounting/payroll"),
        ("post", "/accounting/payroll/pay"),
        ("post", "/accounting/payroll/unpay"),
        ("get", "/accounting/payroll/export.csv"),
        ("get", "/accounting/reports"),
        ("get", "/accounting/reports/export.csv"),
    ):
        response = getattr(other, method)(path, data={})
        assert response.status_code == 403, (role, method, path, response.status_code)


def test_anonymous_visitors_are_sent_to_the_login_page(client, studio):
    anonymous = studio.client.application.test_client()
    for path in ("/accounting/", studio.url, "/accounting/payroll", "/accounting/reports"):
        response = anonymous.get(path)
        assert response.status_code == 302 and "/auth/login" in response.headers["Location"]
    assert anonymous.post(studio.url, data=vector_form()).status_code == 302
    ajax = anonymous.post(studio.url + "/preview", data={}, headers={"X-Requested-With": "XMLHttpRequest"})
    assert ajax.status_code == 401


def test_csrf_is_enforced_on_every_state_change(csrf_client, studio, login_as, get_csrf):
    login_as(csrf_client, studio.manager)
    assert csrf_client.post(studio.url, data=vector_form()).status_code == 400
    assert csrf_client.post(studio.url + "/preview", data=vector_form()).status_code == 400
    assert csrf_client.post("/accounting/payroll/pay", data={"line_id": "1"}).status_code == 400
    assert studio.invoice() is None
    token = get_csrf(csrf_client, "/accounting/")
    data = vector_form()
    data["csrf_token"] = token
    assert csrf_client.post(studio.url, data=data).status_code == 302
    assert studio.invoice() is not None
    # the XHR way: the token travels in a header
    again = form(qty={"print__16x21": 5}, revision=1)
    response = csrf_client.post(studio.url, data=again, headers={"X-CSRFToken": token, "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200 and response.get_json()["ok"] is True


def test_the_popup_form_carries_a_csrf_token_and_a_one_time_token(studio):
    html = studio.client.get(studio.url + "?fragment=1").get_data(as_text=True)
    assert 'name="csrf_token"' in html
    token = re.search(r'name="save_token" value="([A-Za-z0-9_-]{8,64})"', html)
    assert token and 'name="base_revision" value="0"' in html
    other = studio.client.get(studio.url + "?fragment=1").get_data(as_text=True)
    assert token.group(1) not in other  # a fresh token for every render


def test_the_popup_has_exactly_the_agents_md_fields_in_order(studio):
    html = studio.client.get(studio.url + "?fragment=1").get_data(as_text=True)
    order = [
        "qty__print__16x21", "qty__print__20x30", "qty__print__30x40", "data-add-custom=\"print\"",
        "qty__raw_file", "qty__edited_file",
        "qty__frame__16x21", "qty__frame__20x30", "qty__frame__30x40", "data-add-custom=\"frame\"",
        "qty__chassis__16x21", "qty__chassis__20x30", "qty__chassis__30x40", "data-add-custom=\"chassis\"",
        "pay__card_reader", "pay__card_to_card", "pay__cash", "venue_rent",
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions), "fields must follow the AGENTS.md order"
    for text in ("چاپ ابعاد دیگر", "قاب ابعاد دیگر", "شاسی ابعاد دیگر", "جمع کارتخوان", "جمع کارت به کارت", "جمع نقد",
                 "اجارهٔ باغ/تالار", "هزینهٔ نیروها", "هزینه‌های دیگر", "خلاصهٔ حساب", "حقوق هر نیرو"):
        assert text in html, text
    # an "other size" row: size text, sold count and the weight box with its +/- stepper (default 1)
    template = html[html.index("data-custom-template=\"print\""):]
    template = template[: template.index("</template>")]
    assert "تعداد محاسبه برای حقوق نیرو" in template and template.count("data-stepper") >= 2
    assert re.search(r'name="custom__print____IDX____weight"[^>]*', template) or "custom__print____IDX____weight" in template
    assert 'value="1"' in template[template.index("____weight") - 200 : template.index("____weight") + 200] or 'value="1"' in template
