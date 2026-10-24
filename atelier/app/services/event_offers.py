"""Offers: how a hall hands events to ateliers and how an atelier takes them.

This is the single place that changes :class:`~app.models.EventOffer` rows and moves an
event between "free" and "taken".  The ``halls`` module (hall side) and the ``ateliers``
module (atelier side) both call it, so the rules cannot drift.

The business flow (AGENTS.md, step 1): the hall manager gives the dates of the ceremonies to
an atelier manager - either **directly** to chosen ateliers or **open to everybody** (they claim
the date on their events board).  The atelier that says yes becomes ``event.atelier`` and the
event becomes ``confirmed``.

Public API (the signatures of the atelier side are stable)
----------------------------------------------------------
Atelier side::

    accept_offer(offer, actor)                -> Event        a pending direct offer is accepted
    decline_offer(offer, actor)               -> EventOffer
    claim_open_event(event, atelier, actor)   -> Event        take an event the hall opened to all

Hall side::

    offer_events(events, ateliers, actor, message=None) -> OfferBatch   direct offers (new or renewed)
    open_to_all(events, actor)                -> OpenBatch    publish on every atelier's board
    close_open(event, actor)                  -> Event        stop publishing
    withdraw_offer(offer, actor)              -> EventOffer   take a pending offer back
    reoffer(offer, actor, message=None)       -> EventOffer   offer again after a decline / withdrawal
    withdraw_pending(event, actor, reason)    -> int          used when the event is cancelled

Rules
-----
* **Exactly one winner.**  Taking an event is a *conditional UPDATE*
  (``WHERE atelier_id IS NULL AND status IN (open, offered)``) inside a SAVEPOINT, so two
  ateliers tapping "accept" at the same moment cannot both win; the loser gets
  :class:`OfferUnavailable` and nothing of theirs is written.  A partial unique index also
  allows at most one ``accepted`` offer per event.
* **Exclusivity.**  While an *exclusive* :class:`~app.models.HallAgreement` covers the date of
  an event, only its atelier may be offered the event or take it; a hall with
  ``single_atelier_per_day`` gives one atelier only per date.  See :func:`check_exclusivity`.
* **Status history.**  Every change is written to the audit log (``offer.created``,
  ``offer.reoffered``, ``offer.withdrawn``, ``offer.accepted``, ``offer.declined``,
  ``offer.claimed``, ``event.opened`` ...), which the hall's event page shows as a timeline.
* **Notifications.**  The other side is told about everything that concerns it; one batch
  produces one notification per atelier manager, not one per date.

Like every service here nothing is committed: the caller owns the transaction and rolls back
when an :class:`OfferError` escapes.  Every :class:`OfferError` message is Persian and safe to
flash.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import sqlalchemy as sa
from flask import url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.routing import BuildError

from app.extensions import db
from app.models import Atelier, Event, EventOffer, HallAgreement, User
from app.services import audit, eventbus, jalali, notifications, security, staffing
from app.services.constants import (
    EVENT_TRANSITIONS,
    OFFER_TRANSITIONS,
    EventStatus,
    NotificationKind,
    OfferMode,
    OfferStatus,
    Role,
    UserStatus,
    is_legal_transition,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TAKEABLE_STATUSES",
    "OFFERABLE_STATUSES",
    "MAX_PAIRS",
    "OfferError",
    "OfferUnavailable",
    "OfferNotAllowed",
    "ExclusivityConflict",
    "OfferBatch",
    "OpenBatch",
    "exclusive_agreement_for",
    "check_exclusivity",
    "offer_events",
    "open_to_all",
    "close_open",
    "withdraw_offer",
    "reoffer",
    "withdraw_pending",
    "release_accepted",
    "accept_offer",
    "decline_offer",
    "claim_open_event",
]

#: Statuses in which an atelier may still take the event (accept or claim).
TAKEABLE_STATUSES = (EventStatus.OPEN, EventStatus.OFFERED)
#: Statuses in which the hall may still offer the event.
OFFERABLE_STATUSES = (EventStatus.DRAFT, EventStatus.OPEN, EventStatus.OFFERED)
#: Upper bound of ``len(events) * len(ateliers)`` in one call.
MAX_PAIRS = 600
#: Upper bound of ateliers notified by one "open to all" call.
MAX_BROADCAST = 200

UNAVAILABLE = "این مراسم دیگر در دسترس نیست؛ ممکن است به اتلیهٔ دیگری رسیده یا لغو شده باشد."
PAST = "تاریخ این مراسم گذشته است و دیگر قابل پذیرش نیست."
NOT_PENDING = "این پیشنهاد دیگر در انتظار پاسخ نیست."
FORBIDDEN = "اجازهٔ انجام این کار را ندارید."


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class OfferError(Exception):
    """A rule was violated; ``str(exc)`` is a Persian message safe to flash."""


class OfferUnavailable(OfferError):
    """The event is gone (taken by somebody else, withdrawn, cancelled or in the past)."""


class OfferNotAllowed(OfferError):
    """The actor does not manage the hall / atelier concerned."""


class ExclusivityConflict(OfferError):
    """An exclusive agreement or the hall's one-atelier-per-day rule forbids this atelier.

    ``holder`` is the atelier that holds the exclusivity (a hall manager's screen may name it;
    never show it to the other atelier).
    """

    def __init__(self, message: str, holder: Optional[Atelier] = None) -> None:
        super().__init__(message)
        self.holder = holder


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _actor_user(actor: Any) -> Optional[User]:
    """The acting User row, ``None`` for the system; suspended / unknown accounts are refused."""
    if actor is None:
        return None
    uid = audit.actor_id(actor)
    if uid is None:
        raise OfferNotAllowed(FORBIDDEN)
    user = db.session.get(User, uid)
    if user is None or user.status == UserStatus.SUSPENDED:
        raise OfferNotAllowed(FORBIDDEN)
    return user


def _is_admin(user: Optional[User]) -> bool:
    return user is not None and user.role == Role.ADMIN


def _require_hall_manager(user: Optional[User], event: Event) -> None:
    if user is None or _is_admin(user):
        return
    if event.hall is None or event.hall.manager_id != user.id:
        raise OfferNotAllowed(FORBIDDEN)


def _require_atelier_manager(user: Optional[User], atelier: Atelier) -> None:
    if user is None or _is_admin(user):
        return
    if atelier.manager_id != user.id:
        raise OfferNotAllowed(FORBIDDEN)


def _url(endpoint: str, **values: Any) -> Optional[str]:
    """Site-relative URL of a route, ``None`` when that module is not loaded."""
    try:
        return url_for(endpoint, **values)
    except (BuildError, RuntimeError):
        return None


def _day(value: dt.date) -> str:
    return jalali.to_jalali_str(value, "long", persian_digits=True)


def _event_label(event: Event) -> str:
    hall = event.hall.name if event.hall is not None else ""
    return f"{hall} - {_day(event.date)}"


def _fa(number: int) -> str:
    from app.services.money import to_persian_digits

    return to_persian_digits(str(number))


def _safe_notify(*args: Any, **kwargs: Any) -> None:
    """A notification must never break the change it announces."""
    try:
        with db.session.begin_nested():
            notifications.notify(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("could not create an offer notification")


def _set_event_status(event: Event, new: EventStatus, actor_user: Optional[User], reason: str) -> None:
    """Move *event* along ``EVENT_TRANSITIONS`` without notifying anybody.

    The offer workflow announces its own changes (one notification per atelier manager), so the
    chatty :func:`staffing.change_event_status` is only used where its cascade matters
    (confirming, cancelling).  Audit and the ``event.status_changed`` bus event are kept.
    """
    old = event.status
    if old == new:
        return
    if not is_legal_transition(EVENT_TRANSITIONS, old, new):
        raise OfferError("این تغییر وضعیت برای مراسم در حالت فعلی مجاز نیست.")
    event.status = new
    db.session.flush()
    audit.log(actor_user, f"event.{new.value}", event, {"from": old, "to": new, "reason": reason, "via": "offer"})
    eventbus.publish(
        eventbus.EVENT_STATUS_CHANGED,
        event_id=event.id,
        old_status=old.value,
        new_status=new.value,
        actor_id=actor_user.id if actor_user is not None else None,
    )


def _pending_offers(event: Event) -> list[EventOffer]:
    return list(
        db.session.execute(
            sa.select(EventOffer).where(EventOffer.event_id == event.id, EventOffer.status == OfferStatus.PENDING)
        ).scalars()
    )


def _settle_after_pending_removed(event: Event, actor_user: Optional[User], reason: str) -> None:
    """No pending offer left on an ``offered`` event: it is free again (``open``, nobody asked)."""
    if event.atelier_id is not None or event.status != EventStatus.OFFERED:
        return
    if _pending_offers(event):
        return
    if event.offer_mode == OfferMode.DIRECT:
        event.offer_mode = OfferMode.NONE
    _set_event_status(event, EventStatus.OPEN, actor_user, reason)


# --------------------------------------------------------------------------
# exclusivity
# --------------------------------------------------------------------------


def exclusive_agreement_for(event: Event) -> Optional[HallAgreement]:
    """The active exclusive agreement of the event's hall that covers the event date, if any."""
    return db.session.execute(
        sa.select(HallAgreement)
        .where(
            HallAgreement.hall_id == event.hall_id,
            HallAgreement.is_exclusive.is_(True),
            HallAgreement.active_on(event.date),
        )
        .order_by(HallAgreement.start_date.desc(), HallAgreement.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True)
class _Exclusivity:
    """What restricts *event* for ateliers, computed once per event (a batch asks per atelier)."""

    agreement: Optional[HallAgreement]
    single_per_day: bool
    holders: frozenset[int]  # ateliers holding other events of the hall on that date (if single_per_day)

    def conflict(self, atelier: Atelier) -> Optional[ExclusivityConflict]:
        if self.agreement is not None and self.agreement.atelier_id != atelier.id:
            return ExclusivityConflict(
                "این تالار برای این تاریخ با اتلیهٔ دیگری قرارداد انحصاری دارد.", holder=self.agreement.atelier
            )
        rivals = self.holders - {atelier.id}
        if self.single_per_day and rivals:
            return ExclusivityConflict(
                "این تالار هر روز را فقط به یک اتلیه می‌دهد و این تاریخ پیشتر به اتلیهٔ دیگری رسیده است.",
                holder=db.session.get(Atelier, min(rivals)),
            )
        return None


def _exclusivity(event: Event) -> _Exclusivity:
    hall = event.hall
    single = bool(hall is not None and hall.single_atelier_per_day)
    holders: frozenset[int] = frozenset()
    if single:
        holders = frozenset(
            db.session.execute(
                sa.select(Event.atelier_id).where(
                    Event.hall_id == event.hall_id,
                    Event.date == event.date,
                    Event.id != event.id,
                    Event.status != EventStatus.CANCELLED,
                    Event.atelier_id.is_not(None),
                )
            ).scalars()
        )
    return _Exclusivity(exclusive_agreement_for(event), single, holders)


def check_exclusivity(event: Event, atelier: Atelier) -> None:
    """Raise :class:`ExclusivityConflict` when *atelier* may not hold *event*.

    1. an active exclusive agreement of another atelier covers the date of the event;
    2. the hall grants a date to one atelier only (``Hall.single_atelier_per_day``) and a
       different atelier already holds a (non-cancelled) event of that hall on that date.
    """
    conflict = _exclusivity(event).conflict(atelier)
    if conflict is not None:
        raise conflict


# --------------------------------------------------------------------------
# hall side: offering
# --------------------------------------------------------------------------


@dataclass
class OfferBatch:
    """Outcome of :func:`offer_events`."""

    offers: list[EventOffer] = field(default_factory=list)  # new or renewed, pending
    created: int = 0
    renewed: int = 0
    skipped: list[tuple[Event, Atelier, str]] = field(default_factory=list)  # (event, atelier, reason)

    @property
    def count(self) -> int:
        return len(self.offers)


@dataclass
class OpenBatch:
    """Outcome of :func:`open_to_all`."""

    opened: list[Event] = field(default_factory=list)
    skipped: list[tuple[Event, str]] = field(default_factory=list)
    withdrawn: int = 0  # pending direct offers taken back because the event went public
    notified: int = 0


def _offerable_reason(event: Event) -> Optional[str]:
    """Why *event* cannot be offered (``None`` when it can)."""
    if event.atelier_id is not None:
        return "این مراسم پیشتر به یک اتلیه رسیده است."
    if event.status not in OFFERABLE_STATUSES:
        return f"مراسمِ «{event.status.label}» را نمی‌توان پیشنهاد داد."
    if event.date < security.today():
        return "تاریخ این مراسم گذشته است."
    return None


def _format_dates(events: Sequence[Event]) -> str:
    days = sorted({event.date for event in events})
    if len(days) == 1:
        return _day(days[0])
    return f"{_fa(len(days))} تاریخ، از {_day(days[0])} تا {_day(days[-1])}"


def offer_events(
    events: Iterable[Event],
    ateliers: Iterable[Atelier],
    actor: Any,
    *,
    message: Optional[str] = None,
) -> OfferBatch:
    """Offer every event to every atelier (direct offers, status ``pending``).

    * a declined or withdrawn offer of the same pair is **renewed** (same row, history in the
      audit log); a pending one is left alone;
    * an event that cannot be offered (taken, cancelled, in the past) or that an exclusive
      agreement reserves for another atelier is *skipped* with a reason, not an error;
    * the events become ``offered`` / ``direct`` (an event that was open to everybody no longer
      is);
    * each atelier manager gets **one** notification for the whole batch.

    Raises :class:`OfferNotAllowed` when an event is not in the actor's hall.
    """
    actor_user = _actor_user(actor)
    event_list = list(dict.fromkeys(events))
    atelier_list = list(dict.fromkeys(ateliers))
    if len(event_list) * len(atelier_list) > MAX_PAIRS:
        raise OfferError("در هر بار حداکثر ۶۰۰ پیشنهاد (تعداد مراسم × تعداد اتلیه) ثبت می‌شود؛ کمتر انتخاب کنید.")
    text = (message or "").strip() or None
    batch = OfferBatch()
    for event in event_list:
        _require_hall_manager(actor_user, event)
    for atelier in atelier_list:
        if not atelier.is_active or atelier.manager is None or atelier.manager.status != UserStatus.ACTIVE:
            raise OfferError(f"اتلیهٔ «{atelier.name}» فعال نیست و پیشنهاد نمی‌گیرد.")

    now = security.now()
    touched: dict[int, list[Event]] = {}
    existing: dict[tuple[int, int], EventOffer] = {}
    if event_list and atelier_list:
        for row in db.session.execute(
            sa.select(EventOffer).where(
                EventOffer.event_id.in_([e.id for e in event_list]),
                EventOffer.atelier_id.in_([a.id for a in atelier_list]),
            )
        ).scalars():
            existing[(row.event_id, row.atelier_id)] = row
    for event in event_list:
        reason = _offerable_reason(event)
        restrictions = _exclusivity(event) if reason is None else None
        for atelier in atelier_list:
            if reason is not None:
                batch.skipped.append((event, atelier, reason))
                continue
            conflict = restrictions.conflict(atelier) if restrictions is not None else None
            if conflict is not None:
                batch.skipped.append((event, atelier, str(conflict)))
                continue
            offer = existing.get((event.id, atelier.id))
            if offer is not None and offer.status == OfferStatus.PENDING:
                batch.skipped.append((event, atelier, "این پیشنهاد از قبل در انتظار پاسخ است."))
                continue
            if offer is None:
                offer = EventOffer(
                    event=event,
                    atelier=atelier,
                    offered_by_id=actor_user.id if actor_user else None,
                    message=text,
                    offered_at=now,
                )
                db.session.add(offer)
                action, key = "offer.created", "created"
            else:
                if not is_legal_transition(OFFER_TRANSITIONS, offer.status, OfferStatus.PENDING):
                    batch.skipped.append((event, atelier, "این پیشنهاد را نمی‌توان دوباره فرستاد."))
                    continue
                offer.status = OfferStatus.PENDING
                offer.message = text
                offer.offered_by_id = actor_user.id if actor_user else None
                offer.offered_at = now
                offer.responded_at = None
                action, key = "offer.reoffered", "renewed"
            try:
                with db.session.begin_nested():
                    db.session.flush()
            except IntegrityError:  # a concurrent request created the same pair first
                batch.skipped.append((event, atelier, "این پیشنهاد از قبل ثبت شده است."))
                continue
            existing[(event.id, atelier.id)] = offer
            audit.log(actor_user, action, offer, {"event_id": event.id, "atelier_id": atelier.id})
            batch.offers.append(offer)
            setattr(batch, key, getattr(batch, key) + 1)
            touched.setdefault(atelier.id, []).append(event)

    # the events that received at least one offer are "offered" (and no longer open to everybody)
    for event in dict.fromkeys(e for events_ in touched.values() for e in events_):
        event.offer_mode = OfferMode.DIRECT
        _set_event_status(event, EventStatus.OFFERED, actor_user, "پیشنهاد مستقیم به اتلیه")

    for atelier in atelier_list:
        offered = touched.get(atelier.id)
        if not offered:
            continue
        hall_name = offered[0].hall.name if offered[0].hall is not None else "تالار"
        body = f"{hall_name}: {_format_dates(offered)}"
        if text:
            body += f"\n«{text}»"
        _safe_notify(
            atelier.manager_id,
            NotificationKind.OFFER_RECEIVED,
            "پیشنهاد مراسم جدید از تالار",
            body,
            _url("ateliers.events", tab="offers"),
        )
    return batch


def reoffer(offer: EventOffer, actor: Any, *, message: Optional[str] = None) -> EventOffer:
    """Offer again after the atelier declined or the hall withdrew (same row, new round)."""
    if offer.status not in (OfferStatus.DECLINED, OfferStatus.WITHDRAWN):
        raise OfferError("فقط پیشنهادِ ردشده یا پس‌گرفته‌شده را می‌توان دوباره فرستاد.")
    batch = offer_events([offer.event], [offer.atelier], actor, message=message if message is not None else offer.message)
    if not batch.offers:
        reason = batch.skipped[0][2] if batch.skipped else "پیشنهاد دوباره ثبت نشد."
        raise OfferError(reason)
    return batch.offers[0]


def withdraw_offer(offer: EventOffer, actor: Any, *, reason: Optional[str] = None, notify: bool = True) -> EventOffer:
    """The hall takes a **pending** offer back; the atelier manager is told."""
    actor_user = _actor_user(actor)
    event = offer.event
    _require_hall_manager(actor_user, event)
    if offer.status != OfferStatus.PENDING:
        raise OfferError(NOT_PENDING)
    offer.status = OfferStatus.WITHDRAWN
    offer.responded_at = None
    db.session.flush()
    audit.log(actor_user, "offer.withdrawn", offer, {"event_id": event.id, "atelier_id": offer.atelier_id, "reason": reason})
    if notify:
        _safe_notify(
            offer.atelier.manager_id,
            NotificationKind.EVENT_STATUS,
            "پیشنهاد مراسم پس گرفته شد",
            f"تالار پیشنهاد مراسم {_event_label(event)} را پس گرفت." + (f"\n{reason}" if reason else ""),
            _url("ateliers.events", tab="offers"),
        )
    _settle_after_pending_removed(event, actor_user, "پیشنهاد پس گرفته شد")
    return offer


def withdraw_pending(event: Event, actor: Any, reason: Optional[str] = None) -> int:
    """Withdraw every pending offer of *event* (event cancelled, or published to everybody).

    Returns how many offers were withdrawn.  The event keeps its status: the caller decides.
    """
    actor_user = _actor_user(actor)
    _require_hall_manager(actor_user, event)
    count = 0
    for offer in _pending_offers(event):
        offer.status = OfferStatus.WITHDRAWN
        offer.responded_at = None
        db.session.flush()
        audit.log(actor_user, "offer.withdrawn", offer, {"event_id": event.id, "atelier_id": offer.atelier_id, "reason": reason})
        _safe_notify(
            offer.atelier.manager_id,
            NotificationKind.EVENT_STATUS,
            "پیشنهاد مراسم پس گرفته شد",
            f"پیشنهاد مراسم {_event_label(event)} دیگر معتبر نیست." + (f"\n{reason}" if reason else ""),
            _url("ateliers.events", tab="offers"),
        )
        count += 1
    return count


def release_accepted(event: Event, actor: Any = None) -> int:
    """Retire the ``accepted`` offer of an event whose atelier left (cancelled, or handed back).

    The offer stays in the history as ``withdrawn``; without this the "one accepted offer per
    event" index would stop the next atelier from taking the event.  Called by the ``halls``
    event-bus handler whenever a confirmed event goes back to ``open`` / ``draft`` or is cancelled.
    Returns how many offers were changed.
    """
    actor_user = _actor_user(actor)
    count = 0
    for offer in db.session.execute(
        sa.select(EventOffer).where(EventOffer.event_id == event.id, EventOffer.status == OfferStatus.ACCEPTED)
    ).scalars():
        offer.status = OfferStatus.WITHDRAWN
        db.session.flush()
        audit.log(actor_user, "offer.released", offer, {"event_id": event.id, "atelier_id": offer.atelier_id})
        count += 1
    return count


def _broadcast_targets(city: str) -> list[Atelier]:
    """Active ateliers (with an active manager) of the hall's city, newest first, capped."""
    if not city:
        return []
    return list(
        db.session.execute(
            sa.select(Atelier)
            .join(User, User.id == Atelier.manager_id)
            .where(Atelier.is_active.is_(True), Atelier.city == city, User.status == UserStatus.ACTIVE)
            .order_by(Atelier.id.desc())
            .limit(MAX_BROADCAST)
        ).scalars()
    )


def open_to_all(events: Iterable[Event], actor: Any) -> OpenBatch:
    """Publish events on the events board of every atelier (they claim them first come, first served).

    * pending direct offers of these events are withdrawn (the atelier managers are told);
    * a date reserved by an exclusive agreement, or already held by an atelier in a hall with
      ``single_atelier_per_day``, is skipped with a reason;
    * ateliers of the hall's city get **one** notification per call.
    """
    actor_user = _actor_user(actor)
    batch = OpenBatch()
    event_list = list(dict.fromkeys(events))
    for event in event_list:
        _require_hall_manager(actor_user, event)
    for event in event_list:
        reason = _offerable_reason(event)
        if reason is None and exclusive_agreement_for(event) is not None:
            reason = "برای این تاریخ قرارداد انحصاری فعال است؛ مراسم را مستقیم به همان اتلیه پیشنهاد دهید."
        if reason is None and event.hall is not None and event.hall.single_atelier_per_day:
            held = db.session.execute(
                sa.select(Event.id)
                .where(
                    Event.hall_id == event.hall_id,
                    Event.date == event.date,
                    Event.id != event.id,
                    Event.status != EventStatus.CANCELLED,
                    Event.atelier_id.is_not(None),
                )
                .limit(1)
            ).first()
            if held is not None:
                reason = "این تالار هر روز را به یک اتلیه می‌دهد و این تاریخ پیشتر واگذار شده است."
        if reason is not None:
            batch.skipped.append((event, reason))
            continue
        batch.withdrawn += withdraw_pending(event, actor_user, "مراسم برای همهٔ اتلیه‌ها باز شد")
        event.offer_mode = OfferMode.OPEN
        _set_event_status(event, EventStatus.OPEN, actor_user, "باز برای همهٔ اتلیه‌ها")
        db.session.flush()
        audit.log(actor_user, "event.opened", event, {"mode": "open"})
        batch.opened.append(event)

    by_hall: dict[int, list[Event]] = {}
    for event in batch.opened:
        by_hall.setdefault(event.hall_id, []).append(event)
    for hall_events in by_hall.values():
        hall = hall_events[0].hall
        body = f"{hall.name}: {_format_dates(hall_events)}"
        for atelier in _broadcast_targets(hall.city):
            _safe_notify(
                atelier.manager_id,
                NotificationKind.OFFER_RECEIVED,
                "مراسم باز جدید برای اتلیه‌ها",
                body,
                _url("ateliers.events", tab="open"),
            )
            batch.notified += 1
    return batch


def close_open(event: Event, actor: Any) -> Event:
    """Stop publishing an event on the public board (``open`` / ``open`` becomes ``draft`` / ``none``)."""
    actor_user = _actor_user(actor)
    _require_hall_manager(actor_user, event)
    if event.atelier_id is not None or event.offer_mode != OfferMode.OPEN or event.status != EventStatus.OPEN:
        raise OfferError("این مراسم برای همه باز نیست.")
    event.offer_mode = OfferMode.NONE
    _set_event_status(event, EventStatus.DRAFT, actor_user, "پایان انتشار عمومی")
    db.session.flush()
    audit.log(actor_user, "event.open_closed", event, {})
    return event


# --------------------------------------------------------------------------
# atelier side: accept / decline / claim
# --------------------------------------------------------------------------


def _check_takeable(event: Event) -> None:
    if event.atelier_id is not None or event.status not in TAKEABLE_STATUSES:
        raise OfferUnavailable(UNAVAILABLE)
    if event.date < security.today():
        raise OfferUnavailable(PAST)


def _take_event(event: Event, atelier: Atelier) -> None:
    """Attach *atelier* to the still-free event, or lose the race (:class:`OfferUnavailable`)."""
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


def _finish_take(event: Event, offer: EventOffer, atelier: Atelier, actor_user: Optional[User], *, claimed: bool) -> Event:
    """Shared tail of accept / claim: mark the offer, withdraw the others, confirm the event."""
    now = security.now()
    offer.status = OfferStatus.ACCEPTED
    offer.responded_at = now
    # The event was free (the conditional UPDATE proved it), so an ``accepted`` row can only be a
    # leftover of an atelier that handed the event back: retire it, or the "one accepted offer per
    # event" index would refuse the new winner.
    others = [
        other
        for other in db.session.execute(
            sa.select(EventOffer).where(
                EventOffer.event_id == event.id,
                EventOffer.id != offer.id,
                EventOffer.status.in_([OfferStatus.PENDING, OfferStatus.ACCEPTED]),
            )
        ).scalars()
    ]
    notify_others = [other for other in others if other.status == OfferStatus.PENDING]
    for other in others:
        other.status = OfferStatus.WITHDRAWN
        other.responded_at = now
    try:
        db.session.flush()
    except IntegrityError:  # another request accepted a different offer of this event first
        raise OfferUnavailable(UNAVAILABLE) from None
    staffing.change_event_status(
        event,
        EventStatus.CONFIRMED,
        actor_user,
        reason=f"{atelier.name} مراسم را {'برداشت' if claimed else 'پذیرفت'}.",
    )
    audit.log(
        actor_user,
        "offer.claimed" if claimed else "offer.accepted",
        offer,
        {"event_id": event.id, "atelier_id": atelier.id, "withdrawn": [other.id for other in others]},
    )
    for other in notify_others:
        _safe_notify(
            other.atelier.manager_id,
            NotificationKind.OFFER_RESPONSE,
            "مراسم پیشنهادی به اتلیهٔ دیگری رسید",
            f"مراسم {_event_label(event)} دیگر در دسترس نیست.",
        )
    return event


def accept_offer(offer: EventOffer, actor: Any) -> Event:
    """The atelier accepts a direct offer: it becomes the event's atelier and the event is confirmed.

    Raises :class:`OfferUnavailable` when somebody else got there first (the event is taken,
    cancelled, or in the past) and :class:`ExclusivityConflict` when an exclusive agreement
    forbids this atelier.  Nothing is written in that case.
    """
    actor_user = _actor_user(actor)
    atelier = offer.atelier
    _require_atelier_manager(actor_user, atelier)
    if offer.status != OfferStatus.PENDING:
        raise OfferError(NOT_PENDING)
    event = offer.event
    _check_takeable(event)
    check_exclusivity(event, atelier)
    with db.session.begin_nested():
        _take_event(event, atelier)
        return _finish_take(event, offer, atelier, actor_user, claimed=False)


def decline_offer(offer: EventOffer, actor: Any) -> EventOffer:
    """The atelier refuses a direct offer; the hall manager is told.

    When this was the last pending offer of an ``offered`` event, the event becomes ``open``
    again (``offered -> open``, nobody is being asked).
    """
    actor_user = _actor_user(actor)
    _require_atelier_manager(actor_user, offer.atelier)
    if offer.status != OfferStatus.PENDING or not is_legal_transition(
        OFFER_TRANSITIONS, offer.status, OfferStatus.DECLINED
    ):
        raise OfferError(NOT_PENDING)
    event = offer.event
    offer.status = OfferStatus.DECLINED
    offer.responded_at = security.now()
    db.session.flush()
    audit.log(actor_user, "offer.declined", offer, {"event_id": event.id, "atelier_id": offer.atelier_id})
    if event.hall is not None:
        _safe_notify(
            event.hall.manager_id,
            NotificationKind.OFFER_RESPONSE,
            f"{offer.atelier.name} پیشنهاد را نپذیرفت",
            f"مراسم {_event_label(event)} هنوز بدون اتلیه است.",
            _url("halls.event_show", event_id=event.id),
            entity=event,
        )
    _settle_after_pending_removed(event, actor_user, "همهٔ پیشنهادها پاسخ داده شد")
    return offer


def claim_open_event(event: Event, atelier: Atelier, actor: Any) -> Event:
    """Take an event the hall opened to every atelier (``offer_mode=open``, status ``open``).

    First come, first served: the second claimant gets :class:`OfferUnavailable`.
    """
    actor_user = _actor_user(actor)
    _require_atelier_manager(actor_user, atelier)
    if not atelier.is_active:
        raise OfferError("این اتلیه فعال نیست.")
    if event.offer_mode != OfferMode.OPEN or event.status != EventStatus.OPEN:
        raise OfferUnavailable(UNAVAILABLE)
    _check_takeable(event)
    check_exclusivity(event, atelier)
    with db.session.begin_nested():
        offer: Optional[EventOffer] = db.session.execute(
            sa.select(EventOffer).where(EventOffer.event_id == event.id, EventOffer.atelier_id == atelier.id)
        ).scalar_one_or_none()
        if offer is None:
            offer = EventOffer(
                event=event,
                atelier=atelier,
                offered_by_id=actor_user.id if actor_user else None,
                offered_at=security.now(),
            )
            db.session.add(offer)
        elif offer.status == OfferStatus.ACCEPTED:
            raise OfferUnavailable(UNAVAILABLE)
        _take_event(event, atelier)
        db.session.flush()
        return _finish_take(event, offer, atelier, actor_user, claimed=True)
