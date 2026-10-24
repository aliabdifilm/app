"""Hall reports: events per month, offer acceptance rate, response time, per-atelier table."""

from __future__ import annotations

from flask import render_template, request

from app.modules.halls import bp, stats
from app.modules.halls.common import MANAGER, YEAR_RANGE, hall_filter, hall_options, my_halls, year_choices
from app.services import jalali, security
from app.services.permissions import role_required


@bp.get("/reports")
@role_required(MANAGER)
def reports():
    halls = my_halls()
    selected, ids = hall_filter(halls)
    today = security.today()
    this_year = jalali.to_jalali(today)[0]
    year = request.args.get("y", type=int)
    if not year or not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
        year = this_year
    first, last = stats.year_range(year)
    months = stats.events_per_month(ids, year)
    totals, per_atelier = stats.offer_stats(ids, first, last)
    peak = max((row.total for row in months), default=0)
    return render_template(
        "halls/reports.html",
        halls=halls,
        selected=selected,
        hall_choices=hall_options(halls),
        year=year,
        years=year_choices(range(this_year - 3, this_year + 2)),
        months=months,
        peak=peak,
        total_events=sum(row.total for row in months),
        total_booked=sum(row.booked for row in months),
        total_cancelled=sum(row.cancelled for row in months),
        lost=stats.lost_dates(ids, today, first, last),
        totals=totals,
        per_atelier=per_atelier,
        format_hours=stats.format_hours,
        hall_arg=selected.id if selected else None,
    )
