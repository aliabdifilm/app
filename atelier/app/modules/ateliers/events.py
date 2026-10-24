"""Events of the atelier: board of offers, calendar, one page per event, event-level actions."""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Equipment, EquipmentUsage, Event, EventOffer
from app.modules.ateliers import bp, links, offers, queries
from app.modules.ateliers import board as boards
from app.modules.ateliers.common import (
    MANAGER,
    current_atelier,
    describe_staffing_error,
    event_label,
    fail,
    my_event_or_404,
    to_event,
)
from app.services import audit, jalali, security, staffing
from app.services.constants import (
    CLOSED_EVENT_STATUSES,
    ROLE_DISPLAY_ORDER,
    EventStatus,
    OfferStatus,
)
from app.services.forms import Form
from app.services.http import paginate
from app.services.money import to_persian_digits
from app.services.permissions import (
    equipment_or_404,
    event_or_404,
    get_my_atelier,
    get_user,
    my_atelier_ids,
    owned_or_404,
    role_required,
)

PER_PAGE = 10
TABS = ("offers", "open", "upcoming", "past")
MAX_NEED = 10
CALENDAR_YEARS = (1300, 1600)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _same_day_load(atelier_id: int, dates: list[dt.date]) -> dict[dt.date, int]:
    """How many events of mine fall on each of these dates (a hint next to an incoming offer)."""
    if not dates:
        return {}
    rows = db.session.execute(
        sa.select(Event.date, sa.func.count(Event.id))
        .where(Event.atelier_id == atelier_id, Event.date.in_(dates), Event.status != EventStatus.CANCELLED)
        .group_by(Event.date)
    )
    return {day: int(count) for day, count in rows}


def month_title(jy: int, jm: int) -> str:
    return to_persian_digits(f"{jalali.JALALI_MONTHS[jm - 1]} {jy}")


# --------------------------------------------------------------------------
# board of events: offers / open / upcoming / past
# --------------------------------------------------------------------------


@bp.get("/events")
@role_required(MANAGER)
def events():
    atelier = current_atelier()
    today = security.today()
    city_arg = request.args.get("city", "")
    city = None if city_arg == "all" else (atelier.city or None)
    offer_count = int(db.session.scalar(queries.incoming_offer_count([atelier.id], today)) or 0)
    open_count = int(db.session.scalar(queries.open_event_count([atelier.id], today, city)) or 0)
    tab = request.args.get("tab", "")
    if tab not in TABS:
        tab = "offers" if offer_count else "upcoming"

    context: dict[str, Any] = {}
    if tab == "offers":
        page = paginate(queries.incoming_offers([atelier.id], today), per_page=PER_PAGE)
        items = [offer.event for offer in page.items]
    elif tab == "open":
        page = paginate(queries.open_events([atelier.id], today, city), per_page=PER_PAGE)
        items = list(page.items)
    elif tab == "upcoming":
        page = paginate(queries.upcoming(atelier.id, today), per_page=PER_PAGE)
        items = list(page.items)
    else:
        page = paginate(queries.past(atelier.id, today), per_page=PER_PAGE)
        items = list(page.items)
    if tab in ("upcoming", "past"):
        context["boards"] = boards.load_boards(items)
    else:
        context["load"] = _same_day_load(atelier.id, sorted({event.date for event in items}))
    return render_template(
        "ateliers/events.html",
        atelier=atelier,
        tab=tab,
        page=page,
        today=today,
        offer_count=offer_count,
        open_count=open_count,
        city=city,
        city_arg="all" if city is None else "",
        **context,
    )


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------


def _month_url(jy: int, jm: int) -> str:
    return url_for("ateliers.calendar", y=jy, m=jm)


@bp.get("/calendar")
@role_required(MANAGER)
def calendar():
    atelier = current_atelier()
    today = security.today()
    jy, jm, _ = jalali.to_jalali(today)
    year, month = request.args.get("y", type=int), request.args.get("m", type=int)
    if year and month and CALENDAR_YEARS[0] <= year <= CALENDAR_YEARS[1] and 1 <= month <= 12:
        jy, jm = year, month
    first, last = jalali.jalali_month_range(jy, jm)
    grid = jalali.month_grid(jy, jm)

    month_events = list(
        db.session.execute(
            queries.my_events(atelier.id)
            .where(Event.date >= first, Event.date <= last, Event.status != EventStatus.CANCELLED)
            .order_by(Event.date, Event.start_time, Event.id)
        ).scalars().unique()
    )
    event_boards = boards.load_boards(month_events)
    entries: dict[dt.date, list[dict[str, str]]] = {}
    for event in month_events:
        board = event_boards[event.id]
        if event.status in (EventStatus.DONE, EventStatus.INVOICED):
            tone = "neutral"
        else:
            tone = board.completeness.tone
        label = event_label(event)
        if event.start_time:
            label = f"{label} · {to_persian_digits(event.start_time.strftime('%H:%M'))}"
        entries.setdefault(event.date, []).append(
            {"label": label, "url": url_for("ateliers.event_detail", event_id=event.id), "tone": tone}
        )
    pending = db.session.execute(
        queries.incoming_offers([atelier.id], today).where(Event.date >= first, Event.date <= last)
    ).scalars().unique()
    for offer in pending:
        entries.setdefault(offer.event.date, []).append(
            {
                "label": f"پیشنهاد: {event_label(offer.event)}",
                "url": url_for("ateliers.event_detail", event_id=offer.event_id),
                "tone": "info",
            }
        )
    prev_y, prev_m = (jy, jm - 1) if jm > 1 else (jy - 1, 12)
    next_y, next_m = (jy, jm + 1) if jm < 12 else (jy + 1, 1)
    return render_template(
        "ateliers/calendar.html",
        atelier=atelier,
        grid=grid,
        entries=entries,
        title=month_title(jy, jm),
        prev_url=_month_url(prev_y, prev_m) if prev_y >= CALENDAR_YEARS[0] else None,
        next_url=_month_url(next_y, next_m) if next_y <= CALENDAR_YEARS[1] else None,
        today=today,
        this_month_url=url_for("ateliers.calendar"),
        count=len(month_events),
    )


# --------------------------------------------------------------------------
# one event
# --------------------------------------------------------------------------


@bp.get("/events/<int:event_id>")
@role_required(MANAGER)
def event_detail(event_id: int):
    event = event_or_404(event_id, as_=("atelier", "offered"))
    atelier_ids = my_atelier_ids()
    today = security.today()
    mine = event.atelier_id is not None and event.atelier_id in atelier_ids
    if not mine:
        # an event I may look at but that is not mine yet: show it with accept / decline / claim
        current = current_atelier()
        offer = db.session.execute(
            sa.select(EventOffer).where(
                EventOffer.event_id == event.id,
                EventOffer.atelier_id == current.id,
                EventOffer.status == OfferStatus.PENDING,
            )
        ).scalar_one_or_none()
        takeable = event.atelier_id is None and event.status in offers.TAKEABLE_STATUSES and event.date >= today
        return render_template(
            "ateliers/event_offer.html",
            event=event,
            atelier=current,
            offer=offer if takeable else None,
            can_claim=takeable and offer is None and event.offer_mode.value == "open" and event.status == EventStatus.OPEN,
            same_day=_same_day_load(current.id, [event.date]).get(event.date, 0),
            today=today,
        )

    atelier = event.atelier
    board = boards.load_board(event)
    usages = list(
        db.session.execute(
            sa.select(EquipmentUsage)
            .where(EquipmentUsage.event_id == event.id)
            .options(joinedload(EquipmentUsage.equipment))
            .order_by(EquipmentUsage.id)
        ).scalars()
    )
    attached = {usage.equipment_id for usage in usages}
    available_equipment = list(
        db.session.execute(
            sa.select(Equipment)
            .where(Equipment.atelier_id == atelier.id, Equipment.is_active.is_(True))
            .order_by(Equipment.name)
        ).scalars()
    )
    available_equipment = [item for item in available_equipment if item.id not in attached]
    open_event = event.status not in CLOSED_EVENT_STATUSES
    job_urls = {}
    for role in ROLE_DISPLAY_ORDER:
        slots = board.summary[role]
        if slots.shortfall > 0 and open_event:
            terms, _ = boards.prefill_terms(atelier, role)
            job_urls[role.value] = links.marketplace_job_url(event, role, terms, slots=slots.shortfall)
    return render_template(
        "ateliers/event_detail.html",
        event=event,
        atelier=atelier,
        board=board,
        completeness=board.completeness,
        staffable=boards.is_staffable(event),
        usages=usages,
        available_equipment=available_equipment,
        equipment_options=[(item.id, f"{item.name} · {item.kind.label}") for item in available_equipment],
        equipment_open=event.status not in (EventStatus.INVOICED, EventStatus.CANCELLED),
        roles=ROLE_DISPLAY_ORDER,
        job_urls=job_urls,
        invoice=event.invoice,
        invoice_url=links.accounting_invoice_url(event),
        inventory_url=links.inventory_url(),
        has_previous=bool(boards.previous_events(event, limit=1)) and boards.is_staffable(event),
        share_text=boards.share_text(event, board),
        today=today,
        max_need=MAX_NEED,
        needs={role.value: event.needed(role) for role in ROLE_DISPLAY_ORDER},
        can_mark_done=event.status == EventStatus.CONFIRMED and event.date <= today,
        can_hand_back=event.status == EventStatus.CONFIRMED and event.invoice is None,
    )


# --------------------------------------------------------------------------
# offers: accept / decline / claim
# --------------------------------------------------------------------------


def _offer_or_404(offer_id: int) -> EventOffer:
    return owned_or_404(EventOffer, offer_id, lambda offer, user: offer.atelier_id in my_atelier_ids(user))


@bp.post("/offers/<int:offer_id>/accept")
@role_required(MANAGER)
def offer_accept(offer_id: int):
    offer = _offer_or_404(offer_id)
    try:
        event = offers.accept_offer(offer, get_user())
        db.session.commit()
    except offers.OfferError as exc:
        db.session.rollback()
        return fail(str(exc), "ateliers.events", tab="offers")
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.events", tab="offers")
    flash("مراسم را پذیرفتید و مدیر تالار باخبر شد. حالا نیروها را افیش کنید.", "success")
    return to_event(event.id)


@bp.post("/offers/<int:offer_id>/decline")
@role_required(MANAGER)
def offer_decline(offer_id: int):
    offer = _offer_or_404(offer_id)
    try:
        offers.decline_offer(offer, get_user())
        db.session.commit()
    except offers.OfferError as exc:
        db.session.rollback()
        return fail(str(exc), "ateliers.events", tab="offers")
    flash("پیشنهاد رد شد و مدیر تالار باخبر شد.", "success")
    return redirect(url_for("ateliers.events", tab="offers"))


@bp.post("/events/<int:event_id>/claim")
@role_required(MANAGER)
def event_claim(event_id: int):
    """Take an event the hall opened to every atelier."""
    event = event_or_404(event_id, as_=("offered",))
    form = Form(request.form)
    raw_atelier = form.raw("atelier_id")
    atelier = get_my_atelier(int(raw_atelier)) if raw_atelier.isdigit() else current_atelier()
    try:
        offers.claim_open_event(event, atelier, get_user())
        db.session.commit()
    except offers.OfferError as exc:
        db.session.rollback()
        return fail(str(exc), "ateliers.events", tab="open")
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.events", tab="open")
    flash("مراسم را برداشتید و مدیر تالار باخبر شد. حالا نیروها را افیش کنید.", "success")
    return to_event(event.id)


# --------------------------------------------------------------------------
# event-level actions of my events
# --------------------------------------------------------------------------


@bp.post("/events/<int:event_id>/needs")
@role_required(MANAGER)
def event_needs(event_id: int):
    """How many people each role needs at this event (the afish meter counts against these numbers)."""
    event = my_event_or_404(event_id)
    if event.status in CLOSED_EVENT_STATUSES:
        return fail("این مراسم بسته شده است و تعداد نیروها قابل تغییر نیست.", "ateliers.event_detail", event_id=event.id)
    form = Form(request.form)
    needs: dict[str, int] = {}
    for role in ROLE_DISPLAY_ORDER:
        value = form.integer(f"need__{role.value}", f"تعداد {role.label}", min=0, max=MAX_NEED, default=event.needed(role))
        needs[role.value] = value if value is not None else event.needed(role)
    if not form.ok:
        flash(next(iter(form.errors.values())), "error")
        return to_event(event.id, "needs")
    before = dict(event.staffing_needs or {})
    if before != needs:
        event.staffing_needs = needs
        db.session.flush()
        audit.log(get_user(), "event.needs_changed", event, {"old": before, "new": needs})
    db.session.commit()
    flash("تعداد نیروی هر نقش ذخیره شد.", "success")
    return to_event(event.id)


@bp.post("/events/<int:event_id>/done")
@role_required(MANAGER)
def event_done(event_id: int):
    """The ceremony took place: complete the afish and make the event ready for its invoice."""
    event = my_event_or_404(event_id)
    if event.status != EventStatus.CONFIRMED or event.date > security.today():
        return fail("فقط مراسم قطعی که تاریخش فرا رسیده را می‌توان «برگزار شده» ثبت کرد.", "ateliers.event_detail", event_id=event.id)
    try:
        staffing.change_event_status(event, EventStatus.DONE, get_user(), reason="ثبت برگزاری مراسم")
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash("برگزاری مراسم ثبت شد؛ حالا می‌توانید فاکتور آن را ثبت کنید.", "success")
    return to_event(event_id, "invoice")


@bp.post("/events/<int:event_id>/handback")
@role_required(MANAGER)
def event_handback(event_id: int):
    """Give the event back to the hall: the atelier is cleared and every active afish row is cancelled."""
    event = my_event_or_404(event_id)
    if event.status != EventStatus.CONFIRMED or event.invoice is not None:
        return fail("این مراسم را دیگر نمی‌توان به تالار بازگرداند.", "ateliers.event_detail", event_id=event.id)
    try:
        staffing.change_event_status(event, EventStatus.OPEN, get_user(), reason="اتلیه مراسم را به تالار بازگرداند.")
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash("مراسم به تالار بازگردانده شد و افیش‌های آن لغو شد.", "success")
    return redirect(url_for("ateliers.events", tab="upcoming"))


@bp.post("/events/<int:event_id>/equipment")
@role_required(MANAGER)
def event_equipment_attach(event_id: int):
    event = my_event_or_404(event_id)
    form = Form(request.form)
    raw = form.raw("equipment_id")
    equipment = equipment_or_404(int(raw)) if raw.isdigit() else None
    if equipment is None:
        return fail("تجهیزات را انتخاب کنید.", "ateliers.event_detail", event_id=event.id)
    try:
        staffing.attach_equipment(event, equipment, get_user())
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash(f"«{equipment.name}» به مراسم اضافه شد.", "success")
    return to_event(event_id, "equipment")


@bp.post("/events/<int:event_id>/equipment/<int:equipment_id>/remove")
@role_required(MANAGER)
def event_equipment_detach(event_id: int, equipment_id: int):
    event = my_event_or_404(event_id)
    equipment = equipment_or_404(equipment_id)
    try:
        staffing.detach_equipment(event, equipment, get_user())
        db.session.commit()
    except staffing.StaffingError as exc:
        db.session.rollback()
        return fail(describe_staffing_error(exc), "ateliers.event_detail", event_id=event_id)
    flash(f"«{equipment.name}» از مراسم برداشته شد.", "success")
    return to_event(event_id, "equipment")
