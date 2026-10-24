"""Giving dates to ateliers: the offers list, the offer page, open-to-all, withdraw and re-offer."""

from __future__ import annotations

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models import Event, EventOffer
from app.modules.halls import bp, queries, search
from app.modules.halls.common import (
    ERRORS,
    MANAGER,
    describe_error,
    fail,
    hall_filter,
    hall_options,
    year_choices,
    my_halls,
    ok,
)
from app.modules.halls.events import my_events_or_404
from app.services import event_offers, jalali, security
from app.services.constants import EventStatus, OfferMode
from app.services.forms import Form
from app.services.http import paginate
from app.services.money import to_persian_digits
from app.services.permissions import event_or_404, get_user, my_hall_ids, owned_or_404, role_required

PER_PAGE = 15
PICK_PER_PAGE = 30
ATELIERS_PER_PAGE = 8
OFFER_TABS = ("pending", "accepted", "declined", "withdrawn", "open", "all")
TAB_LABELS = {
    "pending": "در انتظار پاسخ",
    "accepted": "پذیرفته‌شده",
    "declined": "ردشده",
    "withdrawn": "پس‌گرفته‌شده",
    "open": "باز برای همه",
    "all": "همه",
}


def _offer_or_404(offer_id: int) -> EventOffer:
    return owned_or_404(EventOffer, offer_id, lambda offer, user: offer.event.hall_id in my_hall_ids(user))


# --------------------------------------------------------------------------
# the list of offers
# --------------------------------------------------------------------------


@bp.get("/offers")
@role_required(MANAGER)
def offers():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    today = security.today()
    counts = queries.offer_status_counts(ids)
    open_count = int(
        db.session.scalar(
            sa.select(sa.func.count(Event.id)).where(
                Event.hall_id.in_(ids),
                Event.offer_mode == OfferMode.OPEN,
                Event.status == EventStatus.OPEN,
                Event.atelier_id.is_(None),
                Event.date >= today,
            )
        )
        or 0
    )
    tab = request.args.get("tab", "")
    if tab not in OFFER_TABS:
        tab = "pending" if counts["pending"] or not sum(counts.values()) else "all"
    if tab == "open":
        stmt = (
            queries.events_of(ids)
            .where(
                Event.offer_mode == OfferMode.OPEN,
                Event.status == EventStatus.OPEN,
                Event.atelier_id.is_(None),
                Event.date >= today,
            )
            .order_by(Event.date, Event.start_time, Event.id)
        )
        page = paginate(stmt, per_page=PER_PAGE)
    else:
        stmt = queries.offers_of(ids)
        if tab != "all":
            stmt = stmt.where(EventOffer.status == tab)
        if tab == "pending":
            stmt = stmt.order_by(Event.date, EventOffer.id)
        else:
            stmt = stmt.order_by(EventOffer.updated_at.desc(), EventOffer.id.desc())
        page = paginate(stmt, per_page=PER_PAGE)
    ratings = queries.ratings_for([offer.atelier_id for offer in page.items]) if tab != "open" else {}
    totals = dict(counts, open=open_count, all=sum(counts.values()))
    return render_template(
        "halls/offers.html",
        halls=halls,
        selected=selected,
        hall_choices=hall_options(halls),
        page=page,
        tab=tab,
        tabs=[(key, TAB_LABELS[key], totals[key]) for key in OFFER_TABS],
        ratings=ratings,
        today=today,
        hall_arg=selected.id if selected else None,
        offerable=event_offers.OFFERABLE_STATUSES,
    )


# --------------------------------------------------------------------------
# the offer page: choose events, choose ateliers, send
# --------------------------------------------------------------------------


def _int_args(name: str) -> list[int]:
    out: list[int] = []
    for raw in request.args.getlist(name):
        if raw.strip().isdigit():
            out.append(int(raw.strip()))
    return list(dict.fromkeys(out))


def _offerable_events(ids: list[int], today) -> sa.Select:
    return (
        queries.events_of(ids)
        .where(
            Event.atelier_id.is_(None),
            Event.status.in_(list(event_offers.OFFERABLE_STATUSES)),
            Event.date >= today,
        )
        .order_by(Event.date, Event.start_time, Event.id)
    )


@bp.route("/offers/new", methods=["GET", "POST"])
@role_required(MANAGER)
def offer_new():
    halls = my_halls()
    today = security.today()
    if request.method == "POST":
        return _send(halls, today)

    event_ids = _int_args("event")
    if not event_ids:  # step 1: pick the dates
        selected, ids = hall_filter(halls)
        stmt = _offerable_events(ids, today)
        month = request.args.get("m", type=int)
        year = request.args.get("y", type=int)
        if month and 1 <= month <= 12:
            jy = year if year and 1300 <= year <= 1600 else jalali.to_jalali(today)[0]
            first, last = jalali.jalali_month_range(jy, month)
            stmt = stmt.where(Event.date >= first, Event.date <= last)
        else:
            month = None
        page = paginate(stmt, per_page=PICK_PER_PAGE)
        return render_template(
            "halls/offer_pick.html",
            halls=halls,
            selected=selected,
            hall_choices=hall_options(halls),
            page=page,
            pending=queries.pending_offer_counts([event.id for event in page.items]),
            month=month,
            year=year or jalali.to_jalali(today)[0],
            years=year_choices(range(jalali.to_jalali(today)[0], jalali.to_jalali(today)[0] + 3)),
            months=list(enumerate(jalali.JALALI_MONTHS, start=1)),
            today=today,
            filters={"hall": selected.id if selected else None, "m": month, "y": request.args.get("y", type=int) if month else None},
        )

    # step 2: pick the ateliers
    chosen = my_events_or_404(event_ids)
    usable = [event for event in chosen if event.atelier_id is None and event.status in event_offers.OFFERABLE_STATUSES and event.date >= today]
    query = request.args.get("q", "").strip()[:80]
    city = request.args.get("city", "").strip()[:80]
    sort = request.args.get("sort", "rating")
    sort = sort if sort in dict(search.SORTS) else "rating"
    verified = request.args.get("verified") == "1"
    picked_ids = _int_args("atelier")
    picked = search.active_ateliers(picked_ids)
    stmt = search.directory(query, city, sort, verified_only=verified, exclude_ids=[atelier.id for atelier in picked])
    page = paginate(stmt, per_page=ATELIERS_PER_PAGE)
    all_ids = [atelier.id for atelier in picked] + [atelier.id for atelier in page.items]
    form = Form({})
    form.values["message"] = ""
    return render_template(
        "halls/offer_new.html",
        events=chosen,
        usable=usable,
        picked=picked,
        page=page,
        ratings=queries.ratings_for(all_ids),
        q=query,
        city=city,
        sort=sort,
        sorts=search.SORTS,
        verified=verified,
        cities=search.cities(),
        today=today,
        event_ids=[event.id for event in chosen],
        filters={"q": query or None, "city": city or None, "sort": sort if sort != "rating" else None, "verified": 1 if verified else None,
                 "event": [event.id for event in chosen], "atelier": [a.id for a in picked] or None},
        **form.context(),
    )


def _send(halls, today):
    form = Form(request.form)
    events_ = my_events_or_404(form.int_list("event_id"))
    wanted = form.int_list("atelier_id")
    message = form.text("message", "پیام به اتلیه", max_len=500, multiline=True)
    ateliers = search.active_ateliers(wanted)
    if not wanted:
        flash("دست‌کم یک اتلیه را انتخاب کنید.", "error")
        return redirect(url_for("halls.offer_new", event=[event.id for event in events_]))
    if len(ateliers) != len(set(wanted)):
        flash("یکی از اتلیه‌های انتخاب‌شده دیگر فعال نیست؛ دوباره انتخاب کنید.", "error")
        return redirect(url_for("halls.offer_new", event=[event.id for event in events_]))
    if not form.ok:
        flash(next(iter(form.errors.values())), "error")
        return redirect(url_for("halls.offer_new", event=[event.id for event in events_], atelier=wanted))
    try:
        batch = event_offers.offer_events(events_, ateliers, get_user(), message=message)
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        flash(describe_error(exc), "error")
        return redirect(url_for("halls.offer_new", event=[event.id for event in events_], atelier=wanted))
    if batch.count:
        flash(
            f"{to_persian_digits(batch.count)} پیشنهاد برای {to_persian_digits(len(ateliers))} اتلیه فرستاده شد؛ مدیر هر اتلیه باخبر می‌شود.",
            "success",
        )
    else:
        flash("پیشنهاد تازه‌ای فرستاده نشد.", "info")
    shown = list(dict.fromkeys(reason for _e, _a, reason in batch.skipped))
    for reason in shown[:3]:
        flash(reason, "warning")
    if len(events_) == 1:
        return redirect(url_for("halls.event_show", event_id=events_[0].id))
    return redirect(url_for("halls.offers", tab="pending"))


# --------------------------------------------------------------------------
# single-offer actions
# --------------------------------------------------------------------------


@bp.post("/offers/<int:offer_id>/withdraw")
@role_required(MANAGER)
def offer_withdraw(offer_id: int):
    offer = _offer_or_404(offer_id)
    event_id = offer.event_id
    try:
        event_offers.withdraw_offer(offer, get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("پیشنهاد پس گرفته شد و مدیر اتلیه باخبر شد.", "halls.event_show", event_id=event_id)


@bp.post("/offers/<int:offer_id>/reoffer")
@role_required(MANAGER)
def offer_reoffer(offer_id: int):
    offer = _offer_or_404(offer_id)
    event_id = offer.event_id
    try:
        event_offers.reoffer(offer, get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("پیشنهاد دوباره فرستاده شد.", "halls.event_show", event_id=event_id)


@bp.post("/events/<int:event_id>/open")
@role_required(MANAGER)
def event_open(event_id: int):
    """Publish the event on every atelier's board (first come, first served)."""
    event = event_or_404(event_id, as_=("hall",))
    try:
        batch = event_offers.open_to_all([event], get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    if batch.skipped:
        return fail(batch.skipped[0][1], "halls.event_show", event_id=event_id)
    return ok("مراسم برای همهٔ اتلیه‌ها باز شد؛ هر اتلیه‌ای که زودتر بردارد، آن را می‌گیرد.", "halls.event_show", event_id=event_id)


@bp.post("/events/<int:event_id>/close-open")
@role_required(MANAGER)
def event_close_open(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    try:
        event_offers.close_open(event, get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("مراسم دیگر برای همه نمایش داده نمی‌شود.", "halls.event_show", event_id=event_id)

