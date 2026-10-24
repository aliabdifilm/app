"""Real-browser checks of the hall panel (Chromium via Playwright, skipped when it is not available).

The Flask app is served on an ephemeral port by werkzeug in a thread (file SQLite database, CSRF on) and the
browser carries a genuine session cookie.  Verified: the Jalali multi-date picker (select, chips, whole month,
rules expanded by the server, Esfand with 29 or 30 days), that the picked dates really become events, the bulk
selection bar, the atelier selection that survives a search, and that no page scrolls horizontally at phone width.
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
from app.models import Atelier, Event, Hall, User  # noqa: E402
from app.services import event_offers, security  # noqa: E402
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
    root = tmp_path_factory.mktemp("halls_browser")
    app = create_app(
        "testing",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{root / 'live.db'}",
        UPLOAD_DIR=str(root / "uploads"),
        WTF_CSRF_ENABLED=True,
    )
    context = app.app_context()
    context.push()
    db.create_all()
    today = security.today()
    hall_manager = User(mobile="09120000002", full_name="مدیر تالار", role=Role.HALL_MANAGER, status=UserStatus.ACTIVE)
    star_manager = User(mobile="09120000003", full_name="مدیر ستاره", role=Role.ATELIER_MANAGER, status=UserStatus.ACTIVE, is_verified=True)
    rival_manager = User(mobile="09120000004", full_name="مدیر رقیب", role=Role.ATELIER_MANAGER, status=UserStatus.ACTIVE)
    db.session.add_all([hall_manager, star_manager, rival_manager])
    db.session.commit()
    hall = Hall(manager=hall_manager, name="تالار گل‌ها", city="شیراز")
    star = Atelier(manager=star_manager, name="اتلیه ستاره", city="شیراز")
    rival = Atelier(manager=rival_manager, name="اتلیه رقیب", city="شیراز")
    db.session.add_all([hall, star, rival])
    db.session.commit()
    events = []
    for offset, hour in ((3, 18), (5, 13), (5, 19), (9, 20)):
        event = Event(hall=hall, date=today + dt.timedelta(days=offset), start_time=dt.time(hour, 0), status=EventStatus.DRAFT)
        db.session.add(event)
        events.append(event)
    db.session.commit()
    event_offers.offer_events([events[0]], [star], hall_manager)
    db.session.commit()
    client = app.test_client()
    login_client(client, hall_manager)
    cookie = client.get_cookie("atelier_session")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base": f"http://127.0.0.1:{server.server_port}",
            "cookie": cookie.value,
            "hall_id": hall.id,
            "event_ids": [event.id for event in events],
            "star_id": star.id,
            "rival_id": rival.id,
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


def open_picker(page, live):
    page.goto(f"{live['base']}/hall/events/bulk")
    page.wait_for_selector("[data-multidate][data-ready] .hl-md__day[data-iso]")


def enabled_days(page):
    return page.locator(".hl-md__day[data-iso]:not([disabled])")


# --------------------------------------------------------------------------
# the multi-date picker
# --------------------------------------------------------------------------


def test_picking_dates_shows_chips_and_removes_them(page, live):
    open_picker(page, live)
    expect(page.locator(".hl-md__count")).to_contain_text("هنوز تاریخی")
    days = enabled_days(page)
    days.nth(0).click()
    days.nth(1).click()
    days.nth(2).click()
    expect(page.locator(".hl-md__count")).to_contain_text("۳ تاریخ")
    assert page.locator(".hl-md__chips .hl-chip").count() == 3
    assert page.locator("input[name='dates']").count() == 3
    assert page.locator(".hl-md__day[aria-pressed='true']").count() == 3
    page.locator(".hl-md__chips .hl-chip button").first.click()
    expect(page.locator(".hl-md__count")).to_contain_text("۲ تاریخ")
    days.nth(1).click()  # clicking a selected day again unselects it
    expect(page.locator(".hl-md__count")).to_contain_text("۱ تاریخ")


def test_whole_month_and_clear_tools(page, live):
    open_picker(page, live)
    page.get_by_role("button", name="انتخاب همهٔ روزهای این ماه").click()
    count = page.locator("input[name='dates']").count()
    assert 1 <= count <= 31
    assert count == enabled_days(page).count()  # past days of the month are not selectable
    page.get_by_role("button", name="پاک کردن همه").click()
    assert page.locator("input[name='dates']").count() == 0


def test_past_days_cannot_be_selected(page, live):
    open_picker(page, live)
    disabled = page.locator(".hl-md__day[data-iso][disabled]")
    today = page.evaluate("Atelier.jalali.todayJalali()[2]")
    assert disabled.count() == today - 1  # every day before today in the current month


def test_picked_dates_are_created_as_events(page, live):
    open_picker(page, live)
    far = page.evaluate(
        """() => {
          const J = window.AtelierJalali, t = J.todayJalali();
          const g = J.toGregorian(t[0], t[1], 1);
          return J.isoString(g[0], g[1], g[2]);
        }"""
    )
    assert far
    page.get_by_role("button", name="ماه بعد").click()
    days = enabled_days(page)
    picked = [days.nth(0).get_attribute("data-iso"), days.nth(2).get_attribute("data-iso")]
    days.nth(0).click()
    days.nth(2).click()
    page.get_by_label("ساعت شروع", exact=True).fill("20:15")
    page.get_by_role("button", name="ثبت مراسم‌ها").click()
    page.wait_for_url("**/hall/events?tab=free**")
    expect(page.locator(".toast, [data-flash] li").first).to_be_attached()
    db.session.expire_all()
    for iso in picked:
        rows = db.session.query(Event).filter(Event.hall_id == live["hall_id"], Event.date == dt.date.fromisoformat(iso), Event.start_time == dt.time(20, 15)).all()
        assert len(rows) == 1


def test_a_rule_is_expanded_by_the_server_into_thursdays_and_fridays(page, live):
    open_picker(page, live)
    page.evaluate(
        """() => {
          const J = window.AtelierJalali, t = J.todayJalali();
          const a = J.toGregorian(t[0], t[1], t[2]);
          const end = new Date(Date.UTC(a[0], a[1] - 1, a[2] + 30));
          Atelier.setValue('rule_from', J.isoString(a[0], a[1], a[2]));
          Atelier.setValue('rule_to', J.isoString(end.getUTCFullYear(), end.getUTCMonth() + 1, end.getUTCDate()));
        }"""
    )
    page.get_by_role("button", name="افزودن به انتخاب").click()
    expect(page.locator(".hl-md__count")).not_to_contain_text("هنوز")
    weekdays = page.evaluate(
        """() => Array.from(document.querySelectorAll("input[name='dates']")).map(i => {
          const p = i.value.split('-').map(Number); return window.AtelierJalali.weekdayIndex(p[0], p[1], p[2]); })"""
    )
    assert weekdays and set(weekdays) <= {5, 6}
    # the pattern fields are only helpers: they are not posted with the form
    page.evaluate("document.querySelector('[data-bulk-form]').addEventListener('submit', e => { window.__posted = Array.from(new FormData(e.target).keys()); e.preventDefault(); }, true)")
    page.get_by_role("button", name="ثبت مراسم‌ها").click()
    posted = page.evaluate("window.__posted")
    assert "dates" in posted and not any(name.startswith("rule_") for name in posted)


def test_esfand_has_29_days_in_a_common_year_and_30_in_a_leap_year(page, live):
    open_picker(page, live)

    def go_to_esfand(year_hint):
        for _ in range(60):
            title = page.locator(".hl-md__title").inner_text()
            if title.startswith("اسفند") and title.endswith(year_hint):
                return
            page.get_by_role("button", name="ماه بعد").click()
        raise AssertionError("Esfand not reached")

    this_year = page.evaluate("window.AtelierJalali.todayJalali()[0]")
    leap_year = next(y for y in range(this_year, this_year + 8) if page.evaluate(f"window.AtelierJalali.isLeap({y})"))
    common_year = next(y for y in range(this_year, this_year + 8) if not page.evaluate(f"window.AtelierJalali.isLeap({y})"))
    for year, expected in sorted(((common_year, 29), (leap_year, 30))):
        fa = page.evaluate(f"window.Atelier.toFaDigits('{year}')")
        go_to_esfand(fa)
        assert page.locator(".hl-md__day[data-iso]").count() == expected, year
        last = page.locator(".hl-md__day[data-iso]").last
        assert "اسفند" in last.get_attribute("aria-label")
        iso = last.get_attribute("data-iso")
        assert page.evaluate(f"(() => {{ const p = '{iso}'.split('-').map(Number); return window.AtelierJalali.toJalali(p[0], p[1], p[2]).join('/'); }})()") == f"{year}/12/{expected}"


def test_typed_dates_without_the_calendar_still_work(page, live):
    page.goto(f"{live['base']}/hall/events/bulk")
    page.locator("textarea[name='dates_text']").fill("1404/12/30")
    page.get_by_role("button", name="ثبت مراسم‌ها").click()
    expect(page.locator(".field__error").first).to_contain_text("اسفند")


# --------------------------------------------------------------------------
# lists and the offer page
# --------------------------------------------------------------------------


def test_the_bulk_bar_follows_the_selection(page, live):
    page.goto(f"{live['base']}/hall/events?tab=free")
    bar = page.locator("[data-bulkbar]")
    expect(bar).to_be_hidden()
    page.locator("[data-bulk-pick]").first.check()
    expect(bar).to_be_visible()
    gap = page.evaluate(
        """() => document.querySelector('.tabbar').getBoundingClientRect().top - document.querySelector('[data-bulkbar]').getBoundingClientRect().bottom"""
    )
    assert gap >= -1, "the action bar is hidden behind the tab bar"
    expect(page.locator("[data-bulkbar] [data-bulk-count]")).to_have_text("۱")
    page.locator("[data-bulk-all]").check()
    count = page.locator("[data-bulk-pick]").count()
    expect(page.locator("[data-bulkbar] [data-bulk-count]")).to_have_text(page.evaluate(f"Atelier.toFaDigits('{count}')"))
    page.locator("[data-bulk-all]").uncheck()
    expect(bar).to_be_hidden()


def test_atelier_choice_survives_a_search(page, live):
    ids = live["event_ids"]
    page.goto(f"{live['base']}/hall/offers/new?event={ids[1]}&event={ids[2]}")
    page.locator(f"#at-{live['star_id']}").check()
    page.get_by_placeholder("نام، شهر یا توضیح").fill("رقیب")
    page.get_by_role("button", name="جستجو").click()
    page.wait_for_load_state()
    assert f"atelier={live['star_id']}" in page.url
    expect(page.locator(f"#at-{live['star_id']}")).to_be_checked()  # still ticked in the "chosen" section
    expect(page.locator(f"#at-{live['rival_id']}")).to_be_visible()


def test_copy_button_gives_feedback(page, live):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live["base"])
    page.goto(f"{live['base']}/hall/events/{live['event_ids'][0]}")
    page.get_by_role("button", name="کپی اطلاعات مراسم").click()
    expect(page.locator(".toast")).to_contain_text("کپی شد")


# --------------------------------------------------------------------------
# phone width
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/hall/",
        "/hall/calendar",
        "/hall/events",
        "/hall/events?tab=free",
        "/hall/events/new",
        "/hall/events/bulk",
        "/hall/events/{event}",
        "/hall/offers",
        "/hall/offers/new",
        "/hall/offers/new?event={event}",
        "/hall/ateliers",
        "/hall/ateliers/{star}",
        "/hall/reports",
        "/hall/agreements",
        "/hall/agreements/new?atelier={star}",
        "/hall/halls",
        "/hall/halls/{hall}",
        "/hall/halls/{hall}/edit",
    ],
)
def test_no_horizontal_scroll_and_big_tap_targets_at_390px(path, page, live):
    url = live["base"] + path.format(event=live["event_ids"][0], star=live["star_id"], hall=live["hall_id"])
    page.goto(url)
    page.wait_for_load_state("networkidle")
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"{path} scrolls sideways by {overflow}px"
    small = page.evaluate(
        """() => Array.from(document.querySelectorAll('main a.btn, main button.btn, main .tabs__tab, main select, main input:not([type=hidden]):not([type=checkbox]):not([type=radio])'))
          .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 36; })
          .map(el => el.outerHTML.slice(0, 80))"""
    )
    assert not small, small
