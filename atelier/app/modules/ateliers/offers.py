"""Hall offers: accept, decline and claim (the atelier side of the hall -> atelier hand-over).

Business rules (docs/DATA_MODEL.md, sections 3 and 4):

* a hall hands an event over with an :class:`EventOffer` (``direct``) or by opening it to every
  atelier (``open``, marketplace);
* the atelier that says yes becomes ``event.atelier`` and the event becomes ``confirmed``;
* at most one offer per event can be ``accepted`` (partial unique index);
* the other pending offers of that event are withdrawn.

Taking an event is a *conditional UPDATE* (``WHERE atelier_id IS NULL``), so two ateliers
tapping "accept" at the same moment cannot both win; the loser gets :class:`OfferUnavailable`.
Services here ``flush`` but never ``commit`` (the route owns the transaction).
"""

from __future__ import annotations

from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Atelier, Event, EventOffer
from app.services import audit, notifications, security, staffing
from app.services.constants import (
    OFFER_TRANSITIONS,
    EventStatus,
    NotificationKind,
    OfferMode,
    OfferStatus,
    is_legal_transition,
)

TAKEABLE_STATUSES = (EventStatus.OPEN, EventStatus.OFFERED)


class OfferError(Exception):
    """A rule was violated; ``str(exc)`` is a Persian message safe to flash."""


class OfferUnavailable(OfferError):
    """The event is gone (taken by somebody else, withdrawn, cancelled or in the past)."""


UNAVAILABLE = "این مراسم دیگر در دسترس نیست؛ ممکن است به اتلیهٔ دیگری رسیده یا لغو شده باشد."
PAST = "تاریخ این مراسم گذشته است و دیگر قابل پذیرش نیست."


def _event_label(event: Event) -> str:
    from app.services import jalali

    hall = event.hall.name if event.hall is not None else ""
    return f"{hall} - {jalali.to_jalali_str(event.date, 'long', persian_digits=True)}"


def _check_takeable(event: Event) -> None:
    if event.atelier_id is not None or event.status not in TAKEABLE_STATUSES:
        raise OfferUnavailable(UNAVAILABLE)
    if event.date < security.today():
        raise OfferUnavailable(PAST)


def _take_event(event: Event, atelier: Atelier) -> None:
    """Attach *atelier* to the still-free event, or lose the race."""
    result = db.session.execute(
        sa.update(Event)
        .where(
            Event.id == event.id,
            Event.atelier_id.is_(None),
            Event.status.in_([status.value for status in TAKEABLE_STATUSES]),
        )
        .values(atelier_id=atelier.id)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise OfferUnavailable(UNAVAILABLE)
    db.session.expire(event)


def _finish_accept(event: Event, offer: EventOffer, atelier: Atelier, actor: Any) -> Event:
    """Shared tail of accept / claim: mark the offer, withdraw the others, confirm the event."""
    offer.status = OfferStatus.ACCEPTED
    offer.responded_at = security.now()
    others = list(
        db.session.execute(
            sa.select(EventOffer).where(
                EventOffer.event_id == event.id,
                EventOffer.id != offer.id,
                EventOffer.status == OfferStatus.PENDING,
            )
        ).scalars()
    )
    for other in others:
        other.status = OfferStatus.WITHDRAWN
        other.responded_at = security.now()
    try:
        db.session.flush()
    except IntegrityError:  # another request accepted a different offer of this event first
        raise OfferUnavailable(UNAVAILABLE) from None
    staffing.change_event_status(
        event,
        EventStatus.CONFIRMED,
        actor,
        reason=f"{atelier.name} مراسم را پذیرفت.",
    )
    audit.log(
        actor,
        "offer.accepted",
        offer,
        {"event_id": event.id, "atelier_id": atelier.id, "withdrawn": [other.id for other in others]},
    )
    for other in others:
        notifications.notify(
            other.atelier.manager_id,
            NotificationKind.OFFER_RESPONSE,
            "مراسم پیشنهادی به اتلیهٔ دیگری رسید",
            f"مراسم {_event_label(event)} دیگر در دسترس نیست.",
        )
    return event


def accept_offer(offer: EventOffer, actor: Any) -> Event:
    """The atelier accepts a direct offer: it becomes the event's atelier and the event is confirmed."""
    if offer.status != OfferStatus.PENDING:
        raise OfferError("این پیشنهاد دیگر در انتظار پاسخ نیست.")
    event, atelier = offer.event, offer.atelier
    _check_takeable(event)
    _take_event(event, atelier)
    return _finish_accept(event, offer, atelier, actor)


def decline_offer(offer: EventOffer, actor: Any) -> EventOffer:
    """The atelier refuses a direct offer; the hall manager is told."""
    if offer.status != OfferStatus.PENDING or not is_legal_transition(
        OFFER_TRANSITIONS, offer.status, OfferStatus.DECLINED
    ):
        raise OfferError("این پیشنهاد دیگر در انتظار پاسخ نیست.")
    event = offer.event
    offer.status = OfferStatus.DECLINED
    offer.responded_at = security.now()
    db.session.flush()
    audit.log(actor, "offer.declined", offer, {"event_id": event.id, "atelier_id": offer.atelier_id})
    if event.hall is not None:
        notifications.notify(
            event.hall.manager_id,
            NotificationKind.OFFER_RESPONSE,
            f"{offer.atelier.name} پیشنهاد را نپذیرفت",
            f"مراسم {_event_label(event)} هنوز بدون اتلیه است.",
            entity=event,
        )
    return offer


def claim_open_event(event: Event, atelier: Atelier, actor: Any) -> Event:
    """Take an event the hall opened to every atelier (``offer_mode=open``, status ``open``)."""
    if event.offer_mode != OfferMode.OPEN or event.status != EventStatus.OPEN:
        raise OfferUnavailable(UNAVAILABLE)
    _check_takeable(event)
    offer: Optional[EventOffer] = db.session.execute(
        sa.select(EventOffer).where(EventOffer.event_id == event.id, EventOffer.atelier_id == atelier.id)
    ).scalar_one_or_none()
    if offer is None:
        offer = EventOffer(event=event, atelier=atelier, offered_by_id=audit.actor_id(actor))
        db.session.add(offer)
    elif offer.status == OfferStatus.ACCEPTED:
        raise OfferUnavailable(UNAVAILABLE)
    _take_event(event, atelier)
    db.session.flush()
    return _finish_accept(event, offer, atelier, actor)
