"""The offer workflow service (``app.services.event_offers``): offer, accept, decline, claim, race, exclusivity."""

from __future__ import annotations

import datetime as dt
import inspect

import pytest
import sqlalchemy as sa
from test_halls_support import day, now, reload, world  # noqa: F401

from sqlalchemy.orm import Session

from app.extensions import db
from app.models import AuditLog, Event, EventOffer, HallAgreement, Notification
from app.services import event_offers as eo
from app.services import security, staffing
from app.services.constants import EventStatus, OfferMode, OfferStatus


@pytest.fixture(autouse=True)
def request_context(app):
    """Services build notification links with ``url_for``, which needs a request context (routes have one)."""
    with app.test_request_context("/"):
        yield


def winner_commits(event, atelier, status="confirmed"):
    """Another request wins the race and commits; this session's identity map stays stale on purpose."""
    other = Session(db.engine)
    try:
        other.execute(sa.update(Event).where(Event.id == event.id).values(atelier_id=atelier.id, status=status))
        other.commit()
    finally:
        other.close()


def offers_of(event):
    db.session.expire_all()
    return list(db.session.scalars(sa.select(EventOffer).where(EventOffer.event_id == event.id).order_by(EventOffer.id)))


def notes_for(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt))


def actions(entity_type=None):
    db.session.expire_all()
    stmt = sa.select(AuditLog.action).order_by(AuditLog.id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    return list(db.session.scalars(stmt))


# --------------------------------------------------------------------------
# the stable atelier-side signatures
# --------------------------------------------------------------------------


def test_the_atelier_side_signatures_are_stable():
    assert list(inspect.signature(eo.accept_offer).parameters) == ["offer", "actor"]
    assert list(inspect.signature(eo.decline_offer).parameters) == ["offer", "actor"]
    assert list(inspect.signature(eo.claim_open_event).parameters) == ["event", "atelier", "actor"]


# --------------------------------------------------------------------------
# offering
# --------------------------------------------------------------------------


def test_a_direct_offer_makes_the_event_offered_and_tells_the_atelier(world):
    batch = eo.offer_events([world.draft], [world.atelier], world.manager, message="لطفاً بپذیرید")
    db.session.commit()
    assert batch.created == 1 and batch.renewed == 0 and not batch.skipped
    event = reload(world.draft)
    assert event.status == EventStatus.OFFERED and event.offer_mode == OfferMode.DIRECT and event.atelier_id is None
    (offer,) = offers_of(world.draft)
    assert offer.status == OfferStatus.PENDING and offer.message == "لطفاً بپذیرید"
    assert offer.offered_by_id == world.manager.id and offer.offered_at is not None
    (note,) = notes_for(world.atelier_manager, "offer_received")
    assert "تالار" in note.title and "تالار گل‌ها" in note.body and "لطفاً بپذیرید" in note.body
    assert note.url == "/atelier/events?tab=offers"
    assert "offer.created" in actions("event_offers") and "event.offered" in actions("events")


def test_a_batch_sends_one_notification_per_atelier_not_one_per_date(world):
    batch = eo.offer_events([world.draft, world.later], [world.atelier, world.rival], world.manager)
    db.session.commit()
    assert batch.created == 4
    assert len(notes_for(world.atelier_manager, "offer_received")) == 1
    assert len(notes_for(world.rival_manager, "offer_received")) == 1
    assert "۲ تاریخ" in notes_for(world.atelier_manager)[0].body


def test_offering_twice_does_not_duplicate(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    again = eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    assert again.count == 0 and again.skipped and "در انتظار پاسخ" in again.skipped[0][2]
    assert len(offers_of(world.draft)) == 1
    assert len(notes_for(world.atelier_manager, "offer_received")) == 1


def test_events_that_cannot_be_offered_are_skipped_with_a_reason(world, make_event):
    old = make_event(world.hall, date=day(-2))
    batch = eo.offer_events([world.confirmed, old, world.draft], [world.atelier], world.manager)
    db.session.commit()
    reasons = {event.id: reason for event, _a, reason in batch.skipped}
    assert "رسیده" in reasons[world.confirmed.id]
    assert "گذشته" in reasons[old.id]
    assert batch.count == 1 and batch.offers[0].event_id == world.draft.id


def test_a_hall_manager_cannot_offer_a_foreign_event(world):
    with pytest.raises(eo.OfferNotAllowed):
        eo.offer_events([world.other_event], [world.atelier], world.manager)
    with pytest.raises(eo.OfferNotAllowed):
        eo.offer_events([world.draft], [world.atelier], world.atelier_manager)
    assert offers_of(world.other_event) == []


def test_an_inactive_atelier_or_manager_cannot_be_offered(world):
    world.rival.is_active = False
    db.session.commit()
    with pytest.raises(eo.OfferError):
        eo.offer_events([world.draft], [world.rival], world.manager)
    world.rival.is_active = True
    world.rival_manager.status = "suspended"
    db.session.commit()
    with pytest.raises(eo.OfferError):
        eo.offer_events([world.draft], [world.rival], world.manager)


def test_the_batch_size_is_capped(world, monkeypatch):
    monkeypatch.setattr(eo, "MAX_PAIRS", 3)
    with pytest.raises(eo.OfferError):
        eo.offer_events([world.draft, world.later], [world.atelier, world.rival], world.manager)
    assert offers_of(world.draft) == [] and offers_of(world.later) == []


# --------------------------------------------------------------------------
# withdraw and re-offer
# --------------------------------------------------------------------------


def test_withdrawing_the_last_pending_offer_frees_the_event(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    eo.withdraw_offer(offer, world.manager)
    db.session.commit()
    assert reload(offer).status == OfferStatus.WITHDRAWN
    event = reload(world.draft)
    assert event.status == EventStatus.OPEN and event.offer_mode == OfferMode.NONE
    note = notes_for(world.atelier_manager, "event_status")[-1]
    assert "پس گرفته شد" in note.title
    assert "offer.withdrawn" in actions("event_offers")


def test_withdrawing_one_of_two_offers_keeps_the_event_offered(world):
    eo.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    first, second = offers_of(world.draft)
    eo.withdraw_offer(first, world.manager)
    db.session.commit()
    assert reload(world.draft).status == EventStatus.OFFERED
    assert reload(second).status == OfferStatus.PENDING


def test_only_a_pending_offer_can_be_withdrawn_and_only_by_the_hall(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    with pytest.raises(eo.OfferNotAllowed):
        eo.withdraw_offer(offer, world.atelier_manager)
    eo.withdraw_offer(offer, world.manager)
    with pytest.raises(eo.OfferError):
        eo.withdraw_offer(offer, world.manager)


def test_reoffer_reuses_the_row_and_restarts_the_clock(world, clock):
    eo.offer_events([world.draft], [world.atelier], world.manager, message="اول")
    db.session.commit()
    (offer,) = offers_of(world.draft)
    first_id, first_time = offer.id, offer.offered_at
    eo.decline_offer(offer, world.atelier_manager)
    db.session.commit()
    assert reload(offer).status == OfferStatus.DECLINED and reload(offer).responded_at is not None
    clock.advance(hours=5)
    again = eo.reoffer(reload(offer), world.manager)
    db.session.commit()
    assert again.id == first_id and again.status == OfferStatus.PENDING and again.responded_at is None
    assert again.message == "اول" and again.offered_at > first_time
    assert reload(world.draft).status == EventStatus.OFFERED
    assert len(offers_of(world.draft)) == 1
    assert "offer.reoffered" in actions("event_offers")
    with pytest.raises(eo.OfferError):
        eo.reoffer(again, world.manager)  # pending: nothing to renew


# --------------------------------------------------------------------------
# accept / decline
# --------------------------------------------------------------------------


def test_accepting_confirms_the_event_and_closes_the_other_offers(world):
    eo.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    mine, theirs = offers_of(world.draft)
    event = eo.accept_offer(mine, world.atelier_manager)
    db.session.commit()
    event = reload(world.draft)
    assert event.status == EventStatus.CONFIRMED and event.atelier_id == world.atelier.id
    assert reload(mine).status == OfferStatus.ACCEPTED and reload(mine).responded_at is not None
    assert reload(theirs).status == OfferStatus.WITHDRAWN
    hall_notes = [n.title for n in notes_for(world.manager)]
    assert any("قطعی" in title for title in hall_notes)
    (rival_note,) = notes_for(world.rival_manager, "offer_response")
    assert "اتلیهٔ دیگری" in rival_note.title
    assert {"offer.accepted"} <= set(actions("event_offers"))


def test_the_wrong_atelier_cannot_accept(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    with pytest.raises(eo.OfferNotAllowed):
        eo.accept_offer(offer, world.rival_manager)
    with pytest.raises(eo.OfferNotAllowed):
        eo.accept_offer(offer, world.manager)
    assert reload(world.draft).atelier_id is None


def test_accepting_an_offer_for_a_past_or_cancelled_event_fails(world, make_event):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    staffing.change_event_status(world.draft, EventStatus.CANCELLED, world.manager)
    db.session.commit()
    with pytest.raises(eo.OfferUnavailable):
        eo.accept_offer(offer, world.atelier_manager)


def test_declining_tells_the_hall_and_reopens_the_event_after_the_last_answer(world):
    eo.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    first, second = offers_of(world.draft)
    eo.decline_offer(first, world.atelier_manager)
    db.session.commit()
    assert reload(world.draft).status == EventStatus.OFFERED  # one answer is still missing
    eo.decline_offer(second, world.rival_manager)
    db.session.commit()
    event = reload(world.draft)
    assert event.status == EventStatus.OPEN and event.offer_mode == OfferMode.NONE
    titles = [n.title for n in notes_for(world.manager, "offer_response")]
    assert any("اتلیه ستاره" in t for t in titles) and any("اتلیه رقیب" in t for t in titles)
    with pytest.raises(eo.OfferError):
        eo.decline_offer(first, world.atelier_manager)  # already answered


# --------------------------------------------------------------------------
# the race: exactly one winner
# --------------------------------------------------------------------------


def test_second_accept_loses_when_the_first_won(world):
    eo.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    first, second = offers_of(world.draft)
    eo.accept_offer(first, world.atelier_manager)
    db.session.commit()
    with pytest.raises(eo.OfferError):
        eo.accept_offer(second, world.rival_manager)
    db.session.rollback()
    event = reload(world.draft)
    assert event.atelier_id == world.atelier.id
    assert reload(second).status == OfferStatus.WITHDRAWN


def test_a_stale_accept_loses_at_the_database_not_just_in_memory(world):
    """Both requests read the event as free; the winner's UPDATE lands first, the loser's conditional UPDATE matches no row."""
    eo.offer_events([world.draft], [world.atelier, world.rival], world.manager)
    db.session.commit()
    first, second = offers_of(world.draft)
    event = db.session.get(Event, world.draft.id)
    assert event.atelier_id is None and second.status == OfferStatus.PENDING  # what the loser's request sees
    winner_commits(event, world.atelier)
    assert event.atelier_id is None  # still stale here
    with pytest.raises(eo.OfferUnavailable):
        eo.accept_offer(second, world.rival_manager)
    db.session.rollback()
    assert reload(world.draft).atelier_id == world.atelier.id
    assert reload(second).status == OfferStatus.PENDING  # the loser's offer was not marked accepted


def test_second_claim_loses(world):
    eo.open_to_all([world.draft], world.manager)
    db.session.commit()
    winner = eo.claim_open_event(reload(world.draft), world.atelier, world.atelier_manager)
    db.session.commit()
    assert winner.atelier_id == world.atelier.id and winner.status == EventStatus.CONFIRMED
    with pytest.raises(eo.OfferUnavailable):
        eo.claim_open_event(reload(world.draft), world.rival, world.rival_manager)
    db.session.rollback()
    assert reload(world.draft).atelier_id == world.atelier.id
    accepted = [o for o in offers_of(world.draft) if o.status == OfferStatus.ACCEPTED]
    assert [o.atelier_id for o in accepted] == [world.atelier.id]
    assert [o.atelier_id for o in offers_of(world.draft)] == [world.atelier.id]  # the loser left no row behind


def test_a_stale_claim_loses_at_the_database(world):
    eo.open_to_all([world.draft], world.manager)
    db.session.commit()
    event = db.session.get(Event, world.draft.id)
    assert event.atelier_id is None and event.status == EventStatus.OPEN
    winner_commits(event, world.atelier)
    assert event.atelier_id is None  # still stale here
    with pytest.raises(eo.OfferUnavailable):
        eo.claim_open_event(event, world.rival, world.rival_manager)
    db.session.rollback()
    assert reload(world.draft).atelier_id == world.atelier.id
    assert offers_of(world.draft) == []


def test_only_open_events_can_be_claimed_and_only_by_the_atelier_manager(world):
    with pytest.raises(eo.OfferUnavailable):
        eo.claim_open_event(world.draft, world.atelier, world.atelier_manager)  # draft, not open to all
    eo.open_to_all([world.draft], world.manager)
    db.session.commit()
    with pytest.raises(eo.OfferNotAllowed):
        eo.claim_open_event(reload(world.draft), world.atelier, world.rival_manager)
    with pytest.raises(eo.OfferNotAllowed):
        eo.claim_open_event(reload(world.draft), world.atelier, world.manager)


def test_handing_back_retires_the_accepted_offer_so_the_next_atelier_can_take_it(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    eo.accept_offer(offer, world.atelier_manager)
    db.session.commit()
    staffing.change_event_status(reload(world.draft), EventStatus.OPEN, world.atelier_manager)  # the atelier hands it back
    db.session.commit()
    assert reload(offer).status == OfferStatus.WITHDRAWN
    assert reload(world.draft).atelier_id is None
    eo.open_to_all([reload(world.draft)], world.manager)
    db.session.commit()
    event = eo.claim_open_event(reload(world.draft), world.rival, world.rival_manager)
    db.session.commit()
    assert reload(world.draft).atelier_id == world.rival.id
    accepted = [o for o in offers_of(world.draft) if o.status == OfferStatus.ACCEPTED]
    assert len(accepted) == 1 and accepted[0].atelier_id == world.rival.id
    assert event is not None


def test_cancelling_a_confirmed_event_retires_its_accepted_offer(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    (offer,) = offers_of(world.draft)
    eo.accept_offer(offer, world.atelier_manager)
    db.session.commit()
    staffing.change_event_status(reload(world.draft), EventStatus.CANCELLED, world.manager)
    db.session.commit()
    assert reload(offer).status == OfferStatus.WITHDRAWN


# --------------------------------------------------------------------------
# open to everybody
# --------------------------------------------------------------------------


def test_open_to_all_publishes_and_tells_the_ateliers_of_the_city(world, make_user, make_atelier):
    tehran = make_atelier(make_user("atelier_manager"), name="اتلیه تهران", city="تهران")
    batch = eo.open_to_all([world.draft, world.later], world.manager)
    db.session.commit()
    assert len(batch.opened) == 2 and not batch.skipped
    for event in (world.draft, world.later):
        event = reload(event)
        assert event.status == EventStatus.OPEN and event.offer_mode == OfferMode.OPEN
    assert len(notes_for(world.atelier_manager, "offer_received")) == 1  # one for the batch
    assert len(notes_for(world.rival_manager, "offer_received")) == 1
    assert notes_for(tehran.manager, "offer_received") == []  # other city
    assert notes_for(world.manager, "offer_received") == []
    assert notes_for(world.atelier_manager)[0].url == "/atelier/events?tab=open"


def test_opening_to_all_withdraws_pending_direct_offers(world):
    eo.offer_events([world.draft], [world.atelier], world.manager)
    db.session.commit()
    batch = eo.open_to_all([reload(world.draft)], world.manager)
    db.session.commit()
    assert batch.withdrawn == 1
    (offer,) = offers_of(world.draft)
    assert offer.status == OfferStatus.WITHDRAWN
    assert reload(world.draft).offer_mode == OfferMode.OPEN
    assert any("پس گرفته شد" in n.title for n in notes_for(world.atelier_manager))


def test_close_open_returns_the_event_to_a_draft(world):
    eo.open_to_all([world.draft], world.manager)
    db.session.commit()
    eo.close_open(reload(world.draft), world.manager)
    db.session.commit()
    event = reload(world.draft)
    assert event.status == EventStatus.DRAFT and event.offer_mode == OfferMode.NONE
    with pytest.raises(eo.OfferError):
        eo.close_open(event, world.manager)


def test_a_direct_offer_on_an_open_event_replaces_the_public_listing(world):
    eo.open_to_all([world.draft], world.manager)
    db.session.commit()
    eo.offer_events([reload(world.draft)], [world.atelier], world.manager)
    db.session.commit()
    event = reload(world.draft)
    assert event.offer_mode == OfferMode.DIRECT and event.status == EventStatus.OFFERED


# --------------------------------------------------------------------------
# exclusivity
# --------------------------------------------------------------------------


def exclusive(world, atelier, **fields):
    agreement = HallAgreement(
        hall=world.hall, atelier=atelier, start_date=fields.pop("start", day(-10)), end_date=fields.pop("end", day(60)), is_exclusive=True, **fields
    )
    db.session.add(agreement)
    db.session.commit()
    return agreement


def test_an_exclusive_agreement_reserves_the_dates_for_its_atelier(world):
    exclusive(world, world.atelier)
    batch = eo.offer_events([world.draft], [world.rival], world.manager)
    assert batch.count == 0 and "انحصاری" in batch.skipped[0][2]
    batch = eo.offer_events([world.draft], [world.atelier], world.manager)
    assert batch.count == 1
    db.session.commit()


def test_an_exclusive_agreement_stops_the_other_atelier_from_accepting_or_claiming(world):
    eo.offer_events([world.draft], [world.rival], world.manager)
    db.session.commit()
    (offer,) = offers_of(world.draft)
    exclusive(world, world.atelier)
    with pytest.raises(eo.ExclusivityConflict) as error:
        eo.accept_offer(offer, world.rival_manager)
    assert error.value.holder.id == world.atelier.id
    db.session.rollback()
    assert reload(world.draft).atelier_id is None
    # ...and the holder may take an open event while the rival may not
    event = reload(world.later)
    event.offer_mode, event.status = OfferMode.OPEN, EventStatus.OPEN
    db.session.commit()
    with pytest.raises(eo.ExclusivityConflict):
        eo.claim_open_event(reload(world.later), world.rival, world.rival_manager)
    db.session.rollback()
    eo.claim_open_event(reload(world.later), world.atelier, world.atelier_manager)
    db.session.commit()
    assert reload(world.later).atelier_id == world.atelier.id


def test_exclusivity_only_covers_its_period_and_is_ended_by_termination(world):
    agreement = exclusive(world, world.atelier, start=day(30), end=day(40))
    assert eo.exclusive_agreement_for(world.draft) is None  # day 10 is outside the period
    agreement.start_date = day(0)
    db.session.commit()
    assert eo.exclusive_agreement_for(reload(world.draft)).id == agreement.id
    agreement.terminated_at = security.now()
    db.session.commit()
    assert eo.exclusive_agreement_for(reload(world.draft)) is None


def test_open_to_all_skips_dates_under_an_exclusive_agreement(world):
    exclusive(world, world.atelier)
    batch = eo.open_to_all([world.draft], world.manager)
    assert not batch.opened and "قرارداد انحصاری" in batch.skipped[0][1]
    assert reload(world.draft).offer_mode == OfferMode.NONE


def test_one_atelier_per_day_hall_refuses_a_second_atelier_on_the_same_date(world, make_event):
    world.hall.single_atelier_per_day = True
    db.session.commit()
    lunch = make_event(world.hall, date=day(25), start_time=dt.time(13, 0))
    dinner = make_event(world.hall, date=day(25), start_time=dt.time(19, 0))
    eo.offer_events([lunch], [world.atelier], world.manager)
    (offer,) = offers_of(lunch)
    eo.accept_offer(offer, world.atelier_manager)
    db.session.commit()
    batch = eo.offer_events([dinner], [world.rival], world.manager)
    assert batch.count == 0 and "فقط به یک اتلیه" in batch.skipped[0][2]
    assert eo.offer_events([dinner], [world.atelier], world.manager).count == 1  # the holder may take both services
    db.session.commit()
    # the other hall of the same manager has no such rule
    other = make_event(world.hall2, date=day(25))
    assert eo.offer_events([other], [world.rival], world.manager).count == 1


def test_the_one_atelier_per_day_rule_is_enforced_when_a_pending_offer_is_accepted(world, make_event):
    lunch = make_event(world.hall, date=day(26), start_time=dt.time(13, 0))
    dinner = make_event(world.hall, date=day(26), start_time=dt.time(19, 0))
    eo.offer_events([lunch], [world.atelier], world.manager)
    eo.offer_events([dinner], [world.rival], world.manager)
    db.session.commit()
    world.hall.single_atelier_per_day = True  # switched on after both offers went out
    db.session.commit()
    lunch_offer = offers_of(lunch)[0]
    dinner_offer = offers_of(dinner)[0]
    eo.accept_offer(lunch_offer, world.atelier_manager)
    db.session.commit()
    with pytest.raises(eo.ExclusivityConflict):
        eo.accept_offer(dinner_offer, world.rival_manager)
    db.session.rollback()
    assert reload(dinner).atelier_id is None
