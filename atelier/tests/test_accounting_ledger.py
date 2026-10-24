"""Tests for app.services.ledger: invoice arithmetic, suggestions and persistence.

The AGENTS.md vector (base 1,000,000 + 9,000 per photo + 150,000 from 70 photos; 80 photos =>
1,870,000 for everybody) is exercised through the pure functions here and through the HTTP layer in
test_accounting_invoice.py.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import (
    AtelierHall,
    AuditLog,
    Equipment,
    EquipmentUsage,
    ExpenseLine,
    Invoice,
    Notification,
    PayrollLine,
)
from app.services import ledger, payroll, staffing
from app.services.constants import (
    AssignmentStatus,
    EquipmentKind,
    EquipmentOwnership,
    EventStatus,
    ExpenseKind,
    InvoiceStatus,
    PaymentMethod,
    PayoutMethod,
    ProductCategory,
    Role,
    Skill,
)

VECTOR = payroll.PayTerms(1_000_000, 9_000, (payroll.BonusTier(70, 150_000),))


def person(key=1, name="علی", role=Skill.PHOTOGRAPHER, terms=VECTOR, user_id=None):
    return ledger.PersonSpec(key, name, role, terms, user_id)


def rows(print_1621=0, print_2030=0, raw=0):
    return [
        {"category": "print", "size": "16x21", "qty": print_1621},
        {"category": "print", "size": "20x30", "qty": print_2030},
        {"category": "raw_file", "qty": raw},
    ]


# --------------------------------------------------------------------------
# build_lines
# --------------------------------------------------------------------------


class TestBuildLines:
    def test_sizes_are_normalised_and_zero_rows_dropped(self):
        built = ledger.build_lines(
            [
                {"category": "print", "size": "۲۱ × ۱۶", "qty": "۳"},
                {"category": "print", "size": "20x30", "qty": 0},
                {"category": "frame", "size": "70*50", "qty": 2, "custom": True, "weight": 3},
            ]
        )
        assert [(l.category, l.size_label, l.qty, l.weight, l.is_custom) for l in built] == [
            (ProductCategory.PRINT, "16x21", 3, 1, False),
            (ProductCategory.FRAME, "50x70", 2, 3, True),
        ]

    def test_order_follows_the_popup(self):
        built = ledger.build_lines(
            [
                {"category": "chassis", "size": "30x40", "qty": 1},
                {"category": "print", "size": "50x70", "qty": 1, "custom": True},
                {"category": "print", "size": "30x40", "qty": 1},
                {"category": "print", "size": "16x21", "qty": 1},
                {"category": "edited_file", "qty": 1},
                {"category": "raw_file", "qty": 1},
                {"category": "frame", "size": "20x30", "qty": 1},
            ]
        )
        assert [(l.category.value, l.size_label) for l in built] == [
            ("print", "16x21"),
            ("print", "30x40"),
            ("print", "50x70"),
            ("raw_file", ""),
            ("edited_file", ""),
            ("frame", "20x30"),
            ("chassis", "30x40"),
        ]

    def test_files_have_no_size_and_no_custom_rows(self):
        assert ledger.build_lines([{"category": "raw_file", "size": "16x21", "qty": 2}])[0].size_label == ""
        with pytest.raises(ledger.LedgerError):
            ledger.build_lines([{"category": "raw_file", "qty": 2, "custom": True}])

    @pytest.mark.parametrize(
        "row",
        [
            {"category": "print", "qty": 1},  # size missing
            {"category": "print", "size": "abc", "qty": 1},
            {"category": "print", "size": "12.5x18", "qty": 1},  # decimal sides cannot be stored
            {"category": "print", "size": "16x21", "qty": -1},
            {"category": "print", "size": "16x21", "qty": 1.5},
            {"category": "print", "size": "16x21", "qty": True},
            {"category": "print", "size": "16x21", "qty": ledger.MAX_QTY + 1},
            {"category": "print", "size": "16x21", "qty": 1, "weight": ledger.MAX_WEIGHT + 1},
            {"category": "nope", "qty": 1},
        ],
    )
    def test_invalid_rows_raise_ledger_error(self, row):
        with pytest.raises(ledger.LedgerError):
            ledger.build_lines([row])

    def test_accepts_line_specs_and_counts_flags(self):
        spec = ledger.LineSpec(ProductCategory.PRINT, "16x21", 4, 2, False)
        again = ledger.build_lines([spec])
        assert again == [spec]
        assert spec.units == 0 and ledger.LineSpec(ProductCategory.PRINT, "16x21", 4, 2).units == 8


# --------------------------------------------------------------------------
# compute_invoice (pure)
# --------------------------------------------------------------------------


class TestComputeInvoice:
    def test_agents_md_vector_for_four_people(self):
        people = [person(i, f"نفر {i}", role) for i, role in enumerate(Skill, start=1)]
        comp = ledger.compute_invoice(
            ledger.build_lines(rows(print_1621=50, print_2030=20, raw=10)),
            {"card_reader": 10_000_000, "card_to_card": 5_000_000, "cash": 3_000_000},
            [ledger.ExpenseSpec(ExpenseKind.VENUE_RENT, "اجاره", 2_000_000)],
            people,
        )
        assert comp.units == 80
        assert [p.pay.total for p in comp.people] == [1_870_000] * 4
        assert comp.staff_total == 7_480_000
        assert comp.income == 18_000_000
        assert comp.expense_total == 9_480_000
        assert comp.net_profit == 8_520_000
        assert comp.expense_by_kind[ExpenseKind.VENUE_RENT] == 2_000_000

    @pytest.mark.parametrize(
        "units, bonus, total",
        [(0, 0, 1_000_000), (69, 0, 1_621_000), (70, 150_000, 1_780_000), (71, 150_000, 1_789_000)],
    )
    def test_bonus_boundary(self, units, bonus, total):
        comp = ledger.compute_invoice(ledger.build_lines(rows(print_1621=units)), None, (), [person()])
        pay = comp.people[0].pay
        assert (pay.bonus, pay.total) == (bonus, total)

    def test_everybody_is_paid_with_their_own_terms(self):
        rich = payroll.PayTerms(2_000_000, 12_000, (payroll.BonusTier(50, 100_000), payroll.BonusTier(90, 400_000)))
        flat = payroll.PayTerms(500_000, 0)
        comp = ledger.compute_invoice(
            ledger.build_lines(rows(print_1621=80)), None, (), [person(1, "الف"), person(2, "ب", terms=rich), person(3, "ج", terms=flat)]
        )
        totals = [p.pay.total for p in comp.people]
        assert totals == [1_870_000, 2_000_000 + 80 * 12_000 + 100_000, 500_000]
        assert comp.people[1].pay.applied_tier == payroll.BonusTier(50, 100_000)  # highest reached, not summed

    def test_weights_and_opt_out_follow_payroll_units(self):
        lines = ledger.build_lines(
            [
                {"category": "print", "size": "16x21", "qty": 10},
                {"category": "frame", "size": "50x70", "qty": 5, "custom": True, "weight": 3},
                {"category": "chassis", "size": "20x30", "qty": 100, "counts": False},
            ]
        )
        comp = ledger.compute_invoice(lines, None, (), [person()])
        assert comp.units == 10 + 15
        assert comp.units == payroll.units_from_lines(lines)

    def test_net_profit_can_be_negative(self):
        comp = ledger.compute_invoice([], {"cash": 100}, [ledger.ExpenseSpec(ExpenseKind.OTHER, "x", 500)], [])
        assert comp.net_profit == -400

    def test_payments_default_to_zero_and_reject_junk(self):
        comp = ledger.compute_invoice([], {"cash": "۱٬۲۰۰"}, (), [])
        assert comp.payments[PaymentMethod.CASH] == 1200 and comp.payments[PaymentMethod.CARD_READER] == 0
        for bad in ({"cash": -1}, {"cash": 1.5}, {"cash": ledger.MAX_AMOUNT + 1}, {"bitcoin": 1}):
            with pytest.raises(ledger.LedgerError):
                ledger.compute_invoice([], bad, (), [])

    def test_staff_pay_cannot_be_typed_by_hand(self):
        with pytest.raises(ledger.LedgerError):
            ledger.compute_invoice([], None, [ledger.ExpenseSpec(ExpenseKind.STAFF_PAY, "x", 1)], [])

    def test_overflow_is_a_validation_error_not_a_crash(self):
        huge = payroll.PayTerms(0, 10**15)
        lines = ledger.build_lines(rows(print_1621=ledger.MAX_QTY))
        with pytest.raises(ledger.LedgerError) as caught:
            ledger.compute_invoice(lines, None, (), [person(terms=huge)])
        assert caught.value.code == "overflow"

    def test_everything_is_an_int_and_serialisable(self):
        comp = ledger.compute_invoice(ledger.build_lines(rows(print_1621=80)), {"cash": 5}, (), [person()])
        data = comp.as_dict()

        def walk(value):
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v)
            else:
                assert not isinstance(value, float)

        walk(data)
        assert data["people"][0]["total"] == 1_870_000
        assert data["people"][0]["applied_tier"] == {"threshold": 70, "amount": 150_000}
        assert data["income"]["total"] == 5


# --------------------------------------------------------------------------
# database: who is paid, suggestions
# --------------------------------------------------------------------------


@pytest.fixture
def setup(make_atelier, make_event, make_hall):
    atelier = make_atelier(default_venue_rent=3_000_000)
    hall = make_hall()
    event = make_event(hall=hall, atelier=atelier, status="confirmed")
    return atelier, hall, event


def test_payable_assignments_only_committed_people_in_workflow_order(setup, make_assignment):
    atelier, hall, event = setup
    editor = make_assignment(event, "editor")
    presenter = make_assignment(event, "presenter", status="accepted")
    make_assignment(event, "seller", status="invited")
    make_assignment(event, "seller", status="declined")
    make_assignment(event, "photographer", status="cancelled")
    photographer = make_assignment(event, "photographer", status="completed")
    got = ledger.payable_assignments(event)
    assert [a.id for a in got] == [presenter.id, photographer.id, editor.id]


def test_suggest_venue_rent_prefers_the_hall_specific_price(setup):
    atelier, hall, event = setup
    assert ledger.suggest_expenses(event).venue_rent == 3_000_000  # atelier default
    db.session.add(AtelierHall(atelier=atelier, hall=hall, rent_amount=2_500_000))
    db.session.commit()
    assert ledger.suggest_expenses(event).venue_rent == 2_500_000
    link = db.session.scalar(sa.select(AtelierHall))
    link.rent_amount = None  # NULL means "no special price"
    db.session.commit()
    assert ledger.suggest_expenses(event).venue_rent == 3_000_000


def test_suggest_equipment_only_for_rented_gear(setup):
    atelier, hall, event = setup
    rented = Equipment(atelier=atelier, name="دوربین اجاره‌ای", kind=EquipmentKind.CAMERA,
                       ownership=EquipmentOwnership.RENTED, rent_cost_per_event=700_000)
    owned = Equipment(atelier=atelier, name="لپ‌تاپ خودی", kind=EquipmentKind.LAPTOP_EDIT)
    db.session.add_all([rented, owned])
    db.session.commit()
    staffing.attach_equipment(event, rented, atelier.manager)
    staffing.attach_equipment(event, owned, atelier.manager)
    db.session.commit()
    suggestion = ledger.suggest_expenses(event)
    assert [(s.label, s.amount) for s in suggestion.equipment] == [("دوربین اجاره‌ای", 700_000)]


# --------------------------------------------------------------------------
# save_invoice
# --------------------------------------------------------------------------


def data(units=80, cash=20_000_000, **kw):
    return ledger.InvoiceData(
        lines=[{"category": "print", "size": "16x21", "qty": units}],
        payments={"cash": cash},
        **kw,
    )


@pytest.fixture
def staffed(setup, make_assignment):
    atelier, hall, event = setup
    people = [make_assignment(event, role) for role in ("presenter", "photographer", "seller", "editor")]
    return atelier, hall, event, people


def test_save_creates_everything_in_one_go(staffed, bus):
    atelier, hall, event, people = staffed
    manager = atelier.manager
    result = ledger.save_invoice(event, data(), manager, nonce="n-first-save-1")
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    assert result.created and invoice.status == InvoiceStatus.SUBMITTED and invoice.revision == 1
    assert invoice.units_total == 80
    assert invoice.income_total == 20_000_000
    assert invoice.expense_total == 3_000_000 + 4 * 1_870_000
    assert invoice.net_profit == 20_000_000 - invoice.expense_total
    assert [(p.units, p.base, p.commission, p.bonus, p.total) for p in invoice.payroll_lines] == [
        (80, 1_000_000, 720_000, 150_000, 1_870_000)
    ] * 4
    assert invoice.payroll_lines[0].applied_tier == {"threshold": 70, "amount": 150_000}
    kinds = sorted((e.kind.value, e.amount) for e in invoice.expenses)
    assert kinds == sorted([("venue_rent", 3_000_000)] + [("staff_pay", 1_870_000)] * 4)
    staff_expense = [e for e in invoice.expenses if e.kind == ExpenseKind.STAFF_PAY]
    assert all(e.is_auto and e.assignment_id for e in staff_expense)
    assert {p.method.value: p.amount for p in invoice.payments} == {"card_reader": 0, "card_to_card": 0, "cash": 20_000_000}
    # the event is invoiced and the terms are frozen
    assert event.status == EventStatus.INVOICED
    assert all(a.is_terms_locked and a.status == AssignmentStatus.COMPLETED for a in people)
    # audit trail
    entry = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "invoice.submitted"))
    assert entry.entity_id == invoice.id and entry.data["after"]["net"] == invoice.net_profit
    assert entry.data["nonce"] == "n-first-save-1"


def test_staff_with_accounts_are_told_and_roster_members_are_not(setup, make_assignment, make_user):
    atelier, hall, event = setup
    user = make_user(Role.STAFF)
    make_assignment(event, "photographer", user=user)
    # a roster member without an account
    from app.models import RosterMember

    member = RosterMember(atelier=atelier, display_name="نیروی ثابت", skills=["seller"])
    db.session.add(member)
    db.session.commit()
    make_assignment(event, "seller", roster_member=member)
    result = ledger.save_invoice(event, data(), atelier.manager)
    db.session.commit()
    notes = db.session.scalars(sa.select(Notification).where(Notification.user_id == user.id)).all()
    mine = [n for n in notes if n.title == "حقوق شما ثبت شد"]
    assert len(mine) == 1 and "۱٬۸۷۰٬۰۰۰" in mine[0].body
    assert result.notified == 1
    # an unchanged re-save does not repeat the news
    ledger.save_invoice(event, data(), atelier.manager)
    db.session.commit()
    assert len([n for n in db.session.scalars(sa.select(Notification).where(Notification.user_id == user.id)) if n.title.startswith("حقوق")]) == 1


def test_resave_recomputes_in_place_and_bumps_the_revision(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(units=80), atelier.manager)
    db.session.commit()
    ids = sorted(db.session.scalars(sa.select(PayrollLine.id)))
    result = ledger.save_invoice(event, data(units=60, cash=1_000_000), atelier.manager, expected_revision=1)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    assert invoice.revision == 2 and not result.created
    assert sorted(db.session.scalars(sa.select(PayrollLine.id))) == ids  # same rows, updated
    assert {l.total for l in invoice.payroll_lines} == {1_000_000 + 60 * 9_000}  # below the 70 tier
    assert invoice.units_total == 60 and invoice.income_total == 1_000_000
    assert db.session.scalar(sa.select(sa.func.count(ExpenseLine.id))) == 5  # no duplicates
    assert [l.qty for l in invoice.lines] == [60]
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "invoice.updated")) is not None


def test_paid_lines_keep_their_marker_and_are_flagged(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(units=80), atelier.manager)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    first = invoice.payroll_lines[0]
    ledger.mark_paid([first], atelier.manager, paid_on=dt.date(2025, 7, 20), method="cash", note="دستی")
    db.session.commit()
    result = ledger.save_invoice(event, data(units=60), atelier.manager)
    db.session.commit()
    line = db.session.get(PayrollLine, first.id)
    assert line.is_paid and line.paid_method == PayoutMethod.CASH and line.paid_note == "دستی"
    assert line.total == 1_540_000  # the formula result; the payment record is untouched
    assert len(result.warnings) == 1 and "۱٬۸۷۰٬۰۰۰" in result.warnings[0] and "۱٬۵۴۰٬۰۰۰" in result.warnings[0]
    assert ledger.paid_amounts([line.id]) == {line.id: 1_870_000}  # mismatch is detectable later
    recalculated = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "payroll.recalculated"))
    assert recalculated.data["old_total"] == 1_870_000 and recalculated.data["new_total"] == 1_540_000


def test_stale_revision_and_double_submit_are_told_apart(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(), atelier.manager, expected_revision=0, nonce="submit-token-aaaa")
    db.session.commit()
    with pytest.raises(ledger.StaleInvoice) as replay:  # the same form posted twice
        ledger.save_invoice(event, data(), atelier.manager, expected_revision=0, nonce="submit-token-aaaa")
    assert replay.value.replay is True and replay.value.current_revision == 1
    with pytest.raises(ledger.StaleInvoice) as stale:  # another tab with an older form
        ledger.save_invoice(event, data(), atelier.manager, expected_revision=0, nonce="submit-token-bbbb")
    assert stale.value.replay is False
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 1


@pytest.mark.parametrize("status", ["draft", "open", "offered", "cancelled"])
def test_events_that_cannot_be_invoiced(make_atelier, make_event, status):
    atelier = make_atelier()
    event = make_event(atelier=atelier, status=status)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger.save_invoice(event, data(), atelier.manager)
    assert caught.value.code == "event_state"
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 0


def test_invalid_data_changes_nothing(staffed):
    atelier, hall, event, people = staffed
    with pytest.raises(ledger.LedgerError):
        ledger.save_invoice(
            event, ledger.InvoiceData(lines=[{"category": "print", "size": "x", "qty": 1}], payments={}), atelier.manager
        )
    db.session.rollback()
    assert event.status == EventStatus.CONFIRMED
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 0


def test_venue_rent_override_and_equipment_lines(staffed):
    atelier, hall, event, people = staffed
    rented = Equipment(atelier=atelier, name="فلاش", kind=EquipmentKind.FLASH,
                       ownership=EquipmentOwnership.RENTED, rent_cost_per_event=400_000)
    db.session.add(rented)
    db.session.commit()
    usage = EquipmentUsage.snapshot(event, rented)
    db.session.add(usage)
    db.session.commit()
    ledger.save_invoice(
        event,
        data(venue_rent=4_200_000, equipment={usage.id: 350_000},
             extras=[ledger.ExpenseSpec(ExpenseKind.MATERIAL, "کاغذ و جوهر", 900_000)]),
        atelier.manager,
    )
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    by_kind = {e.kind: e for e in invoice.expenses if e.kind != ExpenseKind.STAFF_PAY}
    assert by_kind[ExpenseKind.VENUE_RENT].amount == 4_200_000 and not by_kind[ExpenseKind.VENUE_RENT].is_auto
    assert by_kind[ExpenseKind.EQUIPMENT_RENTAL].amount == 350_000
    assert by_kind[ExpenseKind.EQUIPMENT_RENTAL].equipment_usage_id == usage.id
    assert by_kind[ExpenseKind.MATERIAL].label == "کاغذ و جوهر"
    # an unknown usage id (another event's) is refused
    with pytest.raises(ledger.LedgerError):
        ledger.evaluate(event, data(equipment={usage.id + 999: 1}))


def test_recompute_follows_changed_terms_after_unlock(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(units=80), atelier.manager)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    ledger.reopen_invoice(invoice, atelier.manager, "اصلاح پایه حقوق")
    target = people[1]
    staffing.set_terms(target, {"base": 2_000_000, "commission_per_unit": 9_000, "bonus_tiers": []}, atelier.manager)
    result = ledger.recompute(invoice)
    db.session.commit()
    totals = {l.assignment_id: l.total for l in invoice.payroll_lines}
    assert totals[target.id] == 2_000_000 + 720_000
    assert totals[people[0].id] == 1_870_000
    assert invoice.expense_total == 3_000_000 + 3 * 1_870_000 + 2_720_000
    assert [c.new_total for c in result.changes if c.assignment_id == target.id] == [2_720_000]


def test_reopen_then_resubmit_and_void(staffed):
    atelier, hall, event, people = staffed
    manager = atelier.manager
    ledger.save_invoice(event, data(), manager)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    with pytest.raises(ledger.LedgerError):
        ledger.reopen_invoice(invoice, manager, "")
    ledger.reopen_invoice(invoice, manager, "اشتباه در ثبت")
    db.session.commit()
    assert invoice.status == InvoiceStatus.DRAFT and event.status == EventStatus.DONE
    assert not any(a.is_terms_locked for a in people)
    again = ledger.save_invoice(event, data(units=75), manager, expected_revision=invoice.revision)
    db.session.commit()
    assert again.resubmitted and invoice.status == InvoiceStatus.SUBMITTED and event.status == EventStatus.INVOICED
    assert all(a.is_terms_locked for a in people)
    ledger.void_invoice(invoice, manager, "مراسم برگزار نشد")
    db.session.commit()
    assert invoice.status == InvoiceStatus.VOID and event.status == EventStatus.DONE
    with pytest.raises(ledger.LedgerError):
        ledger.save_invoice(event, data(), manager)
    actions = [a.action for a in db.session.scalars(sa.select(AuditLog).order_by(AuditLog.id)) if a.action.startswith("invoice.")]
    assert actions == ["invoice.submitted", "invoice.reopened", "invoice.submitted", "invoice.voided"]


def test_cannot_void_while_payroll_is_paid(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(), atelier.manager)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    ledger.mark_paid(invoice.payroll_lines[:1], atelier.manager, paid_on=dt.date(2025, 7, 20), method="cash")
    with pytest.raises(ledger.LedgerError) as caught:
        ledger.void_invoice(invoice, atelier.manager, "اشتباه")
    assert caught.value.code == "has_payments"


# --------------------------------------------------------------------------
# paying staff
# --------------------------------------------------------------------------


def test_mark_paid_settles_the_invoice_and_undo_reopens_it(staffed):
    atelier, hall, event, people = staffed
    manager = atelier.manager
    ledger.save_invoice(event, data(), manager)
    db.session.commit()
    invoice = db.session.scalar(sa.select(Invoice))
    lines = list(invoice.payroll_lines)
    first = ledger.mark_paid(lines[:3], manager, paid_on=dt.date(2025, 7, 20), method="card_to_card", note="  ۱۲۳۴ ")
    assert len(first.paid) == 3 and first.total == 3 * 1_870_000 and invoice.status == InvoiceStatus.SUBMITTED
    assert lines[0].paid_note == "۱۲۳۴"
    second = ledger.mark_paid(lines, manager, paid_on=dt.date(2025, 7, 21), method="cash")
    assert len(second.paid) == 1 and len(second.skipped) == 3  # already-paid lines are left alone
    assert lines[0].paid_method == PayoutMethod.CARD_TO_CARD
    assert invoice.status == InvoiceStatus.SETTLED
    ledger.unmark_paid(lines[3], manager)
    assert invoice.status == InvoiceStatus.SUBMITTED and not lines[3].is_paid
    with pytest.raises(ledger.LedgerError):
        ledger.unmark_paid(lines[3], manager)
    actions = [a.action for a in db.session.scalars(sa.select(AuditLog).order_by(AuditLog.id)) if a.action.startswith("payroll.")]
    assert actions.count("payroll.paid") == 4 and actions.count("payroll.unpaid") == 1


def test_mark_paid_validates_input(staffed):
    atelier, hall, event, people = staffed
    ledger.save_invoice(event, data(), atelier.manager)
    db.session.commit()
    line = db.session.scalar(sa.select(PayrollLine))
    with pytest.raises(ledger.LedgerError):
        ledger.mark_paid([line], atelier.manager, paid_on=dt.date(2025, 7, 20), method="bitcoin")
    assert not line.is_paid


def test_paid_at_keeps_the_iran_calendar_day():
    from app.services import jalali

    stamp = ledger.paid_at_from_date(dt.date(2025, 7, 17))
    assert jalali.to_jalali_datetime_str(stamp, "numeric", seconds=False).startswith("1404/04/26")


def test_paid_staff_notification_goes_to_the_account(setup, make_assignment, make_user):
    atelier, hall, event = setup
    user = make_user(Role.STAFF)
    make_assignment(event, "photographer", user=user)
    ledger.save_invoice(event, data(), atelier.manager)
    db.session.commit()
    line = db.session.scalar(sa.select(PayrollLine))
    ledger.mark_paid([line], atelier.manager, paid_on=dt.date(2025, 7, 20), method="cash")
    db.session.commit()
    titles = [n.title for n in db.session.scalars(sa.select(Notification).where(Notification.user_id == user.id))]
    assert "حقوق شما پرداخت شد" in titles
