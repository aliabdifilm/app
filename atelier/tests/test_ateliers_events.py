"""Events board: hall offers (accept / decline / claim), calendar, event page, status actions, equipment."""

from __future__ import annotations


import pytest
import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, body, day, mc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AuditLog, Equipment, EquipmentUsage, Event, EventOffer, Invoice, Notification
from app.services import staffing
from app.services.constants import (
    AssignmentStatus,
    EquipmentOwnership,
    EventStatus,
    InvoiceStatus,
    OfferMode,
    OfferStatus,
)


@pytest.fixture
def offer(world, make_event):
    """A direct offer from the hall to my atelier for an event that still has no atelier."""
    event = make_event(world.hall, None, date=day(15), status="offered", offer_mode=OfferMode.DIRECT, notes="مراسم شب")
    row = EventOffer(event=event, atelier=world.atelier, offered_by=world.hall_manager, message="لطفاً زود جواب بدهید")
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def open_event(world, make_event, make_hall):
    return make_event(world.hall, None, date=day(18), status="open", offer_mode=OfferMode.OPEN, title="مراسم ویژه")


def notes_for(user, **where):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id)
    for key, value in where.items():
        stmt = stmt.where(getattr(Notification, key) == value)
    return db.session.scalars(stmt).all()


# --------------------------------------------------------------------------
# direct offers
# --------------------------------------------------------------------------


def test_offers_tab_shows_the_offer_with_its_details(world, mc, offer):
    page = body(mc.get("/atelier/events?tab=offers"))
    assert "تالار گل‌ها" in page and "لطفاً زود جواب بدهید" in page and "مراسم شب" in page
    assert "پذیرش مراسم" in page and "رد پیشنهاد" in page
    assert "منتظر پاسخ شما" in page


def test_offers_tab_is_the_default_when_something_waits(world, mc, offer):
    assert "پذیرش مراسم" in body(mc.get("/atelier/events"))


def test_offer_warns_about_events_the_same_day(world, mc, offer):
    offer.event.date = world.event.date
    db.session.commit()
    assert "مراسم دیگر دارید" in body(mc.get("/atelier/events?tab=offers"))


def test_accepting_an_offer_takes_the_event_and_tells_the_hall(world, mc, offer):
    event_id = offer.event_id
    response = mc.post(f"/atelier/offers/{offer.id}/accept")
    assert response.status_code == 302 and f"/atelier/events/{event_id}" in response.headers["Location"]
    event = db.session.get(Event, event_id)
    db.session.refresh(event)
    assert event.atelier_id == world.atelier.id and event.status == EventStatus.CONFIRMED
    assert reload(offer).status == OfferStatus.ACCEPTED and reload(offer).responded_at is not None
    hall_notes = notes_for(world.hall_manager)
    assert any(world.atelier.name in (n.body or "") for n in hall_notes)
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "offer.accepted")) is not None
    # it is now one of my events, staffable
    assert mc.get(f"/atelier/events/{event_id}").status_code == 200
    assert "افیش نیرو" in body(mc.get(f"/atelier/events/{event_id}"))


def test_accepting_withdraws_the_offers_of_other_ateliers(world, mc, offer):
    rival_offer = EventOffer(event=offer.event, atelier=world.rival, offered_by=world.hall_manager)
    db.session.add(rival_offer)
    db.session.commit()
    mc.post(f"/atelier/offers/{offer.id}/accept")
    assert reload(rival_offer).status == OfferStatus.WITHDRAWN
    assert any("دیگر در دسترس نیست" in n.body for n in notes_for(world.rival_manager, kind="offer_response"))


def test_accepting_twice_changes_nothing_the_second_time(world, mc, offer):
    assert mc.post(f"/atelier/offers/{offer.id}/accept").status_code == 302
    before = len(db.session.scalars(sa.select(AuditLog).where(AuditLog.action == "offer.accepted")).all())
    again = mc.post(f"/atelier/offers/{offer.id}/accept")
    assert again.status_code == 302 and "tab=offers" in again.headers["Location"]
    assert len(db.session.scalars(sa.select(AuditLog).where(AuditLog.action == "offer.accepted")).all()) == before


def test_accepting_an_event_that_was_taken_meanwhile_is_refused(world, mc, offer):
    event = offer.event
    rival_offer = EventOffer(event=event, atelier=world.rival, offered_by=world.hall_manager)
    db.session.add(rival_offer)
    db.session.commit()
    # the rival wins the race (a conditional UPDATE, exactly what the accept flow does)
    from app.modules.ateliers import offers

    offers.accept_offer(rival_offer, world.rival_manager)
    db.session.commit()
    response = mc.post(f"/atelier/offers/{offer.id}/accept")
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(Event, event.id).atelier_id == world.rival.id
    assert db.session.get(EventOffer, offer.id).status != OfferStatus.ACCEPTED


def test_offer_for_a_past_date_cannot_be_accepted(world, mc, offer):
    offer.event.date = day(-2)
    db.session.commit()
    assert mc.post(f"/atelier/offers/{offer.id}/accept").status_code == 302
    assert reload(offer.event).atelier_id is None


def test_declining_an_offer_tells_the_hall_and_keeps_the_event_free(world, mc, offer):
    response = mc.post(f"/atelier/offers/{offer.id}/decline")
    assert response.status_code == 302
    assert reload(offer).status == OfferStatus.DECLINED
    assert reload(offer.event).atelier_id is None
    assert any("نپذیرفت" in n.title for n in notes_for(world.hall_manager))
    assert "پیشنهاد بازی ندارید" in body(mc.get("/atelier/events?tab=offers"))
    # the answer is final for this request: declining again does nothing
    assert mc.post(f"/atelier/offers/{offer.id}/decline").status_code == 302
    assert len(notes_for(world.hall_manager)) == 1


def test_event_page_of_an_offered_event_offers_the_buttons(world, mc, offer):
    page = body(mc.get(f"/atelier/events/{offer.event_id}"))
    assert "پذیرش مراسم" in page and "رد پیشنهاد" in page and "at-slots" not in page  # no staffing before it is mine


# --------------------------------------------------------------------------
# open events: claim
# --------------------------------------------------------------------------


def test_open_events_are_listed_for_my_city_and_all_cities(world, mc, open_event, make_hall, make_event):
    make_event(make_hall(city="تبریز", name="تالار تبریز"), None, date=day(19), status="open", offer_mode=OfferMode.OPEN, title="مراسم تبریز")
    near = body(mc.get("/atelier/events?tab=open"))
    assert "مراسم ویژه" in near
    assert "تالار تبریز" not in near
    assert "تالار تبریز" in body(mc.get("/atelier/events?tab=open&city=all"))


def test_claiming_an_open_event_confirms_it_for_me(world, mc, open_event):
    response = mc.post(f"/atelier/events/{open_event.id}/claim", data={"atelier_id": world.atelier.id})
    assert response.status_code == 302
    event = reload(open_event)
    assert event.atelier_id == world.atelier.id and event.status == EventStatus.CONFIRMED
    offer = db.session.scalar(sa.select(EventOffer).where(EventOffer.event_id == event.id))
    assert offer.status == OfferStatus.ACCEPTED and offer.atelier_id == world.atelier.id
    assert any(world.atelier.name in (n.body or "") for n in notes_for(world.hall_manager))


def test_only_one_atelier_can_claim(world, mc, open_event, client_for):
    assert mc.post(f"/atelier/events/{open_event.id}/claim").status_code == 302
    rival = client_for(world.rival_manager)
    again = rival.post(f"/atelier/events/{open_event.id}/claim")
    assert again.status_code in (302, 404)
    assert reload(open_event).atelier_id == world.atelier.id
    assert len(db.session.scalars(sa.select(EventOffer).where(EventOffer.event_id == open_event.id, EventOffer.status == OfferStatus.ACCEPTED)).all()) == 1


def test_claim_for_somebody_elses_atelier_is_404(world, mc, open_event):
    assert mc.post(f"/atelier/events/{open_event.id}/claim", data={"atelier_id": world.rival.id}).status_code == 404
    assert reload(open_event).atelier_id is None


def test_a_closed_event_cannot_be_claimed(world, mc, make_event):
    private = make_event(world.hall, None, date=day(12), status="draft", offer_mode=OfferMode.NONE)
    assert mc.post(f"/atelier/events/{private.id}/claim").status_code == 404
    full = make_event(world.hall, None, date=day(13), status="open", offer_mode=OfferMode.DIRECT)
    assert mc.post(f"/atelier/events/{full.id}/claim").status_code == 404


def test_declined_open_events_disappear_from_my_open_list(world, mc, open_event):
    db.session.add(EventOffer(event=open_event, atelier=world.atelier, status=OfferStatus.DECLINED))
    db.session.commit()
    assert "مراسم ویژه" not in body(mc.get("/atelier/events?tab=open"))


# --------------------------------------------------------------------------
# lists, calendar
# --------------------------------------------------------------------------


def test_upcoming_and_past_tabs(world, mc):
    past = Event(hall=world.hall, atelier=world.atelier, date=day(-30), status=EventStatus.DONE, title="عروسی قدیمی")
    db.session.add(past)
    db.session.commit()
    up = body(mc.get("/atelier/events?tab=upcoming"))
    assert "عروسی نمونه" in up and "عروسی قدیمی" not in up
    old = body(mc.get("/atelier/events?tab=past"))
    assert "عروسی قدیمی" in old and "برگزار شده" in old and "عروسی نمونه" not in old


def test_pagination_of_my_events(world, mc, make_event):
    for n in range(25):
        make_event(world.hall, world.atelier, date=day(30 + n))
    first = body(mc.get("/atelier/events?tab=upcoming"))
    assert 'aria-label="صفحه‌بندی"' in first
    assert mc.get("/atelier/events?tab=upcoming&page=3").status_code == 200
    assert mc.get("/atelier/events?tab=upcoming&page=99").status_code == 200  # past the end: empty, no 404


def test_calendar_shows_my_events_offers_and_navigation(world, mc, offer):
    month = "/atelier/calendar?y=1404&m=4"  # Tir 1404 = 2025-06-22 .. 2025-07-22
    page = body(mc.get(month))
    assert "تیر ۱۴۰۴" in page and "تالار گل‌ها" in page
    assert "y=1404&amp;m=3" in page and "y=1404&amp;m=5" in page
    assert "مراسم رقیب" not in page and "اتلیه رقیب" not in page
    assert "cal__event--danger" in page  # nobody staffed yet
    # the offer is for day(15) = 2025-07-25, which is in Mordad
    assert "پیشنهاد: تالار گل‌ها" not in page
    mordad = body(mc.get("/atelier/calendar?y=1404&m=5"))
    assert "پیشنهاد: تالار گل‌ها" in mordad and "cal__event--info" in mordad
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    assert "cal__event--danger" in body(mc.get(month))  # still three roles missing


def test_calendar_defaults_to_the_current_jalali_month(world, mc):
    assert "تیر ۱۴۰۴" in body(mc.get("/atelier/calendar"))


# --------------------------------------------------------------------------
# status actions
# --------------------------------------------------------------------------


def test_done_is_only_possible_once_the_day_has_come(world, mc):
    early = mc.post(f"/atelier/events/{world.event.id}/done")
    assert early.status_code == 302
    assert reload(world.event).status == EventStatus.CONFIRMED


def test_done_completes_the_afish(world, mc, clock):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    clock.set("2025-07-18 09:00:00")  # the day after the ceremony
    assert mc.post(f"/atelier/events/{world.event.id}/done").status_code == 302
    assert reload(world.event).status == EventStatus.DONE
    (assignment,) = db.session.scalars(sa.select(Assignment).where(Assignment.event_id == world.event.id)).all()
    assert assignment.status == AssignmentStatus.COMPLETED
    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "افیش فقط برای مشاهده است" in page  # closed: no more staffing


def test_hand_back_clears_the_atelier_and_cancels_the_afish(world, mc):
    staffing.assign_person(world.event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    response = mc.post(f"/atelier/events/{world.event.id}/handback")
    assert response.status_code == 302
    event = reload(world.event)
    assert event.atelier_id is None and event.status == EventStatus.OPEN
    (assignment,) = db.session.scalars(sa.select(Assignment).where(Assignment.event_id == world.event.id)).all()
    assert assignment.status == AssignmentStatus.CANCELLED
    assert mc.get(f"/atelier/events/{world.event.id}").status_code == 404  # no longer mine, and not offered to me
    assert any("لغو" in n.title for n in notes_for(world.staff))


def test_hand_back_is_refused_after_an_invoice_exists(world, mc):
    db.session.add(Invoice(event=world.event, atelier=world.atelier, status=InvoiceStatus.DRAFT))
    db.session.commit()
    mc.post(f"/atelier/events/{world.event.id}/handback")
    assert reload(world.event).atelier_id == world.atelier.id


# --------------------------------------------------------------------------
# equipment and invoice on the event page
# --------------------------------------------------------------------------


@pytest.fixture
def gear(world):
    rented = Equipment(atelier=world.atelier, name="پرینتر اجاره‌ای", kind="printer", ownership=EquipmentOwnership.RENTED, rent_cost_per_event=600_000)
    foreign = Equipment(atelier=world.rival, name="دوربین رقیب", kind="camera")
    db.session.add_all([rented, foreign])
    db.session.commit()
    return rented, foreign


def test_equipment_can_be_attached_with_the_rent_frozen_and_detached(world, mc, gear):
    rented, _ = gear
    assert "پرینتر اجاره‌ای" in body(mc.get(f"/atelier/events/{world.event.id}"))
    assert mc.post(f"/atelier/events/{world.event.id}/equipment", data={"equipment_id": rented.id}).status_code == 302
    usage = db.session.scalar(sa.select(EquipmentUsage))
    assert usage.rent_cost_snapshot == 600_000
    rented.rent_cost_per_event = 900_000
    db.session.commit()
    assert db.session.scalar(sa.select(EquipmentUsage)).rent_cost_snapshot == 600_000  # frozen at attach time
    assert "اجاره ۶۰۰٬۰۰۰ تومان" in body(mc.get(f"/atelier/events/{world.event.id}"))
    assert mc.post(f"/atelier/events/{world.event.id}/equipment/{rented.id}/remove").status_code == 302
    assert db.session.scalar(sa.select(EquipmentUsage)) is None


def test_foreign_equipment_is_404(world, mc, gear):
    _, foreign = gear
    assert mc.post(f"/atelier/events/{world.event.id}/equipment", data={"equipment_id": foreign.id}).status_code == 404
    assert mc.post(f"/atelier/events/{world.event.id}/equipment/{foreign.id}/remove").status_code == 404


def test_event_page_shows_invoice_status_and_links_to_accounting(world, mc, app):
    @app.get("/accounting/events/<int:event_id>/invoice")
    def invoice_stub(event_id):  # stands in for the accounting module
        return "stub"

    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "فاکتور مراسم" in page and "فاکتور را پس از برگزاری" in page  # not held yet
    db.session.add(
        Invoice(
            event=world.event,
            atelier=world.atelier,
            status=InvoiceStatus.SUBMITTED,
            income_total=5_000_000,
            expense_total=3_000_000,
            net_profit=2_000_000,
        )
    )
    db.session.commit()
    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "ثبت شده" in page and "۵٬۰۰۰٬۰۰۰" in page and "۲٬۰۰۰٬۰۰۰" in page
    assert f"/accounting/events/{world.event.id}/invoice" in page


def test_negative_profit_is_shown_as_a_loss(world, mc):
    db.session.add(Invoice(event=world.event, atelier=world.atelier, status=InvoiceStatus.SUBMITTED, income_total=1, expense_total=500, net_profit=-499))
    db.session.commit()
    assert "(زیان)" in body(mc.get(f"/atelier/events/{world.event.id}"))
