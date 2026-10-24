"""Events of the hall: Jalali calendar, list, create, edit, cancel, rate, bulk actions, one event page."""

from __future__ import annotations

import datetime as dt
from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models import Event
from app.modules.halls import bp, forms, queries, rules, timeline
from app.modules.halls.common import (
    ERRORS,
    MANAGER,
    YEAR_RANGE,
    describe_error,
    fail,
    hall_filter,
    hall_options,
    year_choices,
    jalali_month_arg,
    month_title,
    my_halls,
    ok,
    shift_month,
    state_of,
)
from app.services import event_offers, jalali, security
from app.services.constants import CeremonyType, EventStatus
from app.services.forms import Form
from app.services.http import paginate, referrer_path
from app.services.money import to_persian_digits
from app.services.permissions import event_or_404, get_user, hall_or_404, my_hall_ids, role_required

PER_PAGE = 15
MAX_BULK_ACTION = 200

def my_events_or_404(ids: list[int]) -> list[Event]:
    """Events by id, all of them in my halls - one foreign or missing id answers 404 for the whole request."""
    wanted = sorted(set(ids))
    if not wanted or len(wanted) > MAX_BULK_ACTION:
        abort(404)
    rows = list(
        db.session.execute(queries.events_of(my_hall_ids()).where(Event.id.in_(wanted)).order_by(Event.date, Event.id))
        .scalars()
        .unique()
    )
    if len(rows) != len(wanted):
        abort(404)
    return rows


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------


@bp.get("/calendar")
@role_required(MANAGER)
def calendar():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    today = security.today()
    jy, jm = jalali_month_arg(today)
    first, last = jalali.jalali_month_range(jy, jm)
    grid = jalali.month_grid(jy, jm)
    month = queries.month_events(ids, first, last)
    pending = queries.pending_offer_counts([event.id for event in month])
    entries: dict[dt.date, list[dict[str, str]]] = {}
    tally = {"booked": 0, "free": 0, "offered": 0}
    for event in month:
        label, tone = state_of(event, pending.get(event.id, 0))
        if event.status in (EventStatus.DONE, EventStatus.INVOICED):
            tone = "neutral"
        elif event.status == EventStatus.DRAFT:
            tone = "warning"  # nobody has been asked yet
        name = event.hall.name if len(ids) > 1 else event.ceremony_type.label
        if event.start_time:
            name = f"{name} · {to_persian_digits(event.start_time.strftime('%H:%M'))}"
        entries.setdefault(event.date, []).append(
            {"label": name, "url": url_for("halls.event_show", event_id=event.id), "tone": tone}
        )
        if event.status in queries.BOOKED_STATUSES:
            tally["booked"] += 1
        elif event.status == EventStatus.OFFERED:
            tally["offered"] += 1
        else:
            tally["free"] += 1
    extra = {"hall": selected.id} if selected else {}
    prev_y, prev_m = shift_month(jy, jm, -1)
    next_y, next_m = shift_month(jy, jm, 1)
    return render_template(
        "halls/calendar.html",
        halls=halls,
        selected=selected,
        grid=grid,
        entries=entries,
        title=month_title(jy, jm),
        prev_url=url_for("halls.calendar", y=prev_y, m=prev_m, **extra) if prev_y >= YEAR_RANGE[0] else None,
        next_url=url_for("halls.calendar", y=next_y, m=next_m, **extra) if next_y <= YEAR_RANGE[1] else None,
        this_month_url=url_for("halls.calendar", **extra),
        today=today,
        count=len(month),
        tally=tally,
        hall_choices=hall_options(halls),
        month_first=first,
    )


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

TAB_LABELS = {
    "upcoming": "پیش‌رو",
    "free": "بدون اتلیه",
    "offered": "پیشنهادشده",
    "confirmed": "قطعی",
    "past": "گذشته",
    "cancelled": "لغوشده",
}


@bp.get("/events")
@role_required(MANAGER)
def events():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    today = security.today()
    tab = request.args.get("tab", "")
    if tab not in queries.EVENT_TABS:
        tab = "upcoming"
    text = request.args.get("q", "").strip()[:80]
    year = request.args.get("y", type=int)
    month = request.args.get("m", type=int)
    stmt = queries.events_of(ids).where(queries.tab_condition(tab, today))
    if text:
        stmt = stmt.where(queries.search_condition(text))
    if month and 1 <= month <= 12:
        jy = year if year and YEAR_RANGE[0] <= year <= YEAR_RANGE[1] else jalali.to_jalali(today)[0]
        first, last = jalali.jalali_month_range(jy, month)
        stmt = stmt.where(Event.date >= first, Event.date <= last)
    else:
        month = None
    page = paginate(stmt.order_by(*queries.order_for(tab)), per_page=PER_PAGE)
    counts = queries.tab_counts(ids, today)
    pending = queries.pending_offer_counts([event.id for event in page.items])
    this_year = jalali.to_jalali(today)[0]
    return render_template(
        "halls/events.html",
        halls=halls,
        selected=selected,
        page=page,
        tab=tab,
        tabs=[(key, TAB_LABELS[key], counts[key]) for key in queries.EVENT_TABS],
        pending=pending,
        today=today,
        q=text,
        month=month,
        year=year or this_year,
        years=year_choices(range(this_year - 1, this_year + 3)),
        months=list(enumerate(jalali.JALALI_MONTHS, start=1)),
        hall_choices=hall_options(halls),
        filters={"hall": selected.id if selected else None, "q": text or None, "m": month, "y": year if month else None},
    )


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def _event_form_page(form: Form, halls, *, status: int = 200, event: Event | None = None, duplicate: Event | None = None):
    return (
        render_template(
            "halls/event_form.html",
            halls=halls,
            hall_choices=hall_options(halls),
            ceremony_choices=CeremonyType.choices(),
            event=event,
            duplicate=duplicate,
            today=security.today(),
            **form.context(),
        ),
        status,
    )


def _hall_from_form(form: Form, halls):
    """The hall chosen in a form (or the only one); the id is re-checked, never trusted."""
    if len(halls) == 1:
        return halls[0]
    raw = form.raw("hall_id")
    if not raw.isdigit():
        form.error("hall_id", "تالار را انتخاب کنید.")
        return None
    return hall_or_404(int(raw))


@bp.route("/events/new", methods=["GET", "POST"])
@role_required(MANAGER)
def event_new():
    halls = my_halls()
    if request.method == "GET":
        form = Form({})
        selected, _ = hall_filter(halls)
        form.values.update(
            {
                "hall_id": selected.id if selected else halls[0].id,
                "ceremony_type": CeremonyType.WEDDING.value,
                "date": request.args.get("date", ""),
            }
        )
        return _event_form_page(form, halls)
    form = Form(request.form)
    hall = _hall_from_form(form, halls)
    form.values["hall_id"] = hall.id if hall else form.raw("hall_id")
    data = forms.parse_event(form)
    if not form.ok or data is None or hall is None:
        return _event_form_page(form, halls, status=422)
    try:
        event = rules.create_event(hall, get_user(), data)
    except rules.DuplicateEvent as exc:
        db.session.rollback()
        form.error("date", str(exc))
        return _event_form_page(form, halls, status=422, duplicate=exc.existing)
    except rules.RuleError as exc:
        db.session.rollback()
        form.error("date", str(exc))
        return _event_form_page(form, halls, status=422)
    db.session.commit()
    flash("مراسم ثبت شد. حالا می‌توانید آن را به اتلیه پیشنهاد دهید.", "success")
    return redirect(url_for("halls.event_show", event_id=event.id))


# --------------------------------------------------------------------------
# one event
# --------------------------------------------------------------------------


@bp.get("/events/<int:event_id>")
@role_required(MANAGER)
def event_show(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    user = get_user()
    today = security.today()
    offer_rows = queries.event_offers_list(event)
    ratings = queries.ratings_for([offer.atelier_id for offer in offer_rows] + ([event.atelier_id] if event.atelier_id else []))
    history = timeline.describe(queries.timeline(event, offer_rows), offer_rows, user.id)
    review = rules.event_review(event, user) if event.atelier_id else None
    blocker = rules.review_blocker(event, today)
    exclusive = event_offers.exclusive_agreement_for(event) if event.atelier_id is None else None
    can_offer = (
        event.atelier_id is None and event.status in event_offers.OFFERABLE_STATUSES and event.date >= today
    )
    pending = sum(1 for offer in offer_rows if offer.status.value == "pending")
    return render_template(
        "halls/event_show.html",
        event=event,
        hall=event.hall,
        offers=offer_rows,
        pending=pending,
        state=state_of(event, pending),
        ratings=ratings,
        history=history,
        review=review,
        review_blocker=blocker,
        outcome=queries.outcome_of(event) if event.atelier_id else None,
        exclusive=exclusive,
        can_offer=can_offer,
        can_edit=event.status in rules.EDITABLE_STATUSES,
        can_cancel=event.status in rules.CANCELLABLE_STATUSES,
        can_delete=event.status == EventStatus.DRAFT and not offer_rows,
        share=timeline.share_text(event),
        today=today,
    )


@bp.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
@role_required(MANAGER)
def event_edit(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    halls = my_halls()
    if event.status not in rules.EDITABLE_STATUSES:
        return fail(f"مراسمِ «{event.status.label}» قابل ویرایش نیست.", "halls.event_show", event_id=event.id)
    if request.method == "GET":
        form = Form({})
        form.values.update(forms.event_values(event))
        return _event_form_page(form, halls, event=event)
    form = Form(request.form)
    data = forms.parse_event(form)
    if not form.ok or data is None:
        return _event_form_page(form, halls, status=422, event=event)
    try:
        changes = rules.update_event(event, get_user(), data)
    except rules.DuplicateEvent as exc:
        db.session.rollback()
        form.error("date", str(exc))
        return _event_form_page(form, halls, status=422, event=event, duplicate=exc.existing)
    except rules.RuleError as exc:
        db.session.rollback()
        form.error("date", str(exc))
        return _event_form_page(form, halls, status=422, event=event)
    db.session.commit()
    flash("تغییرات مراسم ذخیره شد." if changes else "تغییری برای ذخیره نبود.", "success" if changes else "info")
    return redirect(url_for("halls.event_show", event_id=event.id))


@bp.post("/events/<int:event_id>/cancel")
@role_required(MANAGER)
def event_cancel(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    reason = Form(request.form).text("reason", "دلیل لغو", max_len=300)
    try:
        rules.cancel_event(event, get_user(), reason)
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("مراسم لغو شد و اتلیه‌های ذی‌نفع باخبر شدند.", "halls.event_show", event_id=event_id)


@bp.post("/events/<int:event_id>/reactivate")
@role_required(MANAGER)
def event_reactivate(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    try:
        rules.reactivate_event(event, get_user())
        db.session.commit()
    except rules.DuplicateEvent as exc:
        db.session.rollback()
        return fail(f"{exc} مراسم دیگر را لغو کنید یا زمان را عوض کنید.", "halls.event_show", event_id=event_id)
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("مراسم دوباره فعال شد (پیش‌نویس).", "halls.event_show", event_id=event_id)


@bp.post("/events/<int:event_id>/delete")
@role_required(MANAGER)
def event_delete(event_id: int):
    event = event_or_404(event_id, as_=("hall",))
    try:
        rules.delete_event(event, get_user())
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id)
    return ok("مراسم حذف شد.", "halls.events")


@bp.post("/events/<int:event_id>/review")
@role_required(MANAGER)
def event_review(event_id: int):
    """Rate the atelier that worked the event (one rating per event, editable)."""
    event = event_or_404(event_id, as_=("hall",))
    form = Form(request.form)
    parsed = forms.parse_review(form)
    if parsed is None:
        flash(next(iter(form.errors.values()), "امتیاز را وارد کنید."), "error")
        return redirect(url_for("halls.event_show", event_id=event.id, _anchor="review"))
    try:
        _, created = rules.rate_atelier(event, get_user(), *parsed)
        db.session.commit()
    except ERRORS as exc:
        db.session.rollback()
        return fail(describe_error(exc), "halls.event_show", event_id=event_id, _anchor="review")
    return ok("امتیاز شما ثبت شد." if created else "امتیاز شما ویرایش شد.", "halls.event_show", event_id=event_id, _anchor="review")


# --------------------------------------------------------------------------
# bulk actions on the events list
# --------------------------------------------------------------------------


def _back() -> Any:
    target = referrer_path()
    return redirect(target or url_for("halls.events"))


@bp.post("/events/action")
@role_required(MANAGER)
def events_action():
    """Open to all / cancel / delete several selected events at once (all of them must be mine)."""
    form = Form(request.form)
    action = form.choice("action", "کار", ("offer", "open", "cancel", "delete"), required=True)
    events_ = my_events_or_404(form.int_list("event_ids"))
    if action is None:
        flash("کار انتخاب‌شده نامعتبر است.", "error")
        return _back()
    user = get_user()
    if action == "offer":  # carry the selection to the offer page
        return redirect(url_for("halls.offer_new", event=[event.id for event in events_]))
    try:
        if action == "open":
            batch = event_offers.open_to_all(events_, user)
            db.session.commit()
            if batch.opened:
                flash(f"{to_persian_digits(len(batch.opened))} مراسم برای همهٔ اتلیه‌ها باز شد.", "success")
            for _event, reason in batch.skipped[:3]:
                flash(f"{jalali.to_jalali_str(_event.date, 'long', persian_digits=True)}: {reason}", "warning")
            if len(batch.skipped) > 3:
                flash(f"{to_persian_digits(len(batch.skipped) - 3)} مراسم دیگر هم نادیده گرفته شد.", "warning")
        else:
            done, skipped = 0, []
            for event in events_:
                try:
                    with db.session.begin_nested():
                        if action == "cancel":
                            rules.cancel_event(event, user, form.raw("reason") or None)
                        else:
                            rules.delete_event(event, user)
                    done += 1
                except ERRORS as exc:
                    skipped.append((event, describe_error(exc)))
            db.session.commit()
            verb = "لغو" if action == "cancel" else "حذف"
            if done:
                flash(f"{to_persian_digits(done)} مراسم {verb} شد.", "success")
            for event, reason in skipped[:3]:
                flash(f"{jalali.to_jalali_str(event.date, 'long', persian_digits=True)}: {reason}", "warning")
            if len(skipped) > 3:
                flash(f"{to_persian_digits(len(skipped) - 3)} مراسم دیگر هم {verb} نشد.", "warning")
    except ERRORS as exc:
        db.session.rollback()
        flash(describe_error(exc), "error")
    return _back()

