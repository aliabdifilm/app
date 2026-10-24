"""The atelier directory: search ateliers by name / city, see rating and badge, open a profile."""

from __future__ import annotations

import sqlalchemy as sa
from flask import render_template, request
from sqlalchemy.orm import contains_eager

from app.extensions import db
from app.models import Event, Hall, HallAgreement, Review
from app.modules.halls import bp, queries, search
from app.modules.halls.common import MANAGER, my_halls
from app.services import security
from app.services.http import paginate
from app.services.permissions import my_hall_ids, role_required

PER_PAGE = 12


def _filters() -> dict:
    query = request.args.get("q", "").strip()[:80]
    city = request.args.get("city", "").strip()[:80]
    sort = request.args.get("sort", "rating")
    return {
        "q": query,
        "city": city,
        "sort": sort if sort in dict(search.SORTS) else "rating",
        "verified": request.args.get("verified") == "1",
    }


@bp.get("/ateliers")
@role_required(MANAGER)
def directory():
    halls = my_halls()
    f = _filters()
    page = paginate(search.directory(f["q"], f["city"], f["sort"], verified_only=f["verified"]), per_page=PER_PAGE)
    ids = [atelier.id for atelier in page.items]
    worked = set()
    agreed = set()
    if ids:
        worked = set(
            db.session.execute(
                sa.select(Event.atelier_id)
                .where(Event.hall_id.in_(sorted(my_hall_ids())), Event.atelier_id.in_(ids), Event.status.in_(queries.BOOKED_STATUSES))
                .group_by(Event.atelier_id)
            ).scalars()
        )
        agreed = set(
            db.session.execute(
                sa.select(HallAgreement.atelier_id)
                .where(
                    HallAgreement.hall_id.in_(sorted(my_hall_ids())),
                    HallAgreement.atelier_id.in_(ids),
                    HallAgreement.active_on(security.today()),
                )
                .group_by(HallAgreement.atelier_id)
            ).scalars()
        )
    return render_template(
        "halls/directory.html",
        halls=halls,
        page=page,
        ratings=queries.ratings_for(ids),
        worked=worked,
        agreed=agreed,
        cities=search.cities(),
        sorts=search.SORTS,
        filters={"q": f["q"] or None, "city": f["city"] or None, "sort": f["sort"] if f["sort"] != "rating" else None,
                 "verified": 1 if f["verified"] else None},
        **f,
    )


@bp.get("/ateliers/<int:atelier_id>")
@role_required(MANAGER)
def atelier_show(atelier_id: int):
    from flask import abort

    my_halls()
    atelier = search.active_atelier(atelier_id)
    if atelier is None:
        abort(404)
    today = security.today()
    mine = sorted(my_hall_ids())
    rating = queries.ratings_for([atelier.id]).get(atelier.id)
    with_me = dict(
        db.session.execute(
            sa.select(Event.status, sa.func.count(Event.id))
            .where(Event.hall_id.in_(mine), Event.atelier_id == atelier.id)
            .group_by(Event.status)
        ).all()
    )
    upcoming = list(
        db.session.execute(
            queries.events_of(mine)
            .where(Event.atelier_id == atelier.id, queries.tab_condition("upcoming", today))
            .order_by(Event.date, Event.start_time)
            .limit(5)
        ).scalars().unique()
    )
    agreements = list(
        db.session.execute(
            sa.select(HallAgreement)
            .join(Hall, Hall.id == HallAgreement.hall_id)
            .where(HallAgreement.hall_id.in_(mine), HallAgreement.atelier_id == atelier.id)
            .options(contains_eager(HallAgreement.hall))
            .order_by(HallAgreement.start_date.desc())
            .limit(10)
        ).scalars()
    )
    reviews = list(
        db.session.execute(
            sa.select(Review, Hall.name, Event.date)
            .join(Event, Event.id == Review.event_id)
            .join(Hall, Hall.id == Event.hall_id)
            .where(Review.atelier_id == atelier.id, Review.is_hidden.is_(False))
            .order_by(Review.created_at.desc())
            .limit(6)
        ).all()
    )
    known = sum(with_me.values()) > 0 or any(agreement.is_active_on(today) for agreement in agreements)
    return render_template(
        "halls/atelier_show.html",
        atelier=atelier,
        rating=rating,
        with_me={status.value if hasattr(status, "value") else status: number for status, number in with_me.items()},
        upcoming=upcoming,
        agreements=agreements,
        reviews=reviews,
        known=known,
        today=today,
    )
