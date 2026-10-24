#!/usr/bin/env python3
"""Build docs/styleguide.html from the real design-system templates.

    python docs/styleguide/build.py

The page extends app/templates/base.html and uses every macro of
components/ui.html, so it can never drift from the code.  The output is a plain
static file: open it with file:// or any web server (no Flask needed to view).

``make_app()`` is also imported by tests/test_ui_components.py: it is a tiny
Flask app with the Jinja filters the real app shell registers (toman, fa, jdate,
jdatetime, jweekday) and a fake CSRF token, which is all the templates need.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # .../atelier
sys.path.insert(0, str(ROOT))

from flask import Flask, render_template  # noqa: E402
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

from app.services import jalali, money  # noqa: E402

OUTPUT = ROOT / "docs" / "styleguide.html"


def make_app(extra_template_dirs: list[Path] | None = None) -> Flask:
    """A minimal Flask app that can render the design-system templates."""
    app = Flask(
        "styleguide",
        root_path=str(ROOT / "app"),
        template_folder=str(ROOT / "app" / "templates"),
        static_folder=str(ROOT / "app" / "static"),
    )
    app.config["SECRET_KEY"] = "styleguide-only"
    loaders = [FileSystemLoader(str(d)) for d in (extra_template_dirs or [])]
    loaders.append(app.jinja_loader)
    app.jinja_loader = ChoiceLoader(loaders)

    env = app.jinja_env
    env.filters["toman"] = lambda v: money.format_toman(v, persian_digits=True)
    env.filters["fa"] = lambda v: money.to_persian_digits("" if v is None else str(v))
    env.filters["jdate"] = lambda v: jalali.to_jalali_str(v, "numeric", persian_digits=True)
    env.filters["jdatetime"] = lambda v: jalali.to_jalali_datetime_str(v, "numeric", persian_digits=True)
    env.filters["jweekday"] = lambda v: jalali.weekday_name(v)
    env.globals["csrf_token"] = lambda: "styleguide-csrf-token"

    @app.get("/sg")
    def styleguide_page():  # endpoint used by the pagination demo
        return ""

    return app


class DemoPage:
    """Stand-in for a Flask-SQLAlchemy Pagination object."""

    def __init__(self, page: int, pages: int, total: int):
        self.page, self.pages, self.total = page, pages, total
        self.has_prev, self.has_next = page > 1, page < pages
        self.prev_num, self.next_num = page - 1, page + 1

    def iter_pages(self, left_edge=2, left_current=2, right_current=4, right_edge=2):
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - left_current - 1 < num < self.page + right_current)
                or num > self.pages - right_edge
            ):
                if last + 1 != num:
                    yield None
                yield num
                last = num


def demo_context() -> dict:
    """Context shared by the styleguide page: fake user, nav and sample data."""
    year, month = 1404, 5
    grid = jalali.month_grid(year, month)
    d = lambda day: date(*jalali.jalali_to_gregorian(year, month, day))  # noqa: E731
    events = {
        d(3): [{"label": "عروسی باغ نیلوفر", "url": "#", "tone": "accent"}],
        d(10): [
            {"label": "نامزدی تالار ماهان", "url": "#", "tone": "info"},
            {"label": "حنابندان", "url": "#", "tone": "warning"},
        ],
        d(12): [{"label": "عروسی تالار گل‌های سپید", "url": "#", "tone": "success"}],
        d(26): [
            {"label": "عروسی ۱", "url": "#", "tone": "accent"},
            {"label": "عروسی ۲", "url": "#", "tone": "success"},
            {"label": "عروسی ۳", "url": "#", "tone": "danger"},
            {"label": "عروسی ۴", "url": "#", "tone": "info"},
        ],
    }
    rows = [
        {"name": "عروسی تالار گل‌های سپید", "date": d(12), "staff": 4, "income": 10_500_000, "status": "invoiced"},
        {"name": "نامزدی تالار ماهان", "date": d(10), "staff": 3, "income": 4_200_000, "status": "done"},
        {"name": "عروسی باغ نیلوفر", "date": d(3), "staff": 4, "income": 0, "status": "confirmed"},
    ]
    return {
        "layout": "app",
        "current_user": SimpleNamespace(
            is_authenticated=True,
            display_name="علی عبدی",
            role=SimpleNamespace(label="مدیر اتلیه"),
        ),
        "nav_items": [
            {"label": "داشبورد", "url": "#", "icon": "home", "active": False},
            {"label": "راهنمای طراحی", "url": "#", "icon": "sparkles", "active": True},
            {"label": "تقویم مراسم‌ها", "url": "#", "icon": "calendar", "active": False},
            {"label": "نیروها", "url": "#", "icon": "users", "active": False, "badge": "۳"},
            {"label": "حسابداری", "url": "#", "icon": "receipt", "active": False},
            {"label": "انبار", "url": "#", "icon": "box", "active": False},
            {"label": "تنظیمات", "url": "#", "icon": "settings", "active": False},
        ],
        "unread_count": 4,
        "grid": grid,
        "calendar_events": events,
        "calendar_title": "مرداد ۱۴۰۴",
        "today": d(12),
        "rows": rows,
        "columns": [
            {"key": "name", "label": "مراسم"},
            {"key": "date", "label": "تاریخ", "format": "jdate"},
            {"key": "staff", "label": "نیرو", "format": "fa", "num": True},
            {"key": "income", "label": "درآمد", "format": "toman", "num": True},
            {"key": "status", "label": "وضعیت"},
            {"key": None, "label": "عملیات", "actions": True},
        ],
        "demo_page": DemoPage(page=3, pages=12, total=118),
        "demo_datetime": datetime(2025, 8, 3, 18, 30),
        "icon_names": sorted(_icon_names()),
    }


def _icon_names() -> list[str]:
    import re

    sprite = (ROOT / "app" / "templates" / "components" / "_icons.html").read_text(encoding="utf-8")
    return [n for n in re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite)]


def build() -> Path:
    app = make_app([HERE])
    with app.test_request_context("/sg"):
        html = render_template("styleguide_page.html", **demo_context())
    # file:// friendly: the page lives in docs/, the assets in app/static/.
    html = html.replace('="/static/', '="../app/static/')
    html = html.replace('href="/"', 'href="#"')
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    out = build()
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
