"""Report maths and the inline-SVG trend chart (no chart library, no JavaScript).

Everything here is plain Python over :class:`~app.modules.accounting.queries.InvoiceRow` values, so it is
easy to test.  The chart is described as geometry (rectangles, labels, a polyline) that
``templates/accounting/_chart.html`` renders; colours come from CSS tokens in ``accounting.css``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.services import jalali
from app.services.money import to_persian_digits

__all__ = ["month_buckets", "hall_table", "leaderboard", "trend_chart", "compact"]


def month_buckets(rows: Iterable[Any], jy: int) -> list[dict[str, Any]]:
    """Twelve Jalali months of year *jy* with income, expenses, net, units and event counts."""
    buckets = [
        {"jm": jm, "label": jalali.JALALI_MONTHS[jm - 1], "income": 0, "expenses": 0, "net": 0, "units": 0, "events": 0}
        for jm in range(1, 13)
    ]
    for row in rows:
        year, month, _ = jalali.to_jalali(row.date)
        if year != jy:
            continue
        bucket = buckets[month - 1]
        bucket["income"] += row.income
        bucket["expenses"] += row.expenses
        bucket["net"] += row.net
        bucket["units"] += row.units
        bucket["events"] += 1
    return buckets


def hall_table(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Profitability per hall, best net profit first."""
    halls: dict[int, dict[str, Any]] = {}
    for row in rows:
        hall = halls.setdefault(
            row.hall_id, {"hall": row.hall, "events": 0, "income": 0, "expenses": 0, "net": 0, "units": 0}
        )
        hall["events"] += 1
        hall["income"] += row.income
        hall["expenses"] += row.expenses
        hall["net"] += row.net
        hall["units"] += row.units
    table = sorted(halls.values(), key=lambda h: (-h["net"], h["hall"]))
    for hall in table:
        hall["average_net"] = hall["net"] // hall["events"] if hall["events"] else 0
    return table


def leaderboard(rows: Iterable[tuple[str, str, int, int, int]], limit: int = 15) -> list[dict[str, Any]]:
    """Staff ranked by units sold at the events they worked (a person's units count once per event)."""
    people: dict[str, dict[str, Any]] = {}
    for key, name, invoice_id, units, total in rows:
        person = people.setdefault(key, {"key": key, "name": name, "invoices": {}, "earned": 0})
        person["name"] = name
        person["invoices"][invoice_id] = units
        person["earned"] += total
    ranked = [
        {"name": p["name"], "events": len(p["invoices"]), "units": sum(p["invoices"].values()), "earned": p["earned"]}
        for p in people.values()
    ]
    ranked.sort(key=lambda p: (-p["units"], -p["events"], p["name"]))
    return ranked[:limit]


# --------------------------------------------------------------------------
# chart geometry
# --------------------------------------------------------------------------


def _nice_step(top: int, ticks: int = 4) -> int:
    """A 1 / 2 / 5 x 10^k step so that ``top / step <= ticks``."""
    raw = max(1, -(-top // ticks))
    magnitude = 1
    while magnitude * 10 <= raw:
        magnitude *= 10
    for factor in (1, 2, 5, 10):
        if factor * magnitude >= raw:
            return factor * magnitude
    return 10 * magnitude  # pragma: no cover


def compact(value: int, unit: int) -> str:
    """Axis label: *value* in whole *unit*s with Persian digits (``4000000`` / ``1000000`` -> ``۴``)."""
    return to_persian_digits(value // unit if value >= 0 else -(-value // unit))


@dataclass(frozen=True)
class ChartUnit:
    size: int
    label: str


def trend_chart(months: Sequence[dict[str, Any]], width: int = 720, height: int = 300) -> dict[str, Any]:
    """Geometry of a grouped bar chart (income / expenses per month) with a net-profit line.

    Months run right-to-left like the Jalali calendar.  Returns plain dicts and numbers (all
    integers) so the template only has to print them.
    """
    left, right, top, bottom = 44, 12, 16, 40
    plot_w, plot_h = width - left - right, height - top - bottom
    highest = max([1] + [m["income"] for m in months] + [m["expenses"] for m in months] + [m["net"] for m in months])
    lowest = min([0] + [m["net"] for m in months])
    unit = ChartUnit(1_000_000, "میلیون تومان") if highest >= 1_000_000 else ChartUnit(1_000, "هزار تومان")
    step = _nice_step(highest, 4)
    ceiling = -(-highest // step) * step
    floor = (lowest // step) * step  # floor division rounds toward minus infinity: -1 -> -step, 0 -> 0
    span = max(1, ceiling - floor)

    def y_of(value: int) -> int:
        return top + round((ceiling - value) * plot_h / span)

    zero_y = y_of(0)
    band = plot_w / max(1, len(months))
    bar_w = max(4, int(band * 0.32))
    gap = 2
    grid = []
    tick = floor
    while tick <= ceiling:
        grid.append({"y": y_of(tick), "label": compact(tick, unit.size), "zero": tick == 0})
        tick += step
    bars, labels, dots = [], [], []
    points = []
    for index, month in enumerate(months):
        cx = left + plot_w - int((index + 0.5) * band)  # month 1 at the right edge
        for kind, value, dx in (("income", month["income"], -(bar_w + gap // 2)), ("expense", month["expenses"], gap // 2)):
            if value <= 0:
                continue
            y = y_of(value)
            bars.append(
                {"x": cx + dx, "y": y, "w": bar_w, "h": max(1, zero_y - y), "kind": kind, "month": month["label"], "value": value}
            )
        labels.append({"x": cx, "y": height - bottom + 16, "text": month["label"], "index": month["jm"]})
        if month["events"]:
            dots.append({"x": cx, "y": y_of(month["net"]), "month": month["label"], "value": month["net"]})
            points.append(f"{cx},{y_of(month['net'])}")
    return {
        "width": width,
        "height": height,
        "left": left,
        "right": width - right,
        "unit": unit.label,
        "grid": grid,
        "zero_y": zero_y,
        "bars": bars,
        "labels": labels,
        "dots": dots,
        "net_line": " ".join(points),
        "has_data": any(m["events"] for m in months),
    }
