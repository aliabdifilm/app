"""The invoice popup in a real browser (Chromium via Playwright): full-screen sheet on a phone, live numbers
from the server, "+ other size" rows with the weight stepper, the venue-rent default link, saving from the
modal, validation errors that keep the user in the popup, and the hidden-by-default invoice accordion.

Skipped with a reason when Playwright or a Chromium binary is missing (see tests/test_ui_browser.py).
The Flask app is served on an ephemeral port by a throw-away werkzeug server inside the test.
"""

from __future__ import annotations

import glob
import os
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed (pip install playwright)")

from werkzeug.serving import make_server  # noqa: E402

from app.extensions import db  # noqa: E402
from app.models import Invoice  # noqa: E402
from app.services.testing import login_client  # noqa: E402
from test_accounting_support import studio  # noqa: E402,F401

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 900}
INDEX = "/accounting/?y=1404&m=4"


def _chromium() -> str | None:
    env = os.environ.get("CHROMIUM_BIN")
    if env and Path(env).exists():
        return env
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome", os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium(), args=["--no-sandbox"])
        except Exception as exc:  # no browser binary
            pytest.skip(f"Chromium is not available ({str(exc).splitlines()[0]}); set CHROMIUM_BIN")
        yield b
        b.close()


@pytest.fixture
def live(csrf_app, studio):
    """The app on 127.0.0.1:<free port> with CSRF protection ON; yields (base_url, session cookie, user agent)."""
    server = make_server("127.0.0.1", 0, csrf_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    probe = csrf_app.test_client()
    login_client(probe, studio.manager)
    cookie = probe.get_cookie(csrf_app.config["SESSION_COOKIE_NAME"])
    try:
        yield f"http://127.0.0.1:{server.server_port}", cookie, probe.environ_base.get("HTTP_USER_AGENT", "")
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def page_for(browser, live):
    base, cookie, agent = live
    contexts = []

    def make(viewport):
        mobile = viewport["width"] < 600
        ctx = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile, locale="fa-IR", user_agent=agent)
        ctx.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base}])
        contexts.append(ctx)
        page = ctx.new_page()
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
        page.goto(base + INDEX)
        return page

    yield make
    for ctx in contexts:
        ctx.close()


def units(page) -> str:
    return page.locator("[data-sum='units']").first.inner_text().strip()


def wait_units(page, expected: str) -> None:
    page.wait_for_function(
        "e => { const n = document.querySelector('#invoice-modal [data-sum=\"units\"]'); return n && n.textContent.trim() === e; }",
        arg=expected,
        timeout=8000,
    )


def open_popup(page):
    page.click("a[data-invoice-open]")
    page.wait_for_selector("#invoice-modal [data-invoice-form]")
    page.wait_for_selector("#invoice-modal.is-open")


def test_the_popup_is_a_full_screen_sheet_on_a_phone(page_for):
    page = page_for(PHONE)
    open_popup(page)
    box = page.locator("#invoice-modal .modal__dialog").bounding_box()
    assert box["width"] == PHONE["width"] and box["height"] >= PHONE["height"] - 1
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not page.evaluate("document.querySelector('#invoice-modal .modal__body').scrollWidth > document.querySelector('#invoice-modal .modal__body').clientWidth + 1")
    # the products come in the AGENTS.md order
    labels = page.locator("#invoice-modal .inv-group__title").all_inner_texts()
    assert labels[:4] == ["چاپ", "فایل", "قاب", "شاسی"]
    for text in ("چاپ ۱۶×۲۱", "چاپ ۲۰×۳۰", "چاپ ۳۰×۴۰", "فایل خام", "فایل ادیت", "چاپ ابعاد دیگر", "قاب ابعاد دیگر", "شاسی ابعاد دیگر"):
        assert text in page.locator("#invoice-modal").inner_text()
    assert page.problems == []


def test_live_numbers_come_from_the_server(page_for):
    page = page_for(DESKTOP)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "80")
    wait_units(page, "۸۰")
    people = page.locator("#invoice-modal [data-sum-people] .inv-person")
    assert people.count() == 4
    assert people.first.inner_text().count("۱٬۸۷۰٬۰۰۰") >= 1  # base + commission + bonus = 1,870,000 for everybody
    assert "۱۵۰٬۰۰۰" in people.first.inner_text() and "پلهٔ ۷۰+" in people.first.inner_text()
    page.fill("#invoice-modal #f-qty__print__16x21", "69")
    wait_units(page, "۶۹")
    assert "۱٬۶۲۱٬۰۰۰" in page.locator("#invoice-modal [data-sum-people]").inner_text()  # 69 units: no bonus
    assert "پلهٔ" not in page.locator("#invoice-modal [data-sum-people]").inner_text()
    assert page.problems == []


def test_other_size_rows_weight_and_the_rent_default_link(page_for):
    page = page_for(DESKTOP)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "10")
    page.click("#invoice-modal [data-add-custom='print']")
    row = page.locator("#invoice-modal [data-custom-rows='print'] [data-custom-row]")
    assert row.count() == 1
    assert "تعداد محاسبه برای حقوق نیرو" in row.inner_text()
    assert row.locator("[data-stepper]").count() == 2
    row.locator("input[name$='__size']").fill("۵۰×۷۰")
    row.locator(".stepper").nth(0).locator("[data-stepper-inc]").click()  # qty 1
    row.locator(".stepper").nth(0).locator("[data-stepper-inc]").click()  # qty 2
    row.locator(".stepper").nth(1).locator("[data-stepper-inc]").click()  # weight 2 (default is 1)
    wait_units(page, "۱۴")  # 10 + 2 x 2
    row.locator("[data-remove-row]").click()
    wait_units(page, "۱۰")
    assert page.locator("#invoice-modal [data-custom-row]").count() == 0
    # venue rent: default 3,000,000; the "restore" link only shows once it differs
    restore = page.locator("#invoice-modal [data-restore-default]")
    assert restore.is_hidden()
    page.fill("#invoice-modal #f-venue_rent", "5000000")
    restore.wait_for(state="visible")
    restore.click()
    assert page.locator("#invoice-modal #f-venue_rent").input_value() == "۳٬۰۰۰٬۰۰۰"
    assert restore.is_hidden()
    assert page.problems == []


def test_saving_from_the_popup_then_the_closed_accordion(page_for, studio):
    page = page_for(DESKTOP)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "80")
    page.fill("#invoice-modal #f-pay__cash", "20000000")
    wait_units(page, "۸۰")
    page.click("#invoice-modal .inv-actions button[type=submit]")
    page.wait_for_selector("a:has-text('ویرایش فاکتور')")
    assert page.url.endswith(f"#event-{studio.event.id}") or "accounting" in page.url
    invoice = db.session.scalar(sa.select(Invoice))
    db.session.refresh(invoice)
    assert invoice.units_total == 80 and invoice.income_total == 20_000_000
    assert [l.total for l in invoice.payroll_lines] == [1_870_000] * 4
    # the invoice sits under the card, closed by default; the «فاکتور» button slides it open
    panel = page.locator(f"#inv-{studio.event.id}-panel")
    assert "is-open" not in (panel.get_attribute("class") or "")
    assert not panel.locator("text=حقوق نیروها").first.is_visible()
    page.click(f"button.btn[data-accordion-toggle='inv-{studio.event.id}']")
    panel.locator("text=حقوق نیروها").first.wait_for(state="visible")
    assert page.locator(f"button.btn[data-accordion-toggle='inv-{studio.event.id}']").get_attribute("aria-expanded") == "true"
    assert page.problems == []


def test_a_validation_error_keeps_the_popup_and_the_typed_values(page_for, studio):
    page = page_for(PHONE)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "5")
    page.click("#invoice-modal [data-add-custom='frame']")
    page.locator("#invoice-modal [data-custom-rows='frame'] input[name$='__size']").fill("abc")
    page.locator("#invoice-modal [data-custom-rows='frame'] .stepper__input").nth(0).fill("2")
    page.click("#invoice-modal .inv-actions button[type=submit]")
    page.wait_for_selector("#invoice-modal .alert--danger")
    assert page.locator("#invoice-modal.is-open").count() == 1  # still in the popup
    assert page.locator("#invoice-modal input[name$='__size']").input_value() == "abc"
    assert "فاکتور ذخیره نشد" in page.locator("#invoice-modal").inner_text()
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 0
    # fix it and save: the same popup works again
    page.locator("#invoice-modal input[name$='__size']").fill("۴۰×۶۰")
    page.click("#invoice-modal .inv-actions button[type=submit]")
    page.wait_for_selector("a:has-text('ویرایش فاکتور')")
    assert db.session.scalar(sa.select(sa.func.count(Invoice.id))) == 1


def test_an_unsent_draft_survives_a_closed_popup(page_for):
    page = page_for(DESKTOP)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "42")
    page.fill("#invoice-modal #f-pay__cash", "1234000")
    page.click("#invoice-modal [data-add-custom='frame']")
    row = page.locator("#invoice-modal [data-custom-rows='frame'] [data-custom-row]")
    row.locator("input[name$='__size']").fill("50x70")
    row.locator(".stepper__input").nth(0).fill("3")
    page.click("#invoice-modal [data-add-extra]")
    page.locator("#invoice-modal [data-extra-rows] input[name$='__label']").fill("ایاب و ذهاب")
    page.wait_for_timeout(1200)  # the draft is saved shortly after the last change
    page.keyboard.press("Escape")
    page.wait_for_selector("#invoice-modal", state="hidden")
    open_popup(page)
    banner = page.locator("#invoice-modal .inv-draft")
    banner.wait_for(state="visible")
    assert "پیش‌نویس" in banner.inner_text()
    banner.locator("text=بازیابی پیش‌نویس").click()
    wait_units(page, "۴۵")  # 42 + 3 (the restored "other size" row, weight 1)
    assert page.locator("#invoice-modal #f-qty__print__16x21").input_value() == "۴۲"
    assert page.locator("#invoice-modal #f-pay__cash").input_value() == "۱٬۲۳۴٬۰۰۰"
    assert page.locator("#invoice-modal [data-custom-rows='frame'] input[name$='__size']").input_value() == "50x70"
    assert page.locator("#invoice-modal [data-extra-rows] input[name$='__label']").input_value() == "ایاب و ذهاب"
    assert page.locator("#invoice-modal .inv-draft").count() == 0
    assert page.problems == []


def test_ignoring_a_draft_removes_it(page_for):
    page = page_for(DESKTOP)
    open_popup(page)
    page.fill("#invoice-modal #f-qty__print__16x21", "7")
    page.wait_for_timeout(1200)
    page.keyboard.press("Escape")
    page.wait_for_selector("#invoice-modal", state="hidden")
    open_popup(page)
    page.locator("#invoice-modal .inv-draft").locator("text=نادیده گرفتن").click()
    assert page.locator("#invoice-modal .inv-draft").count() == 0
    page.keyboard.press("Escape")
    page.wait_for_selector("#invoice-modal", state="hidden")
    open_popup(page)
    assert page.locator("#invoice-modal .inv-draft").count() == 0


def test_the_full_page_form_is_the_same_form(page_for, studio):
    """The no-modal route (also where the afish module links to) previews live and saves natively."""
    page = page_for(DESKTOP)
    page.goto(page.url.split("/accounting")[0] + studio.url)
    page.wait_for_selector("[data-invoice-form]")
    page.fill("#f-qty__print__16x21", "70")
    page.wait_for_function("() => document.querySelector('[data-sum=\"units\"]').textContent.trim() === '۷۰'", timeout=8000)
    assert "۱٬۷۸۰٬۰۰۰" in page.locator("[data-sum-people]").inner_text()  # 70 is inside the tier: 1,000,000 + 630,000 + 150,000
    page.click(".inv-actions button[type=submit]")
    page.wait_for_selector("a:has-text('ویرایش فاکتور')")
    invoice = db.session.scalar(sa.select(Invoice))
    db.session.refresh(invoice)
    assert invoice.units_total == 70 and {l.total for l in invoice.payroll_lines} == {1_780_000}
    assert page.problems == []


def _base(page) -> str:
    return page.url.split("/accounting")[0]


def _invoice(studio) -> None:
    """The AGENTS.md vector saved through the service (the app serves with CSRF on here, so no raw POSTs)."""
    from app.services import ledger

    data = ledger.InvoiceData(lines=[{"category": "print", "size": "16x21", "qty": 80}], payments={"cash": 20_000_000})
    ledger.save_invoice(studio.event, data, studio.manager)
    db.session.commit()


def test_payroll_selection_bar_and_the_payment_dialog(page_for, studio):
    _invoice(studio)
    page = page_for(DESKTOP)
    page.goto(_base(page) + "/accounting/payroll?y=1404&m=4")
    page.wait_for_selector("[data-pay-line]")
    bar = page.locator("[data-pay-bar]")
    assert bar.is_hidden()  # nothing selected yet
    page.locator("[data-pay-line]").nth(0).check()
    page.locator("[data-pay-line]").nth(1).check()
    bar.wait_for(state="visible")
    assert "۳٬۷۴۰٬۰۰۰" in bar.inner_text() and "۲ ردیف" in bar.inner_text()
    page.click("[data-pay-open]")
    page.wait_for_selector("#pay-modal.is-open")
    page.select_option("#f-pay_method", "card_to_card")
    page.fill("#f-note", "۱۲۳۴")
    page.click("#pay-modal button[type=submit]")
    page.locator(".toast").filter(has_text="پرداخت ۲ ردیف حقوق ثبت شد").wait_for()
    lines = studio.lines()
    assert sum(1 for l in lines if l.is_paid) == 2
    paid = next(l for l in lines if l.is_paid)
    assert paid.paid_method.value == "card_to_card" and paid.paid_note == "۱۲۳۴"
    assert page.locator("button:has-text('برگشت پرداخت')").count() == 2
    assert page.problems == []


def test_reopening_an_invoice_asks_first(page_for, studio):
    from app.services.constants import InvoiceStatus

    _invoice(studio)
    page = page_for(DESKTOP)
    page.click(f"button.btn[data-accordion-toggle='inv-{studio.event.id}']")
    page.click("summary:has-text('بازگشایی یا ابطال')")
    page.fill("input[name=reason]", "اشتباه در شرایط پرداخت")
    page.click("button:has-text('بازگشایی فاکتور')")
    page.get_by_role("button", name="بله، بازگشایی شود").click()
    page.locator(".toast").filter(has_text="فاکتور بازگشایی شد").wait_for()
    assert studio.invoice().status == InvoiceStatus.DRAFT
    assert page.problems == []
