"""Dashboard, atelier directory, reports, agreements (and the rent they pre-fill), reminders, sharing."""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from test_halls_support import body, day, hc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import AtelierHall, Event, EventOffer, HallAgreement, Notification, Review
from app.modules.halls import rules, stats
from app.services import event_offers, jalali, security
from app.services.constants import OfferStatus


def notes(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id).order_by(Notification.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt))


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


def test_dashboard_numbers(hc, world, app):
    event_offers.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    html = body(hc.get("/hall/"))
    assert "مراسم‌های پیش‌رو" in html and "پیشنهادهای در انتظار پاسخ" in html and "اشغال تالار" in html
    assert "اتلیه ستاره" in html  # the pending offer
    assert "منتظر پاسخ" in html
    # 'free' = the two unassigned dates; both are within 14 days
    assert "۲ مراسم در ۱۴ روز آینده هنوز اتلیه‌ای ندارد" in html or "۱ مراسم در ۱۴ روز" in html


def test_dashboard_for_a_hall_without_events_explains_the_next_step(make_user, make_hall, client_for):
    manager = make_user("hall_manager")
    make_hall(manager)
    html = body(client_for(manager).get("/hall/"))
    assert "مراسم پیش‌رویی ندارید" in html and "پیشنهاد بازی ندارید" in html and "پاسخ تازه‌ای نیست" in html


def test_dashboard_occupancy_counts_days_not_events(hc, world, make_event):
    jy, jm, _ = jalali.to_jalali(security.today())
    first, last = jalali.jalali_month_range(jy, jm)
    make_event(world.hall, date=world.draft.date, start_time=dt.time(12, 0))  # a second service on the same day
    from app.modules.halls import queries

    rows = queries.occupancy([world.hall, world.hall2], first, last)
    hall_row = next(r for r in rows if r.hall.id == world.hall.id)
    assert hall_row.events >= 3
    days_with_events = {e.date for e in db.session.scalars(sa.select(Event).where(Event.hall_id == world.hall.id)) if first <= e.date <= last}
    assert hall_row.days_with_events == len(days_with_events)
    assert next(r for r in rows if r.hall.id == world.hall2.id).events == 0
    assert hall_row.percent == round(100 * len(days_with_events) / hall_row.days_in_month)


def test_dashboard_can_be_scoped_to_one_hall(hc, world):
    html = body(hc.get(f"/hall/?hall={world.hall2.id}"))
    assert "باغ بهار" in html
    assert hc.get("/hall/?hall=9999").status_code == 404


# --------------------------------------------------------------------------
# directory and atelier profile
# --------------------------------------------------------------------------


def test_directory_search_filters_and_sorting(hc, world, make_user, make_atelier):
    tehran = make_atelier(make_user("atelier_manager"), name="اتلیه تهران", city="تهران")
    html = body(hc.get("/hall/ateliers"))
    for name in ("اتلیه ستاره", "اتلیه رقیب", "اتلیه تهران"):
        assert name in html
    assert "اتلیه تهران" not in body(hc.get("/hall/ateliers?city=شیراز"))
    only = body(hc.get("/hall/ateliers?verified=1"))
    assert "اتلیه ستاره" in only and "اتلیه رقیب" not in only
    assert "اتلیه رقیب" in body(hc.get("/hall/ateliers?q=رقیب"))
    assert "اتلیه رقیب" not in body(hc.get("/hall/ateliers?q=ستاره"))
    assert "اتلیهٔ" not in body(hc.get("/hall/ateliers?q=%25"))
    by_name = body(hc.get("/hall/ateliers?sort=name"))
    assert by_name.index("اتلیه تهران") < by_name.index("اتلیه رقیب") < by_name.index("اتلیه ستاره")
    assert tehran.id


def test_directory_hides_pending_or_suspended_managers_and_inactive_ateliers(hc, world):
    world.rival_manager.status = "pending"
    world.atelier.is_active = False
    db.session.commit()
    html = body(hc.get("/hall/ateliers"))
    assert "اتلیه رقیب" not in html and "اتلیه ستاره" not in html
    assert "اتلیه‌ای پیدا نشد" in html


def test_directory_is_paginated(hc, world, make_user, make_atelier):
    for n in range(14):
        make_atelier(make_user("atelier_manager"), name=f"اتلیه شماره {n}", city="شیراز")
    first = body(hc.get("/hall/ateliers"))
    assert "صفحهٔ ۱ از ۲" in first
    assert hc.get("/hall/ateliers?page=2").status_code == 200


def test_atelier_profile_gates_the_phone_until_there_is_a_relationship(hc, world):
    world.rival.phone = "09123334444"
    world.atelier.phone = "09125556666"
    db.session.commit()
    stranger = body(hc.get(f"/hall/ateliers/{world.rival.id}"))
    assert "۰۹۱۲۳۳۳۴۴۴۴" not in stranger and "پس از اولین همکاری" in stranger
    partner = body(hc.get(f"/hall/ateliers/{world.atelier.id}"))  # has events at this hall
    assert "۰۹۱۲۵۵۵۶۶۶۶" in partner
    assert "پیشنهاد مراسم" in partner and "ثبت توافق‌نامه" in partner


def test_atelier_profile_shows_history_and_reviews_of_all_halls(hc, world, client_for, make_event):
    db.session.add(Review(event_id=world.past.id, atelier_id=world.atelier.id, author_id=world.manager.id, subject_id=world.atelier_manager.id, rating=4, comment="وقت‌شناس"))
    other = make_event(world.other_hall, world.atelier, date=day(-3))
    db.session.add(Review(event_id=other.id, atelier_id=world.atelier.id, author_id=world.outsider.id, subject_id=world.atelier_manager.id, rating=2, comment="دیر رسیدند"))
    db.session.commit()
    html = body(hc.get(f"/hall/ateliers/{world.atelier.id}"))
    assert "وقت‌شناس" in html and "دیر رسیدند" in html  # other halls' opinions help choosing
    assert "تالار دیگر" in html
    assert "۳٫۰" in html
    # ...but my own history only counts my halls
    assert "برگزارشده" in html


def test_directory_needs_no_extra_queries_per_atelier(hc, world, make_user, make_atelier):
    for n in range(10):
        make_atelier(make_user("atelier_manager"), name=f"اتلیه {n}", city="شیراز")
    counter = {"n": 0}

    def count(*args):
        counter["n"] += 1

    sa.event.listen(db.engine, "before_cursor_execute", count)
    try:
        hc.get("/hall/ateliers?q=ستاره")
        few = counter["n"]
        counter["n"] = 0
        hc.get("/hall/ateliers")
        many = counter["n"]
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", count)
    assert many <= few + 1


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


def answered_offers(world, clock):
    """atelier: accepts `draft` after 3 h, declines `later` after 1 h; rival: still pending on `later`."""
    event_offers.offer_events([world.draft], [world.atelier], world.manager)
    event_offers.offer_events([world.later], [world.atelier, world.rival], world.manager)
    db.session.commit()
    clock.advance(hours=1)
    later_offer = db.session.scalar(sa.select(EventOffer).where(EventOffer.event_id == world.later.id, EventOffer.atelier_id == world.atelier.id))
    event_offers.decline_offer(later_offer, world.atelier_manager)
    db.session.commit()
    clock.advance(hours=2)
    draft_offer = db.session.scalar(sa.select(EventOffer).where(EventOffer.event_id == world.draft.id))
    event_offers.accept_offer(draft_offer, world.atelier_manager)
    db.session.commit()


def test_offer_statistics(world, clock, app):
    answered_offers(world, clock)
    first, last = stats.year_range(1404)
    totals, per_atelier = stats.offer_stats([world.hall.id, world.hall2.id], first, last)
    assert (totals.sent, totals.accepted, totals.declined, totals.pending, totals.withdrawn) == (3, 1, 1, 1, 0)
    assert totals.answered == 2 and totals.acceptance_rate == 50
    assert sorted(round(h) for h in totals.response_hours) == [1, 3]
    assert round(totals.median_hours) == 2 and round(totals.average_hours) == 2
    rows = {row.atelier.id: row for row in per_atelier}
    assert rows[world.atelier.id].stats.sent == 2 and rows[world.atelier.id].stats.acceptance_rate == 50
    assert rows[world.rival.id].stats.sent == 1 and rows[world.rival.id].stats.acceptance_rate is None


def test_claims_are_counted_apart_from_answers(world):
    event_offers.open_to_all([world.draft], world.manager)
    db.session.commit()
    event_offers.claim_open_event(reload(world.draft), world.rival, world.rival_manager)
    db.session.commit()
    first, last = stats.year_range(1404)
    totals, _ = stats.offer_stats([world.hall.id], first, last)
    assert totals.claims == 1 and totals.sent == 0 and totals.acceptance_rate is None


def test_statistics_never_include_other_halls(world, clock, make_event):
    event_offers.offer_events([world.other_event], [world.atelier], world.outsider)
    db.session.commit()
    first, last = stats.year_range(1404)
    mine, _ = stats.offer_stats([world.hall.id, world.hall2.id], first, last)
    theirs, _ = stats.offer_stats([world.other_hall.id], first, last)
    assert mine.sent == 0 and theirs.sent == 1
    months = stats.events_per_month([world.other_hall.id], 1404)
    assert sum(row.total for row in months) == 1


def test_events_per_month_and_lost_dates(world, make_event):
    months = stats.events_per_month([world.hall.id], 1404)
    by_month = {row.jm: row for row in months}
    draft_month = jalali.to_jalali(world.draft.date)[1]
    assert by_month[draft_month].total >= 2 and by_month[draft_month].free >= 1
    confirmed_month = jalali.to_jalali(world.confirmed.date)[1]
    assert by_month[confirmed_month].booked >= 1
    cancelled = make_event(world.hall, date=day(15), status="cancelled")
    months = stats.events_per_month([world.hall.id], 1404)
    assert sum(r.cancelled for r in months) == 1 and cancelled.id
    lost_event = make_event(world.hall, date=day(-9))  # past, never got an atelier
    first, last = stats.year_range(1404)
    assert stats.lost_dates([world.hall.id], security.today(), first, last) == 1
    assert lost_event.id


def test_esfand_is_the_last_month_of_the_year_report(world, make_event, clock):
    clock.set("2026-02-01 08:30:00")
    make_event(world.hall, date=dt.date(2026, 3, 20))  # 29 Esfand 1404
    make_event(world.hall, date=dt.date(2026, 3, 21))  # 1 Farvardin 1405
    months_1404 = stats.events_per_month([world.hall.id], 1404)
    months_1405 = stats.events_per_month([world.hall.id], 1405)
    assert months_1404[11].total == 1 and months_1405[0].total == 1
    assert stats.year_range(1403)[1] == dt.date(2025, 3, 20)  # 30 Esfand 1403 (leap year)
    assert stats.year_range(1404)[1] == dt.date(2026, 3, 20)


def test_report_page(hc, world, clock):
    answered_offers(world, clock)
    html = body(hc.get("/hall/reports?y=1404"))
    assert "نرخ پذیرش پیشنهاد" in html and "۵۰٪" in html
    assert "زمان پاسخ" in html and "ساعت" in html
    assert "اتلیه ستاره" in html and "اتلیه رقیب" in html
    assert "مراسم‌ها در هر ماه" in html and "<progress" in html
    assert "تیر" in html
    empty = body(hc.get("/hall/reports?y=1399"))
    assert "در این سال مراسمی ثبت نشده" in empty and "پیشنهادی در این سال ثبت نشده" in empty
    assert "—" in empty


def test_format_hours():
    assert stats.format_hours(None) == "—"
    assert stats.format_hours(0.2) == "۱۲ دقیقه"
    assert stats.format_hours(0.001) == "۱ دقیقه"
    assert stats.format_hours(5.4) == "۵ ساعت"
    assert stats.format_hours(72) == "۳ روز"


# --------------------------------------------------------------------------
# agreements
# --------------------------------------------------------------------------


def agree(hc, world, **fields):
    data = {"atelier_id": world.atelier.id, "hall_id": world.hall.id, "start_date": day(1).isoformat(), **fields}
    return hc.post("/hall/agreements/new", data=data)


def test_creating_an_agreement_tells_the_atelier(hc, world):
    response = agree(hc, world, end_date=day(200).isoformat(), is_exclusive="1", rent_per_event="۳٬۰۰۰٬۰۰۰", deposit="500000", contact_name="آقای رضایی", contact_phone="۰۹۱۲۰۰۰۱۱۱۱", notes="شام و ناهار")
    assert response.status_code == 302
    agreement = db.session.scalar(sa.select(HallAgreement))
    assert agreement.is_exclusive and agreement.rent_per_event == 3_000_000 and agreement.deposit == 500_000
    assert agreement.contact_phone == "09120001111" and agreement.end_date == day(200) and agreement.created_by_id == world.manager.id
    (note,) = notes(world.atelier_manager, "system")
    assert "توافق‌نامهٔ جدید" in note.title and "انحصاری" in note.body and "تالار گل‌ها" in note.body
    page = body(hc.get("/hall/agreements"))
    assert "اتلیه ستاره" in page and "انحصاری" in page and "۳٬۰۰۰٬۰۰۰" in page and "فعال" in page


def test_agreement_validation(hc, world):
    bad = [
        ({"start_date": ""}, "تاریخ شروع را وارد کنید"),
        ({"end_date": day(-5).isoformat()}, "پیش از تاریخ شروع"),
        ({"rent_per_event": "100", "revenue_share_percent": "10"}, "یا اجارهٔ هر مراسم"),
        ({"revenue_share_percent": "150"}, "نباید بیشتر"),
        ({"rent_per_event": "abc"}, "نامعتبر"),
    ]
    for fields, message in bad:
        response = agree(hc, world, **fields)
        assert response.status_code == 422 and message in body(response), fields
    assert db.session.scalar(sa.select(sa.func.count(HallAgreement.id))) == 0


def test_overlapping_agreements_with_the_same_atelier_are_refused(hc, world):
    assert agree(hc, world, end_date=day(30).isoformat()).status_code == 302
    response = agree(hc, world, start_date=day(10).isoformat(), end_date=day(50).isoformat())
    assert response.status_code == 422 and "توافق‌نامهٔ فعال دیگری" in body(response)
    assert agree(hc, world, start_date=day(31).isoformat()).status_code == 302  # starts after the first ends


def test_only_one_atelier_can_be_exclusive_in_a_period(hc, world):
    assert agree(hc, world, is_exclusive="1", end_date=day(100).isoformat()).status_code == 302
    data = {"atelier_id": world.rival.id, "hall_id": world.hall.id, "start_date": day(50).isoformat(), "is_exclusive": "1"}
    response = hc.post("/hall/agreements/new", data=data)
    assert response.status_code == 422 and "هم‌زمان انحصاری" in body(response)
    data.pop("is_exclusive")
    assert hc.post("/hall/agreements/new", data=data).status_code == 302  # a non-exclusive one is fine


def test_exclusivity_cannot_start_over_events_that_belong_to_another_atelier(hc, world):
    response = hc.post(
        "/hall/agreements/new",
        data={"atelier_id": world.rival.id, "hall_id": world.hall.id, "start_date": day(0).isoformat(), "end_date": day(60).isoformat(), "is_exclusive": "1"},
    )
    assert response.status_code == 422 and "با اتلیهٔ دیگری ثبت شده" in body(response)  # world.confirmed belongs to the star atelier


def test_an_exclusive_agreement_withdraws_pending_offers_to_others(hc, world):
    event_offers.offer_events([world.draft], [world.rival], world.manager)
    db.session.commit()
    response = agree(hc, world, is_exclusive="1", end_date=day(100).isoformat())
    assert response.status_code == 302
    (offer,) = db.session.scalars(sa.select(EventOffer).where(EventOffer.event_id == world.draft.id)).all()
    assert offer.status == OfferStatus.WITHDRAWN
    assert any("پس گرفته شد" in n.title for n in notes(world.rival_manager))


def test_edit_and_end_an_agreement(hc, world):
    agree(hc, world, end_date=day(30).isoformat(), rent_per_event="1000000")
    agreement = db.session.scalar(sa.select(HallAgreement))
    page = hc.get(f"/hall/agreements/{agreement.id}/edit")
    assert page.status_code == 200 and "اتلیه ستاره" in body(page)
    response = hc.post(
        f"/hall/agreements/{agreement.id}/edit",
        data={"start_date": day(1).isoformat(), "end_date": day(90).isoformat(), "rent_per_event": "2000000", "is_exclusive": ""},
    )
    assert response.status_code == 302
    agreement = reload(agreement)
    assert agreement.end_date == day(90) and agreement.rent_per_event == 2_000_000
    assert any("شرایط توافق‌نامه" in n.title for n in notes(world.atelier_manager))
    assert hc.post(f"/hall/agreements/{agreement.id}/end", data={}).status_code == 302
    assert reload(agreement).terminated_at is not None
    assert any("پایان یافت" in n.title for n in notes(world.atelier_manager))
    assert "پایان‌یافته" in body(hc.get("/hall/agreements?tab=past"))
    assert hc.get(f"/hall/agreements/{agreement.id}/edit").status_code == 302
    again = hc.post(f"/hall/agreements/{agreement.id}/end", data={}, follow_redirects=True)
    assert "پیشتر پایان یافته" in body(again)


def test_agreement_status_words(world):
    today = security.today()
    a = HallAgreement(hall=world.hall, atelier=world.atelier, start_date=today - dt.timedelta(days=5), end_date=today + dt.timedelta(days=5))
    assert rules.agreement_status(a, today) == "active"
    a.start_date = today + dt.timedelta(days=1)
    assert rules.agreement_status(a, today) == "upcoming"
    a.start_date, a.end_date = today - dt.timedelta(days=9), today - dt.timedelta(days=1)
    assert rules.agreement_status(a, today) == "expired"
    a.end_date, a.terminated_at = None, security.now()
    assert rules.agreement_status(a, today) == "terminated"
    a.terminated_at = None
    assert a.is_active_on(today) and a.is_active_on(today + dt.timedelta(days=900))


# --------------------------------------------------------------------------
# rent that pre-fills the invoice
# --------------------------------------------------------------------------


def test_rent_for_hall_precedence(world):
    atelier, hall = world.atelier, world.hall
    atelier.default_venue_rent = 2_000_000
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 2_000_000  # nothing hall-specific
    hall.default_rent = 3_000_000
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 3_000_000  # the hall's list price beats the atelier-wide default
    assert atelier.rent_for_hall(world.hall2) == 2_000_000
    agreement = HallAgreement(hall=hall, atelier=atelier, start_date=dt.date.today() - dt.timedelta(days=1), rent_per_event=4_000_000)
    db.session.add(agreement)
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 4_000_000  # the signed terms beat the list price
    assert world.rival.rent_for_hall(hall) == 3_000_000  # other ateliers do not get this agreement
    db.session.add(AtelierHall(atelier=atelier, hall=hall, rent_amount=5_000_000))
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 5_000_000  # the atelier's own per-hall price always wins
    agreement.terminated_at = security.now()
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 5_000_000


def test_an_ended_agreement_no_longer_sets_the_rent(world):
    hall, atelier = world.hall, world.atelier
    db.session.add(HallAgreement(hall=hall, atelier=atelier, start_date=dt.date(2020, 1, 1), end_date=dt.date(2020, 12, 31), rent_per_event=9_000_000))
    db.session.commit()
    assert atelier.rent_for_hall(hall) == 0
    assert atelier.rent_for_hall(hall, on=dt.date(2020, 6, 1)) == 9_000_000


# --------------------------------------------------------------------------
# reminders
# --------------------------------------------------------------------------


def test_agreement_reminders_go_to_both_sides_once(world, app):
    agreement = HallAgreement(hall=world.hall, atelier=world.atelier, start_date=day(-30), end_date=day(5))
    far = HallAgreement(hall=world.hall2, atelier=world.atelier, start_date=day(-30), end_date=day(90))
    open_ended = HallAgreement(hall=world.hall2, atelier=world.rival, start_date=day(-30))
    db.session.add_all([agreement, far, open_ended])
    db.session.commit()
    with app.test_request_context("/"):
        assert rules.remind_agreements(14, security.today()) == 1
        db.session.commit()
        assert rules.remind_agreements(14, security.today()) == 0  # once per end date
    assert len(notes(world.manager, "system")) == 1 and len(notes(world.atelier_manager, "system")) == 1
    assert "۵ روز دیگر" in notes(world.manager, "system")[0].body
    agreement.end_date = day(8)  # extended -> reminded again for the new date
    agreement.reminded_at = None
    db.session.commit()
    with app.test_request_context("/"):
        assert rules.remind_agreements(14, security.today()) == 1


def test_the_reminder_command_runs(world, app):
    db.session.add(HallAgreement(hall=world.hall, atelier=world.atelier, start_date=day(-30), end_date=day(3)))
    db.session.commit()
    result = app.test_cli_runner().invoke(args=["halls-agreement-reminders", "--days", "7"])
    assert result.exit_code == 0 and "1 agreement reminder(s) sent." in result.output
    assert app.test_cli_runner().invoke(args=["halls-agreement-reminders"]).output.startswith("0 agreement")


# --------------------------------------------------------------------------
# sharing the event facts
# --------------------------------------------------------------------------


def test_share_text_has_the_facts_an_atelier_team_needs(hc, world):
    world.hall.map_url = "https://maps.example.org/x"
    world.hall.logistics = {"loading": "درِ پشتی", "parking": "حیاط", "contact_phone": "09121112233"}
    db.session.commit()
    html = body(hc.get(f"/hall/events/{world.draft.id}"))
    assert "data-hall-share" in html and "data-hall-copy" in html
    assert "https://maps.example.org/x" in html and "درِ پشتی" in html and "بارگیری" in html
    assert "ساعت ۱۸:۰۰" in html

