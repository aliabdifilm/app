"""Business rules of the hall panel: events, halls, photos, ratings, agreements.

Routes parse input and render; everything that decides *whether* something may happen lives
here (and in ``app.services.event_offers`` / ``app.services.staffing``).  Like all services these
functions ``flush`` but never ``commit``; a :class:`RuleError` carries a Persian message that is
safe to flash, and the route rolls back when one escapes.

Every function re-checks that the actor manages the hall concerned (defence in depth: the
routes already load their objects through the ownership helpers).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import sqlalchemy as sa
from flask import url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.routing import BuildError

from app.extensions import db
from app.models import (
    Assignment,
    Atelier,
    Event,
    EventOffer,
    Hall,
    HallAgreement,
    HallPhoto,
    Review,
    User,
)
from app.models.venue import MAX_HALL_PHOTOS
from app.modules.halls.dates import MAX_AHEAD_DAYS, MAX_BULK_DATES
from app.services import audit, event_offers, jalali, notifications, security, staffing, uploads
from app.services.constants import (
    CeremonyType,
    EventStatus,
    NotificationKind,
    OfferMode,
    OfferStatus,
    Role,
    UserStatus,
)
from app.services.money import to_persian_digits

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = (EventStatus.DRAFT, EventStatus.OPEN, EventStatus.OFFERED, EventStatus.CONFIRMED)
CANCELLABLE_STATUSES = EDITABLE_STATUSES
FORBIDDEN = "اجازهٔ انجام این کار را ندارید."
KIND_HALL_PHOTO = "hall"


class RuleError(Exception):
    """A rule was violated; ``str(exc)`` is a Persian message safe to flash."""


class DuplicateEvent(RuleError):
    """There is already an event of that hall at that date and time."""

    def __init__(self, existing: Event) -> None:
        super().__init__("برای این تالار در همین تاریخ و ساعت مراسم دیگری ثبت شده است.")
        self.existing = existing


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _fa(value: Any) -> str:
    return to_persian_digits(str(value))


def _day(value: dt.date) -> str:
    return jalali.to_jalali_str(value, "long", persian_digits=True)


def _url(endpoint: str, **values: Any) -> Optional[str]:
    try:
        return url_for(endpoint, **values)
    except (BuildError, RuntimeError):
        return None


def _manager(hall: Hall, actor: Any) -> User:
    """The acting hall manager of *hall* (admins are not hall managers: they use the admin panel)."""
    uid = audit.actor_id(actor)
    user = db.session.get(User, uid) if uid is not None else None
    if user is None or user.status == UserStatus.SUSPENDED:
        raise RuleError(FORBIDDEN)
    if user.role != Role.HALL_MANAGER or hall.manager_id != user.id:
        raise RuleError(FORBIDDEN)
    return user


def _notify(*args: Any, **kwargs: Any) -> None:
    try:
        with db.session.begin_nested():
            notifications.notify(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a notification must never break the change
        logger.exception("could not create a hall notification")


def lock_hall(hall_id: int) -> None:
    """Serialise writers of one hall's calendar (``SELECT ... FOR UPDATE`` where the database supports it).

    SQLite ignores the clause (it serialises writers anyway); on PostgreSQL it closes the race
    between two requests that both pass the duplicate check.
    """
    db.session.execute(sa.select(Hall.id).where(Hall.id == hall_id).with_for_update()).first()


# --------------------------------------------------------------------------
# events: dates and duplicates
# --------------------------------------------------------------------------


def check_new_date(day: dt.date, today: Optional[dt.date] = None) -> None:
    """A date an event may be created for: today or later, and not absurdly far ahead."""
    today = today or security.today()
    if day < today:
        raise RuleError("تاریخ مراسم نباید گذشته باشد.")
    if (day - today).days > MAX_AHEAD_DAYS:
        raise RuleError("تاریخ مراسم بیش از سه سال بعد است؛ سال را بررسی کنید.")


def find_duplicate(
    hall_id: int, day: dt.date, start_time: Optional[dt.time], *, exclude_id: Optional[int] = None
) -> Optional[Event]:
    """A live (not cancelled) event of the hall at exactly this date and start time (``None`` = no time)."""
    stmt = sa.select(Event).where(
        Event.hall_id == hall_id, Event.date == day, Event.status != EventStatus.CANCELLED
    )
    stmt = stmt.where(Event.start_time.is_(None) if start_time is None else Event.start_time == start_time)
    if exclude_id is not None:
        stmt = stmt.where(Event.id != exclude_id)
    return db.session.execute(stmt.order_by(Event.id).limit(1)).scalars().first()


@dataclass
class EventData:
    """Everything a ceremony form carries."""

    day: dt.date
    start_time: Optional[dt.time] = None
    ceremony_type: CeremonyType = CeremonyType.WEDDING
    guests: Optional[int] = None
    title: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class BulkOutcome:
    """Result of :func:`create_events`."""

    created: list[Event] = field(default_factory=list)
    duplicates: list[tuple[dt.date, Optional[dt.time]]] = field(default_factory=list)
    past: list[dt.date] = field(default_factory=list)
    too_far: list[dt.date] = field(default_factory=list)


def create_event(hall: Hall, actor: Any, data: EventData, *, bulk: bool = False) -> Event:
    """Create one ceremony (status ``draft``, not offered).  Raises :class:`DuplicateEvent` for the same slot."""
    user = _manager(hall, actor)
    if not hall.is_active:
        raise RuleError("این تالار غیرفعال است؛ ابتدا آن را فعال کنید.")
    check_new_date(data.day)
    lock_hall(hall.id)
    existing = find_duplicate(hall.id, data.day, data.start_time)
    if existing is not None:
        raise DuplicateEvent(existing)
    event = Event(
        hall=hall,
        date=data.day,
        start_time=data.start_time,
        ceremony_type=data.ceremony_type,
        guest_count_estimate=data.guests,
        title=data.title,
        notes=data.notes,
        status=EventStatus.DRAFT,
        offer_mode=OfferMode.NONE,
        created_by_id=user.id,
    )
    db.session.add(event)
    db.session.flush()
    audit.log(
        user,
        "event.created",
        event,
        {"hall_id": hall.id, "date": data.day, "time": data.start_time, "bulk": bulk},
    )
    return event


def create_events(
    hall: Hall,
    actor: Any,
    days: Sequence[dt.date],
    times: Sequence[Optional[dt.time]],
    data: EventData,
) -> BulkOutcome:
    """Create an event for every (date, start time); slots that exist already are skipped and reported.

    ``times`` may contain ``None`` (no time).  Dates in the past or too far ahead are reported,
    not created.  Safe to submit twice: the second run creates nothing.
    """
    _manager(hall, actor)
    slots = list(dict.fromkeys(times)) or [None]
    if len(days) * len(slots) > MAX_BULK_DATES:
        raise RuleError(f"در هر بار حداکثر {_fa(MAX_BULK_DATES)} مراسم ثبت می‌شود؛ بازهٔ کوتاه‌تری انتخاب کنید.")
    outcome = BulkOutcome()
    today = security.today()
    for day in sorted(set(days)):
        try:
            check_new_date(day, today)
        except RuleError:
            (outcome.past if day < today else outcome.too_far).append(day)
            continue
        for slot in slots:
            try:
                with db.session.begin_nested():
                    event = create_event(
                        hall,
                        actor,
                        EventData(day, slot, data.ceremony_type, data.guests, data.title, data.notes),
                        bulk=True,
                    )
            except DuplicateEvent:
                outcome.duplicates.append((day, slot))
            else:
                outcome.created.append(event)
    return outcome


# --------------------------------------------------------------------------
# events: edit, cancel, reactivate, delete
# --------------------------------------------------------------------------


def _interested_managers(event: Event) -> list[int]:
    """Atelier managers who have a stake in the event: its atelier and everybody with a pending offer."""
    ids: list[int] = []
    if event.atelier is not None:
        ids.append(event.atelier.manager_id)
    rows = db.session.execute(
        sa.select(Atelier.manager_id)
        .join(EventOffer, EventOffer.atelier_id == Atelier.id)
        .where(EventOffer.event_id == event.id, EventOffer.status == OfferStatus.PENDING)
    ).scalars()
    ids.extend(rows)
    return list(dict.fromkeys(ids))


def _describe_change(name: str, old: Any, new: Any) -> str:
    def show(value: Any) -> str:
        if isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, dt.date):
            return _day(value)
        if isinstance(value, dt.time):
            return to_persian_digits(value.strftime("%H:%M"))
        if isinstance(value, CeremonyType):
            return value.label
        return "—" if value in (None, "") else _fa(value)

    return f"{name}: {show(old)} ← {show(new)}"


def update_event(event: Event, actor: Any, data: EventData) -> dict[str, tuple[Any, Any]]:
    """Apply a ceremony form; returns ``{field: (old, new)}`` of what really changed.

    * done, invoiced and cancelled events are closed (reactivate a cancelled one first);
    * the **date** of an event that already belongs to an atelier is fixed (its staff were
      booked for that day): cancel it and create a new one instead;
    * the new slot must not duplicate another live event of the hall;
    * the atelier (and everybody with a pending offer) is told when the date, time or kind changes.
    """
    user = _manager(event.hall, actor)
    if event.status not in EDITABLE_STATUSES:
        raise RuleError(f"مراسمِ «{event.status.label}» قابل ویرایش نیست.")
    changes: dict[str, tuple[Any, Any]] = {}
    fields = (
        ("date", "تاریخ", event.date, data.day),
        ("start_time", "ساعت", event.start_time, data.start_time),
        ("ceremony_type", "نوع مراسم", event.ceremony_type, data.ceremony_type),
        ("guest_count_estimate", "تعداد میهمان", event.guest_count_estimate, data.guests),
        ("title", "عنوان", event.title, data.title),
        ("notes", "توضیحات", event.notes, data.notes),
    )
    for name, _label, old, new in fields:
        if old != new:
            changes[name] = (old, new)
    if not changes:
        return changes
    if "date" in changes:
        if event.atelier_id is not None:
            raise RuleError("تاریخ مراسمی که به اتلیه رسیده قابل تغییر نیست؛ آن را لغو کنید و مراسم تازه‌ای ثبت کنید.")
        check_new_date(data.day)
    if "date" in changes or "start_time" in changes:
        lock_hall(event.hall_id)
        existing = find_duplicate(event.hall_id, data.day, data.start_time, exclude_id=event.id)
        if existing is not None:
            raise DuplicateEvent(existing)
    for name, (_old, new) in changes.items():
        setattr(event, name, new)
    db.session.flush()
    audit.log(user, "event.updated", event, {"changes": {name: [old, new] for name, (old, new) in changes.items()}})

    important = [(label, old, new) for name, label, old, new in fields if name in changes and name in ("date", "start_time", "ceremony_type")]
    if important:
        lines = [_describe_change(label, old, new) for label, old, new in important]
        for manager_id in _interested_managers(event):
            _notify(
                manager_id,
                NotificationKind.EVENT_STATUS,
                "مشخصات یک مراسم تغییر کرد",
                f"{event.hall.name}\n" + "\n".join(lines),
                _url("ateliers.event_detail", event_id=event.id),
                entity=event,
            )
    return changes


def cancel_event(event: Event, actor: Any, reason: Optional[str] = None) -> Event:
    """Cancel a ceremony: pending offers are withdrawn, the atelier and its staff are told (staffing service)."""
    user = _manager(event.hall, actor)
    if event.status not in CANCELLABLE_STATUSES:
        raise RuleError(f"مراسمِ «{event.status.label}» را نمی‌توان لغو کرد.")
    event_offers.withdraw_pending(event, user, reason or "مراسم لغو شد")
    event.offer_mode = OfferMode.NONE
    staffing.change_event_status(event, EventStatus.CANCELLED, user, reason=reason)
    return event


def reactivate_event(event: Event, actor: Any) -> Event:
    """Bring a cancelled ceremony back as a draft (the slot must still be free and in the future)."""
    user = _manager(event.hall, actor)
    if event.status != EventStatus.CANCELLED:
        raise RuleError("فقط مراسم لغوشده را می‌توان دوباره فعال کرد.")
    check_new_date(event.date)
    lock_hall(event.hall_id)
    existing = find_duplicate(event.hall_id, event.date, event.start_time, exclude_id=event.id)
    if existing is not None:
        raise DuplicateEvent(existing)
    event.atelier = None
    event.offer_mode = OfferMode.NONE
    staffing.change_event_status(event, EventStatus.DRAFT, user, reason="فعال‌سازی دوباره")
    return event


def delete_event(event: Event, actor: Any) -> None:
    """Delete a ceremony that never went anywhere (a draft without offers, staff or invoice)."""
    user = _manager(event.hall, actor)
    if event.status != EventStatus.DRAFT or event.atelier_id is not None:
        raise RuleError("فقط مراسم پیش‌نویس را می‌توان حذف کرد؛ برای بقیه «لغو مراسم» را بزنید.")
    has_offers = db.session.execute(sa.select(EventOffer.id).where(EventOffer.event_id == event.id).limit(1)).first()
    has_staff = db.session.execute(sa.select(Assignment.id).where(Assignment.event_id == event.id).limit(1)).first()
    if has_offers or has_staff or event.invoice is not None:
        raise RuleError("این مراسم سابقهٔ پیشنهاد یا افیش دارد و حذف نمی‌شود؛ آن را لغو کنید.")
    audit.log(user, "event.deleted", event, {"date": event.date, "time": event.start_time, "hall_id": event.hall_id})
    db.session.delete(event)
    db.session.flush()


# --------------------------------------------------------------------------
# ratings
# --------------------------------------------------------------------------

REVIEWABLE_STATUSES = (EventStatus.CONFIRMED, EventStatus.DONE, EventStatus.INVOICED)


def review_blocker(event: Event, today: Optional[dt.date] = None) -> Optional[str]:
    """Why the hall cannot rate the atelier of *event* yet (``None`` = it can)."""
    if event.atelier_id is None:
        return "این مراسم اتلیه‌ای ندارد."
    if event.status not in REVIEWABLE_STATUSES:
        return "امتیاز برای مراسم قطعی، برگزارشده یا فاکتورشده ثبت می‌شود."
    if event.date > (today or security.today()):
        return "پس از برگزاری مراسم می‌توانید به اتلیه امتیاز بدهید."
    return None


def event_review(event: Event, author: Any) -> Optional[Review]:
    """The rating the hall manager *author* gave for this event, if any."""
    uid = audit.actor_id(author)
    return db.session.execute(
        sa.select(Review).where(Review.event_id == event.id, Review.author_id == uid)
    ).scalar_one_or_none()


def rate_atelier(event: Event, actor: Any, rating: int, comment: Optional[str]) -> tuple[Review, bool]:
    """Rate (or re-rate) the atelier that worked *event*; returns ``(review, created)``.

    One rating per hall manager and event.  The atelier's manager is told about a new rating.
    A hidden review (moderated) stays hidden when edited.
    """
    user = _manager(event.hall, actor)
    blocker = review_blocker(event)
    if blocker:
        raise RuleError(blocker)
    atelier = event.atelier
    review = event_review(event, user)
    created = review is None
    if review is None:
        review = Review(
            event_id=event.id,
            atelier_id=atelier.id,
            author_id=user.id,
            subject_id=atelier.manager_id,
            rating=rating,
            comment=comment,
        )
        db.session.add(review)
    else:
        review.rating = rating
        review.comment = comment
    try:
        with db.session.begin_nested():
            db.session.flush()
    except IntegrityError:  # two submissions raced
        raise RuleError("امتیاز شما همین حالا ثبت شد؛ صفحه را دوباره باز کنید.") from None
    audit.log(user, "review.created" if created else "review.updated", review, {"event_id": event.id, "rating": rating})
    if created:
        _notify(
            atelier.manager_id,
            NotificationKind.REVIEW_RECEIVED,
            "تالار به شما امتیاز داد",
            f"{event.hall.name} · {_day(event.date)}: {_fa(rating)} از ۵",
            _url("ateliers.event_detail", event_id=event.id),
            entity=review,
        )
    return review, created


# --------------------------------------------------------------------------
# halls and photos
# --------------------------------------------------------------------------


def hall_name_taken(manager_id: int, name: str, *, exclude_id: Optional[int] = None) -> bool:
    stmt = sa.select(Hall.id).where(Hall.manager_id == manager_id, Hall.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Hall.id != exclude_id)
    return db.session.execute(stmt.limit(1)).first() is not None


def delete_hall(hall: Hall, actor: Any) -> list[str]:
    """Delete a hall that has no events; returns the photo paths to remove from disk after the commit."""
    user = _manager(hall, actor)
    if db.session.execute(sa.select(Event.id).where(Event.hall_id == hall.id).limit(1)).first():
        raise RuleError("این تالار مراسم ثبت‌شده دارد و حذف نمی‌شود؛ می‌توانید آن را غیرفعال کنید.")
    paths = [photo.image_path for photo in hall.photos]
    audit.log(user, "hall.deleted", hall, {"name": hall.name})
    db.session.delete(hall)
    db.session.flush()
    return paths


def add_photo(hall: Hall, actor: Any, file: Any, caption: Optional[str] = None) -> HallPhoto:
    """Store an uploaded picture of the hall (JPEG / PNG / WebP, see ``uploads``); at most 12 per hall."""
    user = _manager(hall, actor)
    count = db.session.scalar(sa.select(sa.func.count(HallPhoto.id)).where(HallPhoto.hall_id == hall.id)) or 0
    if count >= MAX_HALL_PHOTOS:
        raise RuleError(f"هر تالار حداکثر {_fa(MAX_HALL_PHOTOS)} عکس دارد؛ ابتدا یکی را حذف کنید.")
    try:
        stored = uploads.save_image(file, KIND_HALL_PHOTO, min_side=300)
    except uploads.UploadError as exc:
        raise RuleError(str(exc)) from None
    last = db.session.scalar(sa.select(sa.func.max(HallPhoto.sort_order)).where(HallPhoto.hall_id == hall.id))
    photo = HallPhoto(hall=hall, image_path=stored.path, caption=caption, sort_order=(last + 1) if last is not None else 0)
    db.session.add(photo)
    try:
        db.session.flush()
    except Exception:
        uploads.delete_image(stored.path)
        raise
    audit.log(user, "hall.photo_added", hall, {"photo_id": photo.id})
    return photo


def remove_photo(hall: Hall, actor: Any, photo_id: int) -> str:
    """Remove one picture; returns its path (delete the file after the commit)."""
    user = _manager(hall, actor)
    photo = db.session.get(HallPhoto, photo_id)
    if photo is None or photo.hall_id != hall.id:
        raise RuleError("این عکس پیدا نشد.")
    path = photo.image_path
    db.session.delete(photo)
    db.session.flush()
    audit.log(user, "hall.photo_removed", hall, {"photo_id": photo_id})
    return path


# --------------------------------------------------------------------------
# agreements
# --------------------------------------------------------------------------


@dataclass
class AgreementData:
    atelier: Atelier
    start_date: dt.date
    end_date: Optional[dt.date] = None
    is_exclusive: bool = False
    rent_per_event: Optional[int] = None
    revenue_share_percent: Optional[int] = None
    deposit: Optional[int] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


def _overlap_clause(start: dt.date, end: Optional[dt.date]) -> Any:
    """SQL: the agreement's period intersects ``[start, end]`` (``end`` empty = open-ended)."""
    clauses = [sa.or_(HallAgreement.end_date.is_(None), HallAgreement.end_date >= start)]
    if end is not None:
        clauses.append(HallAgreement.start_date <= end)
    return sa.and_(*clauses)


def _validate_agreement(hall: Hall, data: AgreementData, *, exclude_id: Optional[int] = None) -> None:
    if data.end_date is not None and data.end_date < data.start_date:
        raise RuleError("تاریخ پایان نباید پیش از تاریخ شروع باشد.")
    if data.rent_per_event is not None and data.revenue_share_percent is not None:
        raise RuleError("یا اجارهٔ هر مراسم را بنویسید یا درصد سهم؛ هر دو با هم معنی ندارد.")
    base = [HallAgreement.hall_id == hall.id, HallAgreement.terminated_at.is_(None), _overlap_clause(data.start_date, data.end_date)]
    if exclude_id is not None:
        base.append(HallAgreement.id != exclude_id)
    same_atelier = db.session.execute(
        sa.select(HallAgreement.id).where(*base, HallAgreement.atelier_id == data.atelier.id).limit(1)
    ).first()
    if same_atelier:
        raise RuleError("برای این اتلیه در همین بازه توافق‌نامهٔ فعال دیگری هست؛ آن را ویرایش یا پایان دهید.")
    if data.is_exclusive:
        other = db.session.execute(
            sa.select(HallAgreement.id)
            .where(*base, HallAgreement.is_exclusive.is_(True), HallAgreement.atelier_id != data.atelier.id)
            .limit(1)
        ).first()
        if other:
            raise RuleError("در این بازه اتلیهٔ دیگری توافق‌نامهٔ انحصاری دارد؛ دو اتلیه نمی‌توانند هم‌زمان انحصاری باشند.")
        held = db.session.scalar(
            sa.select(sa.func.count(Event.id)).where(
                Event.hall_id == hall.id,
                Event.status != EventStatus.CANCELLED,
                Event.atelier_id.is_not(None),
                Event.atelier_id != data.atelier.id,
                Event.date >= data.start_date,
                *( [Event.date <= data.end_date] if data.end_date is not None else [] ),
            )
        ) or 0
        if held:
            raise RuleError(
                f"در این بازه {_fa(held)} مراسم با اتلیهٔ دیگری ثبت شده است؛ شروع انحصار را بعد از آن‌ها بگذارید."
            )


def _withdraw_foreign_offers(hall: Hall, data: AgreementData, actor_user: User) -> int:
    """An exclusive agreement voids pending offers to other ateliers inside its period."""
    stmt = (
        sa.select(EventOffer)
        .join(Event, Event.id == EventOffer.event_id)
        .where(
            Event.hall_id == hall.id,
            Event.date >= data.start_date,
            EventOffer.status == OfferStatus.PENDING,
            EventOffer.atelier_id != data.atelier.id,
        )
    )
    if data.end_date is not None:
        stmt = stmt.where(Event.date <= data.end_date)
    count = 0
    for offer in db.session.execute(stmt).scalars().unique().all():
        event_offers.withdraw_offer(offer, actor_user, reason="تالار با اتلیهٔ دیگری توافق‌نامهٔ انحصاری بست.")
        count += 1
    return count


def _apply_agreement(agreement: HallAgreement, data: AgreementData) -> None:
    agreement.start_date = data.start_date
    agreement.end_date = data.end_date
    agreement.is_exclusive = data.is_exclusive
    agreement.rent_per_event = data.rent_per_event
    agreement.revenue_share_percent = data.revenue_share_percent
    agreement.deposit = data.deposit
    agreement.contact_name = data.contact_name
    agreement.contact_phone = data.contact_phone
    agreement.notes = data.notes


def create_agreement(hall: Hall, actor: Any, data: AgreementData) -> HallAgreement:
    """Record a new agreement with an atelier; the atelier manager is told."""
    user = _manager(hall, actor)
    atelier = data.atelier
    if not atelier.is_active or atelier.manager is None or atelier.manager.status != UserStatus.ACTIVE:
        raise RuleError("این اتلیه فعال نیست.")
    _validate_agreement(hall, data)
    agreement = HallAgreement(hall=hall, atelier=atelier, created_by_id=user.id, start_date=data.start_date)
    _apply_agreement(agreement, data)
    db.session.add(agreement)
    db.session.flush()
    withdrawn = _withdraw_foreign_offers(hall, data, user) if data.is_exclusive else 0
    audit.log(
        user,
        "agreement.created",
        agreement,
        {"hall_id": hall.id, "atelier_id": atelier.id, "exclusive": data.is_exclusive, "withdrawn_offers": withdrawn},
    )
    _notify(
        atelier.manager_id,
        NotificationKind.SYSTEM,
        "توافق‌نامهٔ جدید با یک تالار",
        _agreement_summary(agreement),
        _url("ateliers.events"),
        entity=agreement,
    )
    return agreement


def update_agreement(agreement: HallAgreement, actor: Any, data: AgreementData) -> HallAgreement:
    """Change the terms of an agreement (the atelier cannot be changed: end it and make a new one)."""
    user = _manager(agreement.hall, actor)
    if agreement.terminated_at is not None:
        raise RuleError("این توافق‌نامه پایان یافته و قابل ویرایش نیست.")
    data.atelier = agreement.atelier
    _validate_agreement(agreement.hall, data, exclude_id=agreement.id)
    old = (agreement.end_date, agreement.is_exclusive, agreement.rent_per_event)
    _apply_agreement(agreement, data)
    if agreement.end_date != old[0]:
        agreement.reminded_at = None  # an extended agreement is reminded again
    db.session.flush()
    withdrawn = _withdraw_foreign_offers(agreement.hall, data, user) if data.is_exclusive and not old[1] else 0
    audit.log(
        user,
        "agreement.updated",
        agreement,
        {"exclusive": data.is_exclusive, "end_date": data.end_date, "rent_per_event": data.rent_per_event, "withdrawn_offers": withdrawn},
    )
    _notify(
        agreement.atelier.manager_id,
        NotificationKind.SYSTEM,
        "شرایط توافق‌نامه با یک تالار تغییر کرد",
        _agreement_summary(agreement),
        _url("ateliers.events"),
        entity=agreement,
    )
    return agreement


def terminate_agreement(agreement: HallAgreement, actor: Any) -> HallAgreement:
    """End an agreement now (it is kept as history); the atelier manager is told."""
    user = _manager(agreement.hall, actor)
    if agreement.terminated_at is not None:
        raise RuleError("این توافق‌نامه پیشتر پایان یافته است.")
    agreement.terminated_at = security.now()
    db.session.flush()
    audit.log(user, "agreement.terminated", agreement, {"hall_id": agreement.hall_id, "atelier_id": agreement.atelier_id})
    _notify(
        agreement.atelier.manager_id,
        NotificationKind.SYSTEM,
        "توافق‌نامه با یک تالار پایان یافت",
        f"{agreement.hall.name}: توافق‌نامه از طرف تالار پایان داده شد.",
        _url("ateliers.events"),
        entity=agreement,
    )
    return agreement


def _agreement_summary(agreement: HallAgreement) -> str:
    parts = [agreement.hall.name]
    period = f"از {_day(agreement.start_date)}"
    period += f" تا {_day(agreement.end_date)}" if agreement.end_date else " (بدون تاریخ پایان)"
    parts.append(period)
    if agreement.is_exclusive:
        parts.append("انحصاری")
    if agreement.rent_per_event is not None:
        parts.append(f"اجارهٔ هر مراسم {to_persian_digits(f'{agreement.rent_per_event:,}').replace(',', '٬')} تومان")
    if agreement.revenue_share_percent is not None:
        parts.append(f"سهم تالار {_fa(agreement.revenue_share_percent)}٪")
    return " · ".join(parts)


def agreement_summary(agreement: HallAgreement) -> str:
    return _agreement_summary(agreement)


def agreement_status(agreement: HallAgreement, today: Optional[dt.date] = None) -> str:
    """``terminated | upcoming | active | expired`` (a machine word; the template maps it to a label)."""
    today = today or security.today()
    if agreement.terminated_at is not None:
        return "terminated"
    if today < agreement.start_date:
        return "upcoming"
    if agreement.end_date is not None and today > agreement.end_date:
        return "expired"
    return "active"


def expiring_agreements(within_days: int = 14, today: Optional[dt.date] = None) -> list[HallAgreement]:
    """Active agreements that end within *within_days* and have not been reminded yet."""
    today = today or security.today()
    horizon = today + dt.timedelta(days=within_days)
    return list(
        db.session.execute(
            sa.select(HallAgreement)
            .where(
                HallAgreement.terminated_at.is_(None),
                HallAgreement.end_date.is_not(None),
                HallAgreement.end_date >= today,
                HallAgreement.end_date <= horizon,
                HallAgreement.start_date <= today,
                HallAgreement.reminded_at.is_(None),
            )
            .order_by(HallAgreement.end_date, HallAgreement.id)
        ).scalars()
    )


def remind_agreements(within_days: int = 14, today: Optional[dt.date] = None) -> int:
    """Tell both managers about agreements that are about to end (once per agreement and end date)."""
    count = 0
    for agreement in expiring_agreements(within_days, today):
        left = (agreement.end_date - (today or security.today())).days
        when = "امروز" if left == 0 else f"{_fa(left)} روز دیگر"
        text = f"توافق‌نامهٔ {agreement.hall.name} و {agreement.atelier.name} {when} ({_day(agreement.end_date)}) پایان می‌یابد."
        _notify(agreement.hall.manager_id, NotificationKind.SYSTEM, "توافق‌نامه رو به پایان است", text, _url("halls.agreement_edit", agreement_id=agreement.id), entity=agreement)
        _notify(agreement.atelier.manager_id, NotificationKind.SYSTEM, "توافق‌نامه رو به پایان است", text, _url("ateliers.events"), entity=agreement)
        agreement.reminded_at = security.now()
        count += 1
    db.session.flush()
    return count
