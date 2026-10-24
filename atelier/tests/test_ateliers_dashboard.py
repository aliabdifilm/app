"""Dashboard: alerts per missing role, hall offers, month KPIs from invoice totals, notifications, query budget."""

from __future__ import annotations

from contextlib import contextmanager

import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, body, day, mc, now, world  # noqa: F401

from app.extensions import db
from app.models import EventOffer, Invoice
from app.services import staffing
from app.services.constants import EventStatus, InvoiceStatus, OfferMode, Role, Skill


@contextmanager
def count_queries():
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", before)


def invoice(event, atelier, status, income, expense, units=0):
    row = Invoice(
        event=event,
        atelier=atelier,
        status=status,
        income_total=income,
        expense_total=expense,
        net_profit=income - expense,
        units_total=units,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_dashboard_for_an_empty_atelier_explains_the_next_step(app, make_user, make_atelier, client_for):
    manager = make_user(Role.ATELIER_MANAGER)
    make_atelier(manager, name="اتلیه خالی")
    page = body(client_for(manager).get("/atelier/"))
    assert "اتلیه خالی" in page and "مراسم پیش‌رویی ندارید" in page and "پیشنهاد بازی ندارید" in page
    assert "فاکتور ثبت‌شده‌ای نیست" in page


def test_month_kpis_come_from_submitted_and_settled_invoices_of_this_jalali_month(world, mc, make_event):
    # "today" is 19 Tir 1404; Tir = 2025-06-22 .. 2025-07-22.  world.event (day 7) is in Tir, world.later (day 20) in Mordad.
    invoice(world.event, world.atelier, InvoiceStatus.SUBMITTED, income=10_000_000, expense=6_000_000, units=80)
    second = make_event(world.hall, world.atelier, date=day(2))
    invoice(second, world.atelier, InvoiceStatus.SETTLED, income=4_000_000, expense=1_000_000, units=20)
    draft = make_event(world.hall, world.atelier, date=day(3))
    invoice(draft, world.atelier, InvoiceStatus.DRAFT, income=99_000_000, expense=0)
    void = make_event(world.hall, world.atelier, date=day(4))
    invoice(void, world.atelier, InvoiceStatus.VOID, income=77_000_000, expense=0)
    invoice(world.later, world.atelier, InvoiceStatus.SUBMITTED, income=55_000_000, expense=0)  # next month
    invoice(world.rival_event, world.rival, InvoiceStatus.SUBMITTED, income=33_000_000, expense=0)  # not mine

    page = body(mc.get("/atelier/"))
    assert "تیر ۱۴۰۴" in page
    assert "۱۴٬۰۰۰٬۰۰۰" in page  # income 10M + 4M
    assert "۷٬۰۰۰٬۰۰۰" in page  # expenses 6M + 1M
    assert "۷٬۰۰۰٬۰۰۰" in page and "۱۰۰ واحد فروش" in page  # profit 7M, units 80 + 20
    assert "از ۲ فاکتور ثبت‌شده" in page
    for leaked in ("۹۹٬۰۰۰٬۰۰۰", "۷۷٬۰۰۰٬۰۰۰", "۵۵٬۰۰۰٬۰۰۰", "۳۳٬۰۰۰٬۰۰۰"):
        assert leaked not in page


def test_loss_is_flagged(world, mc):
    invoice(world.event, world.atelier, InvoiceStatus.SUBMITTED, income=1_000_000, expense=3_000_000)
    assert "(زیان)" in body(mc.get("/atelier/"))


def test_missing_staff_alert_lists_each_missing_role_with_urgency(world, mc):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    staffing.assign_person(world.event, "seller", user=world.staff, terms=BIG_TERMS, actor=world.manager)  # invited, waiting
    db.session.commit()
    page = body(mc.get("/atelier/"))
    assert "پرزنتور: ۱ نفر کم" in page and "ادیتور: ۱ نفر کم" in page
    assert "فروشنده: ۱ نفر کم، ۱ منتظر پاسخ" in page
    assert "عکاس: ۱ نفر کم" not in page
    assert "۷ روز دیگر" in page
    assert "alert--warning" in page  # 7 days away: warning, not danger


def test_near_event_is_a_danger_alert_and_far_events_are_not_alerted(world, mc, make_event):
    make_event(world.hall, world.atelier, date=day(2), title="نزدیک")
    make_event(world.hall, world.atelier, date=day(40), title="دور")
    page = body(mc.get("/atelier/"))
    assert "۲ روز دیگر" in page and "alert--danger" in page
    assert "۴۰ روز دیگر" not in page


def test_complete_staffing_raises_no_alert(world, mc, make_roster_member):
    for role in ("presenter", "photographer", "seller", "editor"):
        member = make_roster_member(world.atelier, display_name=f"نیرو {role}", skills=(Skill.coerce(role),))
        staffing.assign_person(world.event, role, roster_member=member, terms=BIG_TERMS, actor=world.manager)
    world.later.status = EventStatus.CANCELLED
    db.session.commit()
    page = body(mc.get("/atelier/"))
    assert "نفر کم" not in page
    assert "۴ از ۴ قطعی" in page


def test_offers_overdue_and_missing_invoice_sections(world, mc, make_event):
    offered = make_event(world.hall, None, date=day(15), status="offered", offer_mode=OfferMode.DIRECT)
    db.session.add(EventOffer(event=offered, atelier=world.atelier))
    make_event(world.hall, world.atelier, date=day(-3), status="confirmed", title="گذشتهٔ بسته‌نشده")
    make_event(world.hall, world.atelier, date=day(-6), status="done", title="بدون فاکتور")
    invoiced = make_event(world.hall, world.atelier, date=day(-9), status="done", title="فاکتوردار")
    invoice(invoiced, world.atelier, InvoiceStatus.SUBMITTED, 1, 0)
    submitted = make_event(world.hall, world.atelier, date=day(-12), status="confirmed", title="فاکتورشدهٔ تأییدشده")
    invoice(submitted, world.atelier, InvoiceStatus.SETTLED, 1, 0)
    db.session.commit()
    page = body(mc.get("/atelier/"))
    assert "۱ پیشنهاد مراسم منتظر پاسخ شماست" in page
    assert "مراسم برگزارشده هنوز بسته نشده" in page and "فاکتور مراسم ثبت نشده است" in page
    assert page.count("مراسم برگزارشده هنوز بسته نشده") == 1  # not for the confirmed event that already has its invoice
    assert page.count("فاکتور مراسم ثبت نشده است") == 1  # only the done event without a submitted invoice


def test_unread_notifications_are_listed(world, mc, make_notification):
    make_notification(world.manager, "پیام آزمایشی مهم", body="جزئیات پیام")
    page = body(mc.get("/atelier/"))
    assert "اعلان‌های خوانده‌نشده" in page and "پیام آزمایشی مهم" in page and "جزئیات پیام" in page


def test_atelier_switcher_only_appears_with_several_ateliers(world, mc, make_atelier):
    assert 'id="switch-atelier"' not in body(mc.get("/atelier/"))
    make_atelier(world.manager, name="شعبهٔ دوم")
    assert 'id="switch-atelier"' in body(mc.get("/atelier/"))


# --------------------------------------------------------------------------
# no N+1: the number of queries must not grow with the number of events
# --------------------------------------------------------------------------

LIST_PAGES = [
    "/atelier/",
    "/atelier/afish",
    "/atelier/events?tab=upcoming",
    "/atelier/events?tab=offers",
    "/atelier/calendar?y=1404&m=4",
    "/atelier/roster",
]


def test_list_pages_use_a_constant_number_of_queries(world, mc, make_event, make_roster_member, make_user):
    def measure(url):
        with count_queries() as statements:
            assert mc.get(url).status_code == 200
        return len(statements)

    for url in LIST_PAGES:
        mc.get(url)  # warm-up (lazy imports, metadata)
    small = {url: measure(url) for url in LIST_PAGES}

    for n in range(10):
        event = make_event(world.hall, world.atelier, date=day(3 + n), title=f"مراسم {n}")
        member = make_roster_member(world.atelier, display_name=f"نیرو {n}", skills=(Skill.PHOTOGRAPHER,))
        staffing.assign_person(event, "photographer", roster_member=member, terms=BIG_TERMS, actor=world.manager)
        user = make_user(Role.STAFF)
        staffing.assign_person(event, "seller", user=user, terms=BIG_TERMS, actor=world.manager, check_permissions=False)
        offered = make_event(world.hall, None, date=day(25 + n), status="offered", offer_mode=OfferMode.DIRECT)
        db.session.add(EventOffer(event=offered, atelier=world.atelier))
    db.session.commit()

    big = {url: measure(url) for url in LIST_PAGES}
    for url in LIST_PAGES:
        assert big[url] <= small[url] + 1, f"{url}: {small[url]} queries with 2 events, {big[url]} with 22"
