"""Real-browser checks of the marketplace (Chromium via Playwright, skipped when it is not available).

The Flask app is served on an ephemeral port by werkzeug in a thread (file SQLite database, CSRF on) and the
browser carries a genuine session cookie.  Verified: the bonus-tier editor and the live pay example on the post
form (the number comes from ``payroll.compute_pay`` on the server), the preset refill when the role changes, a post
created and an application sent through real forms with CSRF, the share box, and that no page scrolls
horizontally at phone width for any role.
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
from app.models import Assignment, Atelier, Event, Hall, JobApplication, JobPost, StaffProfile, User  # noqa: E402
from app.services.constants import (  # noqa: E402
    ApplicationStatus,
    AssignmentStatus,
    EventStatus,
    Role,
    Skill,
    UserStatus,
)
from app.services.testing import login_client  # noqa: E402

expect = sync_api.expect
PHONE = {"width": 390, "height": 844}
TERMS = {"base": 1_000_000, "commission_per_unit": 9_000, "bonus_tiers": [{"threshold": 70, "amount": 150_000}]}


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
    root = tmp_path_factory.mktemp("marketplace_browser")
    app = create_app(
        "testing",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{root / 'live.db'}",
        UPLOAD_DIR=str(root / "uploads"),
        WTF_CSRF_ENABLED=True,
    )
    context = app.app_context()
    context.push()
    db.create_all()
    today = dt.date.today()

    def user(number, name, role, **fields):
        row = User(mobile=f"0912000{number:04d}", full_name=name, role=role, status=UserStatus.ACTIVE, **fields)
        db.session.add(row)
        db.session.commit()
        return row

    manager = user(1, "مدیر اتلیه", Role.ATELIER_MANAGER, is_verified=True)
    hall_manager = user(2, "مدیر تالار", Role.HALL_MANAGER)
    staff = user(3, "سارا نیرو", Role.STAFF, city="شیراز")
    other = user(4, "رضا ادیتور", Role.STAFF, city="شیراز")
    atelier = Atelier(manager=manager, name="اتلیه ستاره", city="شیراز", phone="07112345678",
                      default_pay_presets={"photographer": TERMS, "seller": {"base": 400_000, "commission_per_unit": 3_000, "bonus_tiers": []}})
    hall = Hall(manager=hall_manager, name="تالار گل‌ها", city="شیراز")
    db.session.add_all([atelier, hall])
    db.session.commit()
    event = Event(hall=hall, atelier=atelier, date=today + dt.timedelta(days=5), status=EventStatus.CONFIRMED, title="عروسی")
    past = Event(hall=hall, atelier=atelier, date=today - dt.timedelta(days=3), status=EventStatus.DONE, title="عروسی گذشته")
    db.session.add_all([event, past])
    db.session.commit()
    for person, skills in ((staff, ["photographer", "seller"]), (other, ["editor"])):
        profile = StaffProfile(user=person, bio="عکاس با سابقه", experience_years=4, city="شیراز", is_listed=True)
        profile.skills = skills
        db.session.add(profile)
    post = JobPost(atelier=atelier, event=event, role=Skill.PHOTOGRAPHER, date=event.date, city="شیراز", title="عکاس برای عروسی",
                   description="ساعت ۱۸ در تالار", slots=1, **{"base_pay": 1_000_000, "commission_per_unit": 9_000})
    post.apply_terms(TERMS)
    second = JobPost(atelier=atelier, role=Skill.EDITOR, date=today + dt.timedelta(days=9), city="شیراز", title="ادیتور", slots=2,
                     base_pay=500_000, commission_per_unit=2_000)
    db.session.add_all([post, second])
    db.session.commit()
    application = JobApplication(post=post, user=staff, message="سلام، آماده‌ام", status=ApplicationStatus.PENDING)
    done = Assignment(event=past, role=Skill.PHOTOGRAPHER, user=other, status=AssignmentStatus.COMPLETED, base_pay=1, commission_per_unit=1)
    db.session.add_all([application, done])
    db.session.commit()
    ids = {
        "post": post.id, "second": second.id, "application": application.id, "staff": staff.id, "other": other.id,
        "done": done.id, "event": event.id,
    }
    cookies = {}
    for key, person in (("manager", manager), ("staff", staff), ("hall", hall_manager)):
        client = app.test_client()
        login_client(client, person)
        cookies[key] = client.get_cookie("atelier_session").value
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"base": f"http://127.0.0.1:{server.server_port}", "cookies": cookies, "ids": ids}
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


def open_page(browser, live, who, viewport=PHONE):
    context = browser.new_context(viewport=viewport, locale="fa-IR")
    if who:
        context.add_cookies([{"name": "atelier_session", "value": live["cookies"][who], "url": live["base"]}])
    tab = context.new_page()
    tab.set_default_timeout(8000)
    return context, tab


@pytest.fixture
def manager_page(browser, live):
    context, tab = open_page(browser, live, "manager")
    yield tab
    context.close()


@pytest.fixture
def staff_page(browser, live):
    context, tab = open_page(browser, live, "staff")
    yield tab
    context.close()


# --------------------------------------------------------------------------
# the post form
# --------------------------------------------------------------------------


def test_tier_editor_adds_and_removes_rows(manager_page, live):
    manager_page.goto(f"{live['base']}/jobs/new?role=photographer")
    rows = manager_page.locator("[data-tier-row]")
    assert rows.count() == 1  # the preset's tier
    manager_page.get_by_role("button", name="افزودن پله").click()
    assert rows.count() == 2
    row = manager_page.locator("[data-tier-row]").nth(1)
    row.locator("input[name='tier_threshold[]']").fill("۱۰۰")
    row.locator("[data-money]").fill("300000")
    assert row.locator("input[type=hidden][name='tier_amount[]']").input_value() == "300000"
    manager_page.locator("[data-tier-remove]").nth(1).click()
    assert rows.count() == 1


def test_the_live_example_is_computed_by_the_server(manager_page, live):
    manager_page.goto(f"{live['base']}/jobs/new?role=photographer")
    line = manager_page.locator("[data-example-line]")
    expect(line).to_have_text("اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان")  # the AGENTS.md vector
    manager_page.locator("#f-base").fill("2000000")
    expect(line).to_have_text("اگر ۸۰ عکس فروخته شود: ۲٬۸۷۰٬۰۰۰ تومان")
    manager_page.locator("[data-tier-remove]").first.click()
    expect(line).to_have_text("اگر ۸۰ عکس فروخته شود: ۲٬۷۲۰٬۰۰۰ تومان")


def test_changing_the_role_refills_the_terms_from_the_atelier_preset(manager_page, live):
    manager_page.goto(f"{live['base']}/jobs/new")
    manager_page.select_option("#f-role", "seller")
    expect(manager_page.locator("input[type=hidden][name='base']")).to_have_value("400000")
    expect(manager_page.locator("input[type=hidden][name='commission']")).to_have_value("3000")


def test_a_typed_amount_is_not_overwritten_by_a_preset(manager_page, live):
    manager_page.goto(f"{live['base']}/jobs/new")
    manager_page.locator("#f-base").fill("123000")
    manager_page.select_option("#f-role", "seller")
    manager_page.wait_for_timeout(400)
    assert manager_page.locator("input[type=hidden][name='base']").input_value() == "123000"


def test_creating_a_post_through_the_form_with_csrf(manager_page, live):
    manager_page.goto(f"{live['base']}/jobs/new?role=editor")
    manager_page.locator("#f-title").fill("ادیتور برای جمعه")
    tomorrow = (dt.date.today() + dt.timedelta(days=12)).isoformat()
    manager_page.evaluate("(iso) => Atelier.setValue('#f-date', iso)", tomorrow)
    manager_page.locator("#f-base").fill("700000")
    manager_page.get_by_role("button", name="انتشار آگهی").click()
    manager_page.wait_for_url("**/applicants")
    assert db.session.query(JobPost).filter_by(title="ادیتور برای جمعه").count() == 1
    post = db.session.query(JobPost).filter_by(title="ادیتور برای جمعه").one()
    assert post.date.isoformat() == tomorrow and post.base_pay == 700_000


# --------------------------------------------------------------------------
# applying and the share box
# --------------------------------------------------------------------------


def test_apply_through_the_browser(browser, live):
    context, page = open_page(browser, live, "staff")
    try:
        page.goto(f"{live['base']}/jobs/{live['ids']['second']}")
        assert page.locator("textarea[name=message]").count() == 0  # staff lacks the editor skill: reason, not a form
        assert "مهارت «ادیتور»" in page.content()
        page.goto(f"{live['base']}/jobs/{live['ids']['post']}")
        assert "در انتظار بررسی" in page.content()  # the fixture application already exists
    finally:
        context.close()


def test_share_box_copies_the_link(staff_page, live):
    staff_page.goto(f"{live['base']}/jobs/{live['ids']['post']}")
    staff_page.evaluate("() => { window.__copied = null; navigator.clipboard.writeText = (t) => { window.__copied = t; return Promise.resolve(); }; }")
    staff_page.get_by_role("button", name="کپی پیوند").click()
    expect(staff_page.locator(".toast")).to_contain_text("کپی شد")
    assert staff_page.evaluate("() => window.__copied").endswith(f"/jobs/{live['ids']['post']}")


def test_the_filters_are_open_on_a_wide_screen_and_collapsed_on_a_phone(browser, live):
    context, desktop = open_page(browser, live, None, {"width": 1280, "height": 900})
    try:
        desktop.goto(f"{live['base']}/jobs")
        assert desktop.locator("[data-mk-filters]").evaluate("el => el.open") is True
    finally:
        context.close()
    context, phone = open_page(browser, live, None)
    try:
        phone.goto(f"{live['base']}/jobs")
        assert phone.locator("[data-mk-filters]").evaluate("el => el.open") is False
    finally:
        context.close()


# --------------------------------------------------------------------------
# phone width
# --------------------------------------------------------------------------

MANAGER_PAGES = [
    "/jobs", "/jobs/{post}", "/jobs/manage", "/jobs/manage?tab=all", "/jobs/manage/inbox", "/jobs/{post}/applicants",
    "/jobs/manage/applications/{application}", "/jobs/new", "/jobs/new?role=photographer&event={event}", "/jobs/{post}/edit",
    "/talent", "/talent/{staff}", "/talent/{staff}/invite", "/jobs/reviews", "/jobs/reviews/new/{done}",
]
STAFF_PAGES = ["/jobs", "/jobs/{post}", "/jobs/applications", "/jobs/invitations", "/jobs/reviews"]
ANONYMOUS_PAGES = ["/jobs", "/jobs/{post}"]


def check_no_overflow(page, live, path):
    page.goto(live["base"] + path.format(**live["ids"]))
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"{path} overflows by {overflow}px at 390px"
    # Elements inside a deliberate scroll/clip container (tab strips, table wrappers, visually-hidden table heads)
    # may extend past the viewport; everything else must fit.
    offenders = page.evaluate(
        """() => {
            const clipped = (el) => {
                for (let p = el.parentElement; p && p.tagName !== 'MAIN'; p = p.parentElement) {
                    if (getComputedStyle(p).overflowX !== 'visible') return true;
                }
                return false;
            };
            return Array.from(document.querySelectorAll('main *')).filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && (r.right > innerWidth + 1 || r.left < -1) && !clipped(el);
            }).map(el => el.tagName.toLowerCase() + '.' + String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className).split(' ').join('.'));
        }"""
    )
    assert offenders == [], f"{path}: elements stick out of the viewport: {offenders}"


@pytest.mark.parametrize("path", MANAGER_PAGES)
def test_manager_pages_do_not_scroll_horizontally_on_a_phone(manager_page, live, path):
    check_no_overflow(manager_page, live, path)


@pytest.mark.parametrize("path", STAFF_PAGES)
def test_staff_pages_do_not_scroll_horizontally_on_a_phone(staff_page, live, path):
    check_no_overflow(staff_page, live, path)


@pytest.mark.parametrize("path", ANONYMOUS_PAGES)
def test_public_pages_do_not_scroll_horizontally_on_a_phone(browser, live, path):
    context, page = open_page(browser, live, None)
    try:
        check_no_overflow(page, live, path)
    finally:
        context.close()


def test_no_console_errors_on_the_main_pages(browser, live):
    context, page = open_page(browser, live, "manager")
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    try:
        for path in ("/jobs", "/jobs/new?role=photographer", "/jobs/manage", "/talent"):
            page.goto(live["base"] + path)
            page.wait_for_timeout(300)
    finally:
        context.close()
    assert errors == []
