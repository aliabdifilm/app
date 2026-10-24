"""Events of the hall: create, duplicates, edit, cancel, reactivate, delete, bulk-add, calendar and list."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from test_halls_support import body, day, hc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import AuditLog, Event, EventOffer, Notification
from app.services import event_offers, jalali
from app.services.constants import EventStatus, OfferMode, OfferStatus


def events_on(hall, date):
    db.session.expire_all()
    return list(db.session.scalars(sa.select(Event).where(Event.hall_id == hall.id, Event.date == date).order_by(Event.id)))


def count_events(hall=None):
    db.session.expire_all()
    stmt = sa.select(sa.func.count(Event.id))
    if hall is not None:
        stmt = stmt.where(Event.hall_id == hall.id)
    return db.session.scalar(stmt)


def audit_actions(entity=None):
    db.session.expire_all()
    stmt = sa.select(AuditLog.action).order_by(AuditLog.id)
    if entity:
        stmt = stmt.where(AuditLog.entity_type == entity)
    return list(db.session.scalars(stmt))


def notes(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt))


NEW = {"ceremony_type": "wedding"}


def new_event(hc, hall, date, **fields):
    data = {"hall_id": hall.id, "date": date.isoformat(), **NEW, **fields}
    return hc.post("/hall/events/new", data=data)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_create_an_event_with_persian_digits(hc, world):
    response = new_event(hc, world.hall, day(30), start_time="۱۸:۳۰", guests="۲۵۰", title="  عروسی ي ك  ", notes="یادداشت")
    assert response.status_code == 302
    (event,) = events_on(world.hall, day(30))
    assert response.headers["Location"].endswith(f"/hall/events/{event.id}")
    assert event.start_time == dt.time(18, 30) and event.guest_count_estimate == 250
    assert event.status == EventStatus.DRAFT and event.offer_mode == OfferMode.NONE and event.atelier_id is None
    assert event.created_by_id == world.manager.id
    assert event.title == "عروسی ی ک"  # Arabic yeh / kaf normalised
    assert "event.created" in audit_actions("events")


def test_a_jalali_date_typed_into_the_field_is_accepted(hc, world):
    response = hc.post("/hall/events/new", data={"hall_id": world.hall.id, "date": "۱۴۰۴/۰۵/۱۲", **NEW})
    assert response.status_code == 302
    assert len(events_on(world.hall, jalali.parse_jalali("1404/05/12"))) == 1


def test_the_new_event_form_keeps_the_hall_and_prefilled_date(hc, world):
    html = body(hc.get(f"/hall/events/new?hall={world.hall2.id}&date={day(33).isoformat()}"))
    assert f'value="{day(33).isoformat()}"' in html
    assert 'name="hall_id"' in html and "باغ بهار" in html


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"date": ""}, "تاریخ مراسم را وارد کنید"),
        ({"date": "not-a-date"}, "نامعتبر"),
        ({"date": day(-1).isoformat()}, "نباید گذشته باشد"),
        ({"date": day(2000).isoformat()}, "سه سال"),
        ({"date": day(40).isoformat(), "start_time": "25:99"}, "ساعت شروع نامعتبر"),
        ({"date": day(40).isoformat(), "guests": "abc"}, "عدد صحیح"),
        ({"date": day(40).isoformat(), "guests": "99999"}, "نباید بیشتر"),
        ({"date": day(40).isoformat(), "ceremony_type": "funeral"}, "نوع مراسم نامعتبر"),
        ({"date": day(40).isoformat(), "title": "x" * 200}, "نباید بیش از"),
    ],
)
def test_invalid_input_is_a_422_with_a_persian_message_and_creates_nothing(hc, world, fields, message):
    before = count_events()
    data = {"hall_id": world.hall.id, **NEW, "title": "عنوان من", **fields}
    response = hc.post("/hall/events/new", data=data)
    assert response.status_code == 422
    html = body(response)
    assert message in html
    assert count_events() == before
    if fields.get("title") != "x" * 200:
        assert "عنوان من" in html  # what was typed is kept


def test_a_second_event_at_the_same_hall_date_and_time_is_refused(hc, world):
    assert new_event(hc, world.hall, day(30), start_time="18:00").status_code == 302
    response = new_event(hc, world.hall, day(30), start_time="18:00")
    assert response.status_code == 422
    assert "مراسم دیگری ثبت شده" in body(response)
    assert len(events_on(world.hall, day(30))) == 1


def test_the_duplicate_message_links_to_the_existing_event(hc, world):
    response = new_event(hc, world.hall, world.draft.date, start_time="18:00")
    assert response.status_code == 422
    assert f"/hall/events/{world.draft.id}" in body(response)


def test_other_times_halls_and_dates_are_not_duplicates(hc, world):
    assert new_event(hc, world.hall, day(30), start_time="13:00").status_code == 302
    assert new_event(hc, world.hall, day(30), start_time="19:00").status_code == 302  # lunch and dinner
    assert new_event(hc, world.hall2, day(30), start_time="13:00").status_code == 302  # another hall
    assert new_event(hc, world.hall, day(31), start_time="13:00").status_code == 302  # another day
    assert count_events() == 4 + 1 + 4  # the world's four events, the outsider's one, ours


def test_two_events_without_a_time_on_one_day_are_duplicates_but_a_timed_one_is_not(hc, world):
    assert new_event(hc, world.hall, day(30)).status_code == 302
    assert new_event(hc, world.hall, day(30)).status_code == 422
    assert new_event(hc, world.hall, day(30), start_time="20:00").status_code == 302


def test_submitting_the_same_form_twice_creates_one_event(hc, world):
    first = new_event(hc, world.hall, day(30), start_time="18:00")
    second = new_event(hc, world.hall, day(30), start_time="18:00")
    assert (first.status_code, second.status_code) == (302, 422)
    assert len(events_on(world.hall, day(30))) == 1


def test_a_cancelled_slot_can_be_used_again(hc, world):
    assert new_event(hc, world.hall, day(30), start_time="18:00").status_code == 302
    (first,) = events_on(world.hall, day(30))
    assert hc.post(f"/hall/events/{first.id}/cancel", data={}).status_code == 302
    assert new_event(hc, world.hall, day(30), start_time="18:00").status_code == 302
    assert len(events_on(world.hall, day(30))) == 2


def test_an_inactive_hall_takes_no_new_events(hc, world):
    world.hall2.is_active = False
    db.session.commit()
    response = new_event(hc, world.hall2, day(30))
    assert response.status_code == 422 and "غیرفعال" in body(response)


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def edit(hc, event, **fields):
    data = {"date": event.date.isoformat(), "ceremony_type": event.ceremony_type.value, **fields}
    return hc.post(f"/hall/events/{event.id}/edit", data=data)


def test_edit_changes_the_fields_and_is_audited(hc, world):
    response = edit(hc, world.draft, start_time="19:15", guests="450", title="تازه", notes="ن")
    assert response.status_code == 302
    event = reload(world.draft)
    assert event.start_time == dt.time(19, 15) and event.guest_count_estimate == 450 and event.title == "تازه"
    assert "event.updated" in audit_actions("events")
    row = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "event.updated"))
    assert "start_time" in row.data["changes"]


def test_edit_without_changes_says_so(hc, world):
    response = edit(hc, world.draft, start_time="18:00", guests="300", title="عروسی نمونه")
    assert response.status_code == 302
    assert "event.updated" not in audit_actions("events")


def test_editing_the_time_tells_everybody_with_a_pending_offer(hc, world):
    event_offers.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    edit(hc, world.draft, start_time="20:00")
    for manager in (world.atelier_manager, world.rival_manager):
        titles = [n.title for n in notes(manager, "event_status")]
        assert any("مشخصات یک مراسم تغییر کرد" in t for t in titles)
    assert "ساعت" in notes(world.atelier_manager, "event_status")[-1].body


def test_the_date_of_an_event_with_an_atelier_cannot_change_but_the_time_can(hc, world):
    response = edit(hc, world.confirmed, date=day(21).isoformat())
    assert response.status_code == 422 and "قابل تغییر نیست" in body(response)
    assert reload(world.confirmed).date == day(20)
    assert edit(hc, world.confirmed, start_time="21:00").status_code == 302
    assert reload(world.confirmed).start_time == dt.time(21, 0)
    assert any("مشخصات" in n.title for n in notes(world.atelier_manager, "event_status"))


def test_editing_into_an_occupied_slot_is_refused(hc, world):
    response = edit(hc, world.later, date=world.draft.date.isoformat(), start_time="18:00")
    assert response.status_code == 422 and "مراسم دیگری ثبت شده" in body(response)
    assert reload(world.later).date == day(12)


def test_closed_events_cannot_be_edited(hc, world):
    world.past.status = EventStatus.DONE
    db.session.commit()
    assert hc.get(f"/hall/events/{world.past.id}/edit").status_code == 302
    assert edit(hc, world.past, start_time="10:00").status_code == 302
    assert reload(world.past).start_time is None


# --------------------------------------------------------------------------
# cancel, reactivate, delete
# --------------------------------------------------------------------------


def test_cancelling_an_offered_event_withdraws_the_offers_and_tells_the_ateliers(hc, world):
    event_offers.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    response = hc.post(f"/hall/events/{world.draft.id}/cancel", data={"reason": "تالار تعطیل است"})
    assert response.status_code == 302
    assert reload(world.draft).status == EventStatus.CANCELLED
    (offer,) = db.session.scalars(sa.select(EventOffer).where(EventOffer.event_id == world.draft.id))
    assert offer.status == OfferStatus.WITHDRAWN
    assert any("تالار تعطیل است" in n.body for n in notes(world.atelier_manager))


def test_cancelling_a_confirmed_event_notifies_the_atelier_and_its_staff(hc, world, make_assignment, make_user):
    staff = make_user("staff")
    assignment = make_assignment(world.confirmed, user=staff)
    assert hc.post(f"/hall/events/{world.confirmed.id}/cancel", data={}).status_code == 302
    assert reload(world.confirmed).status == EventStatus.CANCELLED
    assert reload(assignment).status.value == "cancelled"
    assert any("لغو" in n.title or "وضعیت" in n.title for n in notes(world.atelier_manager))
    assert notes(staff)  # the person who was staffed hears about it too


def test_cancelling_twice_or_a_done_event_is_a_polite_error(hc, world):
    assert hc.post(f"/hall/events/{world.draft.id}/cancel", data={}).status_code == 302
    response = hc.post(f"/hall/events/{world.draft.id}/cancel", data={}, follow_redirects=True)
    assert "نمی‌توان لغو کرد" in body(response)
    world.past.status = EventStatus.DONE
    db.session.commit()
    response = hc.post(f"/hall/events/{world.past.id}/cancel", data={}, follow_redirects=True)
    assert "نمی‌توان لغو کرد" in body(response)
    assert reload(world.past).status == EventStatus.DONE


def test_reactivating_gives_a_free_draft_and_checks_the_slot(hc, world):
    hc.post(f"/hall/events/{world.confirmed.id}/cancel", data={})
    assert hc.post(f"/hall/events/{world.confirmed.id}/reactivate", data={}).status_code == 302
    event = reload(world.confirmed)
    assert event.status == EventStatus.DRAFT and event.atelier_id is None and event.offer_mode == OfferMode.NONE
    # a new event took the slot meanwhile -> cannot come back
    hc.post(f"/hall/events/{world.draft.id}/cancel", data={})
    new_event(hc, world.hall, world.draft.date, start_time="18:00")
    response = hc.post(f"/hall/events/{world.draft.id}/reactivate", data={}, follow_redirects=True)
    assert "مراسم دیگری ثبت شده" in body(response)
    assert reload(world.draft).status == EventStatus.CANCELLED


def test_only_a_clean_draft_can_be_deleted(hc, world):
    later_id, draft_id, confirmed_id = world.later.id, world.draft.id, world.confirmed.id
    assert hc.post(f"/hall/events/{later_id}/delete", data={}).status_code == 302
    assert db.session.get(Event, later_id) is None
    assert "event.deleted" in audit_actions("events")
    event_offers.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    response = hc.post(f"/hall/events/{draft_id}/delete", data={}, follow_redirects=True)
    assert "حذف" in body(response)
    assert db.session.get(Event, draft_id) is not None
    hc.post(f"/hall/events/{confirmed_id}/delete", data={})
    assert db.session.get(Event, confirmed_id) is not None


# --------------------------------------------------------------------------
# bulk add
# --------------------------------------------------------------------------


def bulk(hc, hall, **fields):
    return hc.post("/hall/events/bulk", data={"hall_id": hall.id, "ceremony_type": "wedding", **fields})


def test_bulk_add_picked_dates(hc, world):
    picked = [day(30), day(31), day(33)]
    response = bulk(hc, world.hall, dates=[d.isoformat() for d in picked], start_time="18:00")
    assert response.status_code == 302 and "tab=free" in response.headers["Location"]
    for d in picked:
        (event,) = events_on(world.hall, d)
        assert event.start_time == dt.time(18, 0) and event.status == EventStatus.DRAFT
    assert audit_actions("events").count("event.created") >= 3


def test_bulk_add_typed_jalali_dates_including_esfand_30_of_a_leap_year(hc, world, clock):
    clock.set("2025-01-10 08:30:00")  # 21 Dey 1403
    text = "1403/12/28\n۱۴۰۳/۱۲/۲۹\n1403/12/30\n1404/01/01"
    response = bulk(hc, world.hall, dates_text=text)
    assert response.status_code == 302
    found = sorted(e.date for e in db.session.scalars(sa.select(Event).where(Event.hall_id == world.hall.id, Event.date >= dt.date(2025, 3, 1), Event.date <= dt.date(2025, 3, 31))))
    assert found == [dt.date(2025, 3, 18), dt.date(2025, 3, 19), dt.date(2025, 3, 20), dt.date(2025, 3, 21)]
    assert jalali.to_jalali(found[2]) == (1403, 12, 30)


def test_bulk_add_with_esfand_30_of_a_common_year_is_rejected_whole(hc, world):
    before = count_events()
    response = bulk(hc, world.hall, dates_text="1404/12/28\n1404/12/30\n1404/12/29")
    assert response.status_code == 422
    html = body(response)
    assert "اسفند" in html and "۲۹ روز" in html and "کبیسه" in html
    assert count_events() == before  # nothing half-created
    assert "1404/12/28" in html or "۱۴۰۴/۱۲/۲۸" in html  # the typed text is kept for correction


def test_bulk_add_reports_every_bad_line(hc, world):
    response = bulk(hc, world.hall, dates_text="1404/13/01\nhello\n1404/07/31")
    assert response.status_code == 422
    html = body(response)
    assert "ماه باید" in html and "قالب تاریخ" in html and "۳۰ روز" in html


def test_bulk_add_by_weekly_rule_on_the_server(hc, world):
    start, end = day(40), day(68)  # four full weeks
    response = bulk(
        hc, world.hall,
        rule_mode="weekly", rule_from=start.isoformat(), rule_to=end.isoformat(), rule_weekdays=["5", "6"],
    )
    assert response.status_code == 302
    created = [e.date for e in db.session.scalars(sa.select(Event).where(Event.hall_id == world.hall.id, Event.date >= start))]
    assert created and all(jalali.weekday_index(d) in (5, 6) for d in created)
    assert len(created) == sum(1 for n in range(29) if jalali.weekday_index(start + dt.timedelta(days=n)) in (5, 6))


def test_bulk_add_by_monthly_rule_clamps_the_30th_of_a_common_esfand(hc, world, clock):
    clock.set("2026-01-10 08:30:00")  # Dey 1404
    start = dt.date(*jalali.jalali_to_gregorian(1404, 11, 1))
    end = dt.date(*jalali.jalali_to_gregorian(1404, 12, 29))
    response = bulk(hc, world.hall, rule_mode="monthly", rule_from=start.isoformat(), rule_to=end.isoformat(), rule_days="30", rule_short="clamp")
    assert response.status_code == 302
    got = sorted(e.date for e in db.session.scalars(sa.select(Event).where(Event.hall_id == world.hall.id, Event.date >= start)))
    assert [jalali.to_jalali(d)[1:] for d in got] == [(11, 30), (12, 29)]


def test_pattern_dates_union_with_picked_and_typed_dates(hc, world):
    response = bulk(
        hc, world.hall, dates=[day(30).isoformat()], dates_text=day(31).isoformat(),
        rule_mode="weekly", rule_from=day(40).isoformat(), rule_to=day(46).isoformat(), rule_weekdays=["0"],
    )
    assert response.status_code == 302
    assert len(events_on(world.hall, day(30))) == 1 and len(events_on(world.hall, day(31))) == 1


def test_a_preselected_pattern_with_empty_dates_is_ignored(hc, world):
    response = bulk(hc, world.hall, dates=[day(30).isoformat()], rule_mode="weekly", rule_weekdays=["5", "6"])
    assert response.status_code == 302
    assert len(events_on(world.hall, day(30))) == 1


def test_bulk_add_with_two_start_times_makes_lunch_and_dinner(hc, world):
    response = bulk(hc, world.hall, dates=[day(30).isoformat()], start_time="13:00", start_time_2="19:30")
    assert response.status_code == 302
    times = sorted(e.start_time for e in events_on(world.hall, day(30)))
    assert times == [dt.time(13, 0), dt.time(19, 30)]


def test_running_the_same_bulk_twice_creates_nothing_new_and_says_so(hc, world):
    data = dict(dates=[day(30).isoformat(), day(31).isoformat()], start_time="18:00")
    assert bulk(hc, world.hall, **data).status_code == 302
    before = count_events()
    response = hc.post("/hall/events/bulk", data={"hall_id": world.hall.id, "ceremony_type": "wedding", **data}, follow_redirects=True)
    assert count_events() == before
    assert "از قبل ثبت شده بود" in body(response)


def test_past_dates_are_skipped_not_fatal(hc, world):
    response = bulk(hc, world.hall, dates=[day(-3).isoformat(), day(30).isoformat()])
    assert response.status_code == 302
    assert events_on(world.hall, day(-3)) == []  # nothing was created in the past
    assert len(events_on(world.hall, day(30))) == 1
    followed = hc.post("/hall/events/bulk", data={"hall_id": world.hall.id, "ceremony_type": "wedding", "dates": [day(-3).isoformat(), day(31).isoformat()]}, follow_redirects=True)
    assert "تاریخ گذشته نادیده گرفته شد" in body(followed)


def test_bulk_add_needs_at_least_one_date_and_caps_the_size(hc, world):
    response = bulk(hc, world.hall)
    assert response.status_code == 422 and "دست‌کم یک تاریخ" in body(response)
    many = [(day(30) + dt.timedelta(days=n)).isoformat() for n in range(367)]
    response = bulk(hc, world.hall, dates=many)
    assert response.status_code == 422 and "حداکثر" in body(response)


def test_bulk_add_into_a_foreign_hall_is_404(client_for, world, make_hall):
    make_hall(world.outsider, name="دومی")
    response = bulk(client_for(world.outsider), world.hall, dates=[day(30).isoformat()])
    assert response.status_code == 404


def test_expand_endpoint_returns_dates_and_explains_errors(hc, world):
    ok = hc.post("/hall/api/dates/expand", json={"mode": "weekly", "from": "1403/12/20", "to": "1404/01/10", "weekdays": [5]})
    assert ok.status_code == 200
    data = ok.get_json()["data"]
    assert data["count"] == len(data["dates"]) and data["count"] >= 3
    assert all(jalali.weekday_index(dt.date.fromisoformat(d)) == 5 for d in data["dates"])
    monthly = hc.post("/hall/api/dates/expand", json={"mode": "monthly", "from": "1404/11/01", "to": "1404/12/29", "days": [30], "short": "skip"})
    assert [jalali.to_jalali(dt.date.fromisoformat(d))[1:] for d in monthly.get_json()["data"]["dates"]] == [(11, 30)]
    bad = hc.post("/hall/api/dates/expand", json={"mode": "weekly", "from": "1404/12/30", "to": "1405/01/10", "weekdays": [5]})
    assert bad.status_code == 422 and bad.get_json()["ok"] is False and "from" in bad.get_json()["fields"]
    assert hc.post("/hall/api/dates/expand", json={"mode": "weekly", "from": "1404/01/01", "to": "1404/02/01", "weekdays": []}).status_code == 422
    assert hc.post("/hall/api/dates/expand", json={"mode": "nope", "from": "1404/01/01", "to": "1404/02/01"}).status_code == 422
    assert hc.post("/hall/api/dates/expand", json={"mode": "weekly", "from": "1404/01/01", "to": "1408/01/01", "weekdays": [1]}).status_code == 422


# --------------------------------------------------------------------------
# list and calendar
# --------------------------------------------------------------------------


def test_the_event_list_has_tabs_counts_and_search(hc, world):
    html = body(hc.get("/hall/events"))
    assert "تالار گل‌ها" in html and "بدون اتلیه" in html
    free = body(hc.get("/hall/events?tab=free"))
    assert free.count("hl-event__pick") >= 2  # the two drafts can be ticked
    past = body(hc.get("/hall/events?tab=past"))
    assert "hl-event__pick" not in past
    assert "عروسی نمونه" in body(hc.get("/hall/events?q=نمونه"))
    assert "عروسی نمونه" not in body(hc.get("/hall/events?q=چیزی-که-نیست"))
    assert "مراسمی پیدا نشد" in body(hc.get("/hall/events?q=چیزی-که-نیست"))
    assert hc.get("/hall/events?q=%25").status_code == 200  # LIKE wildcards are escaped


def test_the_event_list_is_paginated_without_extra_queries_per_row(hc, world, make_event, app):
    for n in range(40):
        make_event(world.hall, date=day(50 + n))
    first = body(hc.get("/hall/events"))
    assert "صفحهٔ ۱ از" in first
    assert hc.get("/hall/events?page=2").status_code == 200
    assert hc.get("/hall/events?page=999").status_code == 200
    counter = {"n": 0}

    def count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    sa.event.listen(db.engine, "before_cursor_execute", count)
    try:
        hc.get("/hall/events")
        small = counter["n"]
        counter["n"] = 0
        hc.get("/hall/events?per_page=100")
        large = counter["n"]
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", count)
    assert large <= small + 2  # 100 rows cost the same queries as 15


def test_the_calendar_places_events_on_their_jalali_day(hc, world):
    jy, jm, jd = jalali.to_jalali(world.draft.date)
    html = body(hc.get(f"/hall/calendar?y={jy}&m={jm}"))
    assert "cal__event" in html and f"/hall/events/{world.draft.id}" in html
    assert "۱۸:۰۰" in html or "عروسی" in html


def test_the_calendar_knows_the_length_of_esfand(hc, world):
    leap = body(hc.get("/hall/calendar?y=1403&m=12"))
    common = body(hc.get("/hall/calendar?y=1404&m=12"))
    assert leap.count('role="gridcell"') > common.count('role="gridcell"') - 7  # same grid, one more numbered cell
    assert "۳۰" in leap.split("cal__grid")[1].split("cal__agenda")[0]
    assert "۳۰" not in common.split("cal__grid")[1].split("cal__agenda")[0]


def test_calendar_hall_filter(hc, world):
    only_second = body(hc.get(f"/hall/calendar?hall={world.hall2.id}"))
    assert "باغ بهار" in only_second
    assert hc.get("/hall/calendar?hall=abc").status_code == 404
    assert hc.get("/hall/calendar?hall=all").status_code == 200


# --------------------------------------------------------------------------
# bulk actions on the list
# --------------------------------------------------------------------------


def test_bulk_open_to_all(hc, world):
    response = hc.post("/hall/events/action", data={"action": "open", "event_ids": [world.draft.id, world.later.id]})
    assert response.status_code == 302
    for event in (world.draft, world.later):
        event = reload(event)
        assert event.status == EventStatus.OPEN and event.offer_mode == OfferMode.OPEN


def test_bulk_offer_action_carries_the_selection_to_the_offer_page(hc, world):
    response = hc.post("/hall/events/action", data={"action": "offer", "event_ids": [world.draft.id, world.later.id]})
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/hall/offers/new") and f"event={world.draft.id}" in location and f"event={world.later.id}" in location


def test_bulk_cancel_and_delete_report_what_they_skipped(hc, world):
    response = hc.post("/hall/events/action", data={"action": "cancel", "event_ids": [world.draft.id, world.confirmed.id]}, follow_redirects=True)
    assert reload(world.draft).status == EventStatus.CANCELLED and reload(world.confirmed).status == EventStatus.CANCELLED
    later_id, confirmed_id = world.later.id, world.confirmed.id
    response = hc.post("/hall/events/action", data={"action": "delete", "event_ids": [later_id, confirmed_id]}, follow_redirects=True)
    assert db.session.get(Event, later_id) is None
    assert db.session.get(Event, confirmed_id) is not None
    assert "۱ مراسم حذف شد" in body(response) and "فقط مراسم پیش‌نویس را می‌توان حذف کرد" in body(response)


def test_bulk_action_validates_its_input(hc, world):
    assert hc.post("/hall/events/action", data={"action": "open"}).status_code == 404
    assert hc.post("/hall/events/action", data={"action": "boom", "event_ids": [world.draft.id]}).status_code == 302
    assert reload(world.draft).status == EventStatus.DRAFT
    assert hc.post("/hall/events/action", data={"action": "open", "event_ids": ["x", "999999"]}).status_code == 404


def test_cancelling_publishes_the_status_event(hc, world, bus):
    hc.post(f"/hall/events/{world.draft.id}/cancel", data={})
    assert ("event.status_changed", {"event_id": world.draft.id, "old_status": "draft", "new_status": "cancelled", "actor_id": world.manager.id}) in bus

