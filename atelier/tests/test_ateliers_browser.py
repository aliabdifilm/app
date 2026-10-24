"""Real-browser checks of the atelier panel (Chromium via Playwright, skipped when it is not available).

The Flask app is served on an ephemeral port by werkzeug in a thread (file SQLite database, CSRF on), the
browser carries a genuine session cookie.  Verified: the bonus-tier editor (add / remove rows), the live pay
example (server computed through ``payroll.compute_pay``), a full afish submit with CSRF, the share menu, and
that no page scrolls horizontally at phone width.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
import threading
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed (pip install playwright)")

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Assignment, Atelier, Event, Hall, RosterMember, User  # noqa: E402
from app.services.constants import EventStatus, Role, UserStatus  # noqa: E402
from app.services.testing import login_client  # noqa: E402

expect = sync_api.expect
PHONE = {"width": 390, "height": 844}


def _chromium() -> str | None:
    env = os.environ.get("CHROMIUM_BIN")
    if env and Path(env).exists():
        return env
    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if _chromium() is None:
        pytest.skip("no Chromium binary found (set CHROMIUM_BIN)")
    root = tmp_path_factory.mktemp("ateliers_browser")
    app = create_app(
        "testing",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{root / 'live.db'}",
        UPLOAD_DIR=str(root / "uploads"),
        WTF_CSRF_ENABLED=True,
    )
    context = app.app_context()
    context.push()
    db.create_all()
    manager = User(mobile="09120000001", full_name="مدیر اتلیه", role=Role.ATELIER_MANAGER, status=UserStatus.ACTIVE)
    hall_manager = User(mobile="09120000002", full_name="مدیر تالار", role=Role.HALL_MANAGER, status=UserStatus.ACTIVE)
    db.session.add_all([manager, hall_manager])
    db.session.commit()
    atelier = Atelier(
        manager=manager,
        name="اتلیه ستاره",
        city="شیراز",
        default_pay_presets={
            "photographer": {"base": 1_000_000, "commission_per_unit": 9_000, "bonus_tiers": [{"threshold": 70, "amount": 150_000}]}
        },
    )
    hall = Hall(manager=hall_manager, name="تالار گل‌ها", city="شیراز")
    db.session.add_all([atelier, hall])
    db.session.commit()
    event = Event(hall=hall, atelier=atelier, date=dt.date.today() + dt.timedelta(days=5), status=EventStatus.CONFIRMED, title="عروسی")
    member = RosterMember(atelier=atelier, display_name="رضا عکاس", skills=["photographer"])
    db.session.add_all([event, member])
    db.session.commit()
    client = app.test_client()
    login_client(client, manager)
    cookie = client.get_cookie("atelier_session")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base": f"http://127.0.0.1:{server.server_port}",
            "cookie": cookie.value,
            "event_id": event.id,
            "member_id": member.id,
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        db.session.remove()
        db.engine.dispose()
        context.pop()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        instance = p.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture
def page(browser, live):
    context = browser.new_context(viewport=PHONE, locale="fa-IR")
    context.add_cookies([{"name": "atelier_session", "value": live["cookie"], "url": live["base"]}])
    tab = context.new_page()
    tab.set_default_timeout(8000)
    yield tab
    context.close()


def assign_url(live):
    return f"{live['base']}/atelier/events/{live['event_id']}/afish/assign?role=photographer&member={live['member_id']}"


def test_tier_editor_adds_and_removes_rows(page, live):
    page.goto(assign_url(live))
    rows = page.locator("[data-tier-row]")
    assert rows.count() == 1  # the preset's tier
    page.get_by_role("button", name="افزودن پله").click()
    assert rows.count() == 2
    page.locator("[data-tier-row]").nth(1).locator("input[name='tier_threshold[]']").fill("۱۰۰")
    page.locator("[data-tier-row]").nth(1).locator("[data-money]").fill("300000")
    # the money widget formats with Persian digits and keeps the raw integer in a hidden twin
    assert page.locator("[data-tier-row]").nth(1).locator("input[type=hidden][name='tier_amount[]']").input_value() == "300000"
    page.locator("[data-tier-remove]").nth(1).click()
    assert rows.count() == 1
    page.locator("[data-tier-remove]").first.click()  # the last row is cleared, not removed
    assert rows.count() == 1
    assert page.locator("input[type=hidden][name='tier_amount[]']").input_value() == ""


def test_pay_example_is_computed_by_the_server_and_follows_the_inputs(page, live):
    page.goto(assign_url(live))
    total = page.locator("[data-example-total]")
    assert total.inner_text() == "۱٬۸۷۰٬۰۰۰"  # AGENTS.md vector: 1,000,000 + 80 x 9,000 + 150,000
    page.locator("#f-base").fill("2000000")
    expect(total).to_have_text("۲٬۸۷۰٬۰۰۰")  # the CSP forbids eval, so wait with expect() instead of wait_for_function
    page.locator("[data-tier-remove]").first.click()  # no bonus tier any more
    expect(total).to_have_text("۲٬۷۲۰٬۰۰۰")


def test_afish_submit_through_the_browser_with_csrf(page, live):
    page.goto(assign_url(live))
    page.get_by_role("button", name="افیش و قطعی کردن").click()
    page.wait_for_url(f"**/atelier/events/{live['event_id']}**")
    assert "رضا عکاس" in page.content()
    assert db.session.query(Assignment).count() == 1
    assignment = db.session.query(Assignment).one()
    assert (assignment.base_pay, assignment.commission_per_unit) == (1_000_000, 9_000)
    assert assignment.bonus_tiers == [{"threshold": 70, "amount": 150_000}]


def test_share_menu_copies_the_afish_text(page, live):
    page.goto(f"{live['base']}/atelier/events/{live['event_id']}")
    page.get_by_text("ارسال افیش").click()
    copy = page.locator("[data-copy-text]")
    assert "افیش مراسم" in copy.get_attribute("data-copy-text")
    page.evaluate("() => { window.__copied = null; navigator.clipboard.writeText = (t) => { window.__copied = t; return Promise.resolve(); }; }")
    copy.click()
    expect(page.locator(".toast")).to_contain_text("کپی شد")
    assert "اتلیه ستاره" in page.evaluate("() => window.__copied")


@pytest.mark.parametrize(
    "path",
    [
        "/atelier/",
        "/atelier/afish",
        "/atelier/events?tab=upcoming",
        "/atelier/calendar",
        "/atelier/roster",
        "/atelier/roster/new",
        "/atelier/settings",
        "/atelier/events/{event}",
        "/atelier/events/{event}/afish/new?role=photographer",
        "/atelier/events/{event}/afish/assign?role=photographer&member={member}",
    ],
)
def test_no_horizontal_scroll_at_phone_width(page, live, path):
    page.goto(live["base"] + path.format(event=live["event_id"], member=live["member_id"]))
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"{path} overflows by {overflow}px at 390px"
    offenders = page.evaluate(
        """() => Array.from(document.querySelectorAll('main *')).filter(el => {
            const r = el.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1 || r.left < -1; }).length"""
    )
    assert offenders == 0
