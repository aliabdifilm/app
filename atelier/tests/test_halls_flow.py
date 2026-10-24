"""The business flow through the screens: hall offers dates, ateliers answer, the hall sees it and rates."""

from __future__ import annotations

import sqlalchemy as sa
from test_halls_support import body, day, hc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import EventOffer, Invoice, Notification, Review
from app.services import event_offers
from app.services.constants import EventStatus, OfferMode, OfferStatus


def offer_rows(event):
    db.session.expire_all()
    return list(db.session.scalars(sa.select(EventOffer).where(EventOffer.event_id == event.id).order_by(EventOffer.id)))


def offer_for(event, atelier):
    db.session.expire_all()
    return db.session.scalar(sa.select(EventOffer).where(EventOffer.event_id == event.id, EventOffer.atelier_id == atelier.id))


def notes(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id).order_by(Notification.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt))


# --------------------------------------------------------------------------
# giving dates: the two-step page
# --------------------------------------------------------------------------


def test_step_one_lists_only_dates_that_can_be_offered(hc, world):
    html = body(hc.get("/hall/offers/new"))
    assert f'id="ev-{world.draft.id}"' in html and f'id="ev-{world.later.id}"' in html
    assert f'id="ev-{world.confirmed.id}"' not in html and f'id="ev-{world.past.id}"' not in html  # taken / past
    assert f'id="ev-{world.other_event.id}"' not in html  # somebody else's
    assert 'name="event"' in html


def test_step_two_shows_the_directory_with_rating_city_and_badge(hc, world):
    db.session.add(Review(event_id=world.past.id, atelier_id=world.atelier.id, author_id=world.manager.id, subject_id=world.atelier_manager.id, rating=5))
    db.session.commit()
    html = body(hc.get(f"/hall/offers/new?event={world.draft.id}&event={world.later.id}"))
    assert "۲ مراسم انتخاب شده" in html
    assert "اتلیه ستاره" in html and "اتلیه رقیب" in html
    assert "تأییدشده" in html  # the verified manager
    assert 'name="atelier_id"' in html and 'name="message"' in html
    assert html.index("اتلیه ستاره") < html.index("اتلیه رقیب")  # rated first
    searched = body(hc.get(f"/hall/offers/new?event={world.draft.id}&q=رقیب"))
    assert "اتلیه رقیب" in searched and "اتلیه ستاره" not in searched
    picked = body(hc.get(f"/hall/offers/new?event={world.draft.id}&atelier={world.rival.id}&q=ستاره"))
    assert "انتخاب‌شده" in picked and 'value="%d" checked' % world.rival.id in picked


def test_sending_offers_to_two_ateliers_for_two_dates(hc, world):
    response = hc.post(
        "/hall/offers/new",
        data={"event_id": [world.draft.id, world.later.id], "atelier_id": [world.atelier.id, world.rival.id], "message": "ناهار و شام"},
    )
    assert response.status_code == 302 and response.headers["Location"].endswith("/hall/offers?tab=pending")
    assert len(offer_rows(world.draft)) == 2 and len(offer_rows(world.later)) == 2
    assert all(o.status == OfferStatus.PENDING and o.message == "ناهار و شام" for o in offer_rows(world.draft))
    assert reload(world.draft).status == EventStatus.OFFERED
    assert len(notes(world.atelier_manager, "offer_received")) == 1 and len(notes(world.rival_manager, "offer_received")) == 1
    page = body(hc.get("/hall/offers"))
    assert "اتلیه ستاره" in page and "اتلیه رقیب" in page and "در انتظار پاسخ" in page


def test_a_single_event_offer_returns_to_the_event_page_with_the_offers_listed(hc, world):
    response = hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": world.atelier.id})
    assert response.headers["Location"].endswith(f"/hall/events/{world.draft.id}")
    html = body(hc.get(response.headers["Location"]))
    assert "اتلیه ستاره" in html and "پس گرفتن" in html and "پیشنهاد به ۱ اتلیه" in html


def test_offer_form_errors_do_not_send_anything(hc, world):
    assert hc.post("/hall/offers/new", data={"event_id": world.draft.id}).status_code == 302  # no atelier chosen
    assert hc.post("/hall/offers/new", data={"atelier_id": world.atelier.id}).status_code == 404  # no events
    assert hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": 99999}).status_code == 302
    world.rival.is_active = False
    db.session.commit()
    followed = hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": [world.atelier.id, world.rival.id]}, follow_redirects=True)
    assert "دیگر فعال نیست" in body(followed)
    assert offer_rows(world.draft) == []


def test_inactive_ateliers_are_not_in_the_directory(hc, world):
    world.rival.is_active = False
    db.session.commit()
    assert "اتلیه رقیب" not in body(hc.get(f"/hall/offers/new?event={world.draft.id}"))
    assert hc.get(f"/hall/ateliers/{world.rival.id}").status_code == 404


def test_a_message_is_limited(hc, world):
    followed = hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": world.atelier.id, "message": "ا" * 600}, follow_redirects=True)
    assert "نباید بیش از" in body(followed)
    assert offer_rows(world.draft) == []


# --------------------------------------------------------------------------
# withdraw, re-offer, open to all
# --------------------------------------------------------------------------


def test_withdraw_and_reoffer_from_the_event_page(hc, world):
    event_offers.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offer_rows(world.draft)
    assert hc.post(f"/hall/offers/{offer.id}/withdraw", data={}).status_code == 302
    assert reload(offer).status == OfferStatus.WITHDRAWN and reload(world.draft).status == EventStatus.OPEN
    html = body(hc.get(f"/hall/events/{world.draft.id}"))
    assert "پس گرفته شده" in html and "پیشنهاد دوباره" in html
    assert hc.post(f"/hall/offers/{offer.id}/reoffer", data={}).status_code == 302
    assert reload(offer).status == OfferStatus.PENDING and len(offer_rows(world.draft)) == 1
    twice = hc.post(f"/hall/offers/{offer.id}/reoffer", data={}, follow_redirects=True)
    assert "پیشنهادِ ردشده یا پس‌گرفته‌شده" in body(twice)


def test_open_to_all_and_close_from_the_event_page(hc, world):
    assert hc.post(f"/hall/events/{world.draft.id}/open", data={}).status_code == 302
    event = reload(world.draft)
    assert event.offer_mode == OfferMode.OPEN and event.status == EventStatus.OPEN
    assert "بستن انتشار عمومی" in body(hc.get(f"/hall/events/{world.draft.id}"))
    assert hc.post(f"/hall/events/{world.draft.id}/close-open", data={}).status_code == 302
    assert reload(world.draft).status == EventStatus.DRAFT
    again = hc.post(f"/hall/events/{world.draft.id}/close-open", data={}, follow_redirects=True)
    assert "برای همه باز نیست" in body(again)


def test_the_open_tab_lists_events_open_to_all(hc, world):
    event_offers.open_to_all([world.draft], world.manager)
    db.session.commit()
    html = body(hc.get("/hall/offers?tab=open"))
    assert "مراسم‌های باز برای همه" in html and f"/hall/events/{world.draft.id}" in html
    assert f"/hall/events/{world.later.id}" not in html


# --------------------------------------------------------------------------
# the other side: ateliers answer with their own screens
# --------------------------------------------------------------------------


def test_an_atelier_accepts_on_its_board_and_the_hall_sees_who(hc, world, client_for):
    hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": [world.atelier.id, world.rival.id]})
    mine, theirs = offer_for(world.draft, world.atelier), offer_for(world.draft, world.rival)
    atelier_client = client_for(world.atelier_manager)
    response = atelier_client.post(f"/atelier/offers/{mine.id}/accept", data={})
    assert response.status_code == 302
    event = reload(world.draft)
    assert event.status == EventStatus.CONFIRMED and event.atelier_id == world.atelier.id
    assert reload(theirs).status == OfferStatus.WITHDRAWN
    html = body(hc.get(f"/hall/events/{world.draft.id}"))
    assert "اتلیه ستاره" in html and "پذیرفته شده" in html and "پس گرفته شده" in html
    assert any("قطعی" in n.title for n in notes(world.manager))
    assert "ستاره" in body(hc.get("/hall/offers?tab=accepted"))
    dashboard = body(hc.get("/hall/"))
    assert "پاسخ‌های اخیر" in dashboard and "اتلیه ستاره" in dashboard


def test_an_atelier_declines_and_the_hall_can_offer_again(hc, world, client_for):
    hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": world.atelier.id})
    (offer,) = offer_rows(world.draft)
    assert client_for(world.atelier_manager).post(f"/atelier/offers/{offer.id}/decline", data={}).status_code == 302
    assert reload(offer).status == OfferStatus.DECLINED
    assert any("نپذیرفت" in n.title for n in notes(world.manager, "offer_response"))
    html = body(hc.get("/hall/offers?tab=declined"))
    assert "ردشده" in html and "پیشنهاد دوباره" in html
    assert hc.post(f"/hall/offers/{offer.id}/reoffer", data={}).status_code == 302
    assert reload(offer).status == OfferStatus.PENDING


def test_two_ateliers_race_for_an_open_date_and_the_second_loses(hc, world, client_for):
    hc.post(f"/hall/events/{world.draft.id}/open", data={})
    first = client_for(world.atelier_manager).post(f"/atelier/events/{world.draft.id}/claim", data={"atelier_id": world.atelier.id})
    second = client_for(world.rival_manager).post(f"/atelier/events/{world.draft.id}/claim", data={"atelier_id": world.rival.id})
    assert first.status_code == 302 and second.status_code in (302, 404)
    event = reload(world.draft)
    assert event.atelier_id == world.atelier.id and event.status == EventStatus.CONFIRMED
    accepted = [o for o in offer_rows(world.draft) if o.status == OfferStatus.ACCEPTED]
    assert [o.atelier_id for o in accepted] == [world.atelier.id]
    assert [o.status for o in offer_rows(world.draft)] == [OfferStatus.ACCEPTED]  # the loser left no offer behind


def test_the_event_page_timeline_tells_the_story(hc, world, client_for):
    hc.post("/hall/offers/new", data={"event_id": world.draft.id, "atelier_id": [world.atelier.id, world.rival.id]})
    mine = offer_for(world.draft, world.atelier)
    client_for(world.atelier_manager).post(f"/atelier/offers/{mine.id}/accept", data={})
    html = body(hc.get(f"/hall/events/{world.draft.id}"))
    history = html.split("تاریخچه")[1]
    assert "پیشنهاد به «اتلیه ستاره» فرستاده شد" in history
    assert "«اتلیه ستاره» پیشنهاد را پذیرفت" in history
    assert "مراسم قطعی شد" in history


# --------------------------------------------------------------------------
# outcome and rating
# --------------------------------------------------------------------------


def test_the_outcome_shows_head_counts_and_never_the_ateliers_money(hc, world, make_assignment, make_user):
    for role in ("photographer", "seller"):
        make_assignment(world.past, role=role, user=make_user("staff"), status="confirmed", base_pay=1_234_567)
    invoice = Invoice(event_id=world.past.id, atelier_id=world.atelier.id, income_total=9_876_543, expense_total=1_111_111, net_profit=8_765_432)
    db.session.add(invoice)
    db.session.commit()
    html = body(hc.get(f"/hall/events/{world.past.id}"))
    assert "نتیجهٔ مراسم" in html and "نیروهای قطعی" in html
    assert "۲ از ۴ نفر" in html
    for secret in ("9876543", "۹٬۸۷۶٬۵۴۳", "۱٬۲۳۴٬۵۶۷", "۸٬۷۶۵٬۴۳۲", "1234567"):
        assert secret not in html
    assert "اطلاعات مالی اتلیه برای تالار نمایش داده نمی‌شود" in html


def test_rating_the_atelier_after_the_event(hc, world):
    url = f"/hall/events/{world.past.id}/review"
    assert hc.post(url, data={"rating": "5", "comment": "عالی بودند"}).status_code == 302
    review = db.session.scalar(sa.select(Review).where(Review.event_id == world.past.id))
    assert review.rating == 5 and review.comment == "عالی بودند"
    assert review.author_id == world.manager.id and review.subject_id == world.atelier_manager.id and review.atelier_id == world.atelier.id
    assert review.assignment_id is None
    note = notes(world.atelier_manager, "review_received")
    assert len(note) == 1 and "۵ از ۵" in note[0].body
    # one rating per event: a second submit edits it
    assert hc.post(url, data={"rating": "3", "comment": ""}).status_code == 302
    rows = list(db.session.scalars(sa.select(Review).where(Review.event_id == world.past.id)))
    assert len(rows) == 1 and rows[0].rating == 3 and rows[0].comment is None
    assert len(notes(world.atelier_manager, "review_received")) == 1  # no second notification
    page = body(hc.get(f"/hall/events/{world.past.id}"))
    assert "ویرایش امتیاز" in page and "امتیاز فعلی شما" in page


def test_rating_validation_and_timing(hc, world):
    url = f"/hall/events/{world.past.id}/review"
    for bad in ("0", "6", "abc", ""):
        assert hc.post(url, data={"rating": bad}).status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(Review.id))) == 0
    # not yet held / no atelier
    for event in (world.confirmed, world.draft):
        followed = hc.post(f"/hall/events/{event.id}/review", data={"rating": "4"}, follow_redirects=True)
        assert db.session.scalar(sa.select(sa.func.count(Review.id))) == 0
    followed = hc.post(f"/hall/events/{world.confirmed.id}/review", data={"rating": "4"}, follow_redirects=True)
    assert "پس از برگزاری مراسم" in body(followed)
    page = body(hc.get(f"/hall/events/{world.confirmed.id}"))
    assert "پس از برگزاری مراسم می‌توانید به اتلیه امتیاز بدهید" in page


def test_ratings_feed_the_directory_and_a_hidden_review_does_not_count(hc, world, make_event):
    hc.post(f"/hall/events/{world.past.id}/review", data={"rating": "5"})
    other = make_event(world.hall2, world.atelier, date=day(-2))
    hc.post(f"/hall/events/{other.id}/review", data={"rating": "2"})
    html = body(hc.get("/hall/ateliers"))
    assert "۳٫۵" in html and "۲" in html
    review = db.session.scalar(sa.select(Review).where(Review.event_id == other.id))
    review.is_hidden = True
    db.session.commit()
    assert "۵٫۰" in body(hc.get("/hall/ateliers"))


def test_dashboard_asks_for_missing_ratings(hc, world):
    html = body(hc.get("/hall/"))
    assert "منتظر امتیاز شما" in html and "امتیاز دادن" in html
    hc.post(f"/hall/events/{world.past.id}/review", data={"rating": "4"})
    assert "منتظر امتیاز شما" not in body(hc.get("/hall/"))

