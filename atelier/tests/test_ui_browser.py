"""Real-browser tests of the design system (Chromium via Playwright).

They verify what unit tests cannot: that the declarative widgets in core.js really work
(live money formatting with a caret-safe editor, the Jalali date picker, modals with a focus
trap, the sliding accordion, confirm dialogs, steppers), that the layout is correct at phone
and desktop width (no horizontal scroll, 44px tap targets, stacked tables, RTL), and that
dark mode, print and reduced-motion work.

The tests are skipped, with the reason, when Playwright or a Chromium binary is missing:

    pip install playwright            # the python wheel only; do NOT run `playwright install`
    CHROMIUM_BIN=/path/to/chrome      # optional; /opt/pw-browsers/chromium-*/ and
                                      # ~/.cache/ms-playwright/chromium-*/ are searched

The pages are served by a tiny stdlib HTTP server (so cookies, fetch, CSRF headers and the
service worker behave like the real thing); no Flask app and no database are needed.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed (pip install playwright)")
expect = sync_api.expect

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DOCS = ROOT / "docs"

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 900}


def _chromium_executable() -> str | None:
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


# --------------------------------------------------------------------------
# a tiny test server
# --------------------------------------------------------------------------


def _load_builder():
    spec = importlib.util.spec_from_file_location("styleguide_build", DOCS / "styleguide" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, pages: dict[str, str]):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.pages = pages
        self.posts: list[tuple[str, dict]] = []
        self.api_calls: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in self.server.pages:
            return self._send(200, self.server.pages[path].encode(), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            file = (STATIC / path[len("/static/") :]).resolve()
            if STATIC in file.parents and file.is_file():
                ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                if file.suffix == ".webmanifest":
                    ctype = "application/manifest+json"
                if file.suffix in (".js", ".css"):
                    ctype += "; charset=utf-8"
                extra = {"Service-Worker-Allowed": "/"} if file.name == "sw.js" else {}
                return self._send(200, file.read_bytes(), ctype, extra)
        if path == "/styleguide/demo.js":
            return self._send(200, (DOCS / "styleguide" / "demo.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/api/ok":
            return self._json(200, {"ok": True, "n": 3})
        if path == "/api/empty":
            return self._send(204, b"", "application/json")
        if path.startswith("/api/fail/"):
            code = int(path.rsplit("/", 1)[1])
            body = {"message": "پیام سرور"} if self.headers.get("X-Want-Message") else {}
            return self._json(code, body)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        path = urlparse(self.path).path
        if path == "/api/echo":
            call = {
                "csrf": self.headers.get("X-CSRFToken"),
                "content_type": self.headers.get("Content-Type"),
                "accept": self.headers.get("Accept"),
                "body": json.loads(raw) if raw else None,
            }
            self.server.api_calls.append(call)
            return self._json(200, call)
        self.server.posts.append((path, {"raw": raw, "csrf": self.headers.get("X-CSRFToken")}))
        self._send(200, "<html><body>posted</body></html>".encode(), "text/html; charset=utf-8")

    def _json(self, status: int, data: dict):
        self._send(status, json.dumps(data).encode(), "application/json")


def _render_pages() -> dict[str, str]:
    from flask import render_template

    sg = _load_builder()
    app = sg.make_app([DOCS / "styleguide"])
    pages = {}
    with app.test_request_context("/sg"):
        ctx = sg.demo_context()
        pages["/"] = render_template("styleguide_page.html", **ctx).replace('src="styleguide/demo.js"', 'src="/styleguide/demo.js"')
        pages["/landing"] = render_template("public/landing.html")
        pages["/404"] = render_template("errors/404.html")
        pages["/500"] = render_template("errors/500.html")
        pages["/app404"] = render_template("errors/404.html", **ctx)
    with app.test_request_context("/flash"):
        from flask import flash

        flash("مراسم ذخیره شد", "success")
        flash("پرداخت ناموفق بود", "error")
        flash("<b>x</b> نباید HTML شود", "warning")
        pages["/flash"] = render_template("errors/404.html", **ctx)
    return pages


@pytest.fixture(scope="module")
def server():
    srv = _Server(_render_pages())
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=_chromium_executable(), args=["--no-sandbox"])
        except Exception as exc:  # no browser binary
            pytest.skip(f"Chromium is not available ({str(exc).splitlines()[0]}); set CHROMIUM_BIN")
        yield b
        b.close()


@pytest.fixture()
def make_page(browser, server):
    """make_page(viewport, path='/', scheme='light', touch=False, **context_kwargs) -> Page"""
    contexts = []

    def _make(viewport=DESKTOP, path="/", scheme="light", touch=False, **kwargs):
        ctx = browser.new_context(
            viewport=viewport,
            color_scheme=scheme,
            locale="fa-IR",
            is_mobile=touch,
            has_touch=touch,
            **kwargs,
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: page.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{server.server_address[1]}{path}")
        page.wait_for_function("window.Atelier !== undefined")
        return page

    yield _make
    for c in contexts:
        c.close()


def hidden_value(page, name):
    return page.locator(f'input[type="hidden"][name="{name}"]').input_value()


# --------------------------------------------------------------------------
# money widget
# --------------------------------------------------------------------------


class TestMoneyWidget:
    def test_initial_value_is_grouped_with_persian_digits_and_raw_value_is_submitted(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="base_pay"]')
        expect(visible).to_have_value("۱٬۰۰۰٬۰۰۰")
        assert hidden_value(page, "base_pay") == "1000000"
        assert visible.get_attribute("name") is None, "only the hidden raw field carries the name"
        assert visible.get_attribute("inputmode") == "numeric"
        assert page.errors == []

    def test_typing_formats_live(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="base_pay"]')
        visible.fill("")
        visible.press_sequentially("1870000")
        expect(visible).to_have_value("۱٬۸۷۰٬۰۰۰")
        assert hidden_value(page, "base_pay") == "1870000"

    def test_accepts_persian_and_arabic_digits_and_pasted_separators(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="base_pay"]')
        visible.fill("")
        visible.press_sequentially("۱۲۳۴۵۶")
        expect(visible).to_have_value("۱۲۳٬۴۵۶")
        visible.evaluate("e => { e.value = '١٬٨٧٠٬٠٠٠ تومان'; e.dispatchEvent(new Event('input', {bubbles: true})) }")
        expect(visible).to_have_value("۱٬۸۷۰٬۰۰۰")
        assert hidden_value(page, "base_pay") == "1870000"
        visible.evaluate("e => { e.value = 'abc'; e.dispatchEvent(new Event('input', {bubbles: true})) }")
        expect(visible).to_have_value("")
        assert hidden_value(page, "base_pay") == ""

    def test_editing_in_the_middle_keeps_the_caret(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="base_pay"]')
        visible.fill("")
        visible.press_sequentially("1234567")  # ۱٬۲۳۴٬۵۶۷
        visible.evaluate("e => e.setSelectionRange(1, 1)")  # right after the first digit
        visible.press("9")
        expect(visible).to_have_value("۱۹٬۲۳۴٬۵۶۷")
        assert visible.evaluate("e => e.selectionStart") == 2, "caret stays right after the typed digit"
        visible.press("Backspace")
        expect(visible).to_have_value("۱٬۲۳۴٬۵۶۷")
        assert visible.evaluate("e => e.selectionStart") == 1

    def test_backspace_over_a_separator_deletes_the_digit_before_it(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="base_pay"]')
        visible.fill("")
        visible.press_sequentially("1234")  # ۱٬۲۳۴
        visible.evaluate("e => e.setSelectionRange(2, 2)")  # between the separator and ۲
        visible.press("Backspace")
        expect(visible).to_have_value("۱٬۲۳۴")  # separator re-inserted, caret before it
        visible.press("Backspace")
        expect(visible).to_have_value("۲۳۴")

    def test_form_submission_carries_the_raw_integer(self, make_page):
        page = make_page()
        data = page.evaluate(
            """() => { const f = document.querySelector('[data-name="base_pay"]').form;
                       return Object.fromEntries(new FormData(f)); }"""
        )
        assert data["base_pay"] == "1000000" and data["commission"] == "9000"

    def test_programmatic_api(self, make_page):
        page = make_page()
        assert page.evaluate("Atelier.getValue('base_pay')") == 1000000
        page.evaluate("Atelier.setValue('base_pay', 1870000)")
        expect(page.locator('[data-name="base_pay"]')).to_have_value("۱٬۸۷۰٬۰۰۰")
        assert page.evaluate("Atelier.getValue('base_pay')") == 1870000

    def test_input_event_reports_the_integer(self, make_page):
        page = make_page()
        page.evaluate(
            "window.__seen = []; document.addEventListener('atelier:input', e => window.__seen.push([e.detail.name, e.detail.value]))"
        )
        page.locator('[data-name="commission"]').fill("")
        page.locator('[data-name="commission"]').press_sequentially("12")
        assert page.evaluate("window.__seen")[-1] == ["commission", 12]

    def test_max_bound_blocks_the_form_natively(self, make_page):
        page = make_page()
        page.evaluate("document.querySelector('[data-name=\"commission\"]').setAttribute('data-max', '5000')")
        page.locator('[data-name="commission"]').fill("")
        page.locator('[data-name="commission"]').press_sequentially("9000")
        assert "بیشتر" in page.locator('[data-name="commission"]').evaluate("e => e.validationMessage")

    def test_cloned_initialised_widgets_keep_a_single_correctly_named_hidden_field(self, make_page):
        page = make_page()
        names = page.evaluate(
            """() => {
              const src = document.querySelector('[data-name="commission"]');
              const copy = src.parentNode.cloneNode(true);
              src.closest('form').appendChild(copy);
              Atelier.init(copy);
              return [...src.closest('form').querySelectorAll('input[type=hidden][name="commission"]')].length;
            }"""
        )
        assert names == 2, "the original and the clone each have exactly one hidden field"

    def test_widgets_added_later_are_initialised_by_init(self, make_page):
        page = make_page()
        page.evaluate(
            """() => { const d = document.createElement('div');
                       d.innerHTML = '<input id="late" name="late" data-money value="2500">';
                       document.body.appendChild(d); Atelier.init(d); }"""
        )
        expect(page.locator("#late")).to_have_value("۲٬۵۰۰")
        assert hidden_value(page, "late") == "2500"


# --------------------------------------------------------------------------
# Jalali date picker
# --------------------------------------------------------------------------


class TestJalaliDatePicker:
    def test_iso_value_is_shown_as_jalali_and_submitted_as_iso(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        expect(visible).to_have_value("۱۴۰۴/۰۴/۲۶")
        assert hidden_value(page, "event_date") == "2025-07-17"
        assert visible.get_attribute("name") is None

    def test_picker_opens_with_saturday_first_grid_and_picks_a_day(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        visible.click()
        pop = page.locator(".jd-pop")
        expect(pop).to_be_visible()
        assert pop.locator(".jd-pop__dow span").all_inner_texts() == ["ش", "ی", "د", "س", "چ", "پ", "ج"]
        assert page.locator(".jd-day").count() == 31  # Tir has 31 days
        page.locator('.jd-day[data-d="5"]').click()
        expect(pop).to_have_count(0)
        expect(visible).to_have_value("۱۴۰۴/۰۴/۰۵")
        assert hidden_value(page, "event_date") == "2025-06-26"

    def test_first_of_farvardin_1404_is_a_friday_in_the_last_column(self, make_page):
        page = make_page()
        page.evaluate("Atelier.setValue('event_date', '2025-03-21')")
        page.locator('[data-name="event_date"]').click()
        cells = page.locator(".jd-pop__grid > *")
        # 6 blank cells (Saturday..Thursday) then day 1 on Friday
        assert cells.nth(6).inner_text() == "۱"
        assert "is-weekend" in cells.nth(6).get_attribute("class")
        assert page.locator(".jd-pop select").nth(1).input_value() == "1404"

    def test_month_navigation_and_selects(self, make_page):
        page = make_page()
        page.locator('[data-name="event_date"]').click()
        page.locator(".jd-pop__nav").nth(1).click()  # next month
        assert page.locator(".jd-pop select").first.input_value() == "5"
        assert page.locator(".jd-day").count() == 31
        page.locator(".jd-pop select").first.select_option("12")  # Esfand 1404 (not a leap year)
        assert page.locator(".jd-day").count() == 29
        page.locator(".jd-pop select").nth(1).select_option("1403")  # leap year
        assert page.locator(".jd-day").count() == 30

    def test_typing_a_jalali_date_updates_the_iso_value(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        visible.fill("")
        visible.press_sequentially("1403/12/30")  # 1403 is a leap year
        assert hidden_value(page, "event_date") == "2025-03-20"
        visible.press("Tab")
        expect(visible).to_have_value("۱۴۰۳/۱۲/۳۰")
        page.locator('[data-name="event_date"]').fill("")
        page.locator('[data-name="event_date"]').press_sequentially("۱۴۰۴/۱/۵")
        page.keyboard.press("Tab")
        expect(page.locator('[data-name="event_date"]')).to_have_value("۱۴۰۴/۰۱/۰۵")
        assert hidden_value(page, "event_date") == "2025-03-25"

    def test_invalid_date_is_rejected_by_the_browser(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        visible.fill("")
        visible.press_sequentially("1404/12/30")  # 1404 is not a leap year
        assert hidden_value(page, "event_date") == ""
        assert "نامعتبر" in visible.evaluate("e => e.validationMessage")
        visible.press("Tab")
        assert visible.get_attribute("aria-invalid") == "true"

    def test_min_max_are_enforced_and_disable_days(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="birth_date"]')  # data-max = 2008-01-01
        visible.click()
        assert page.locator(".jd-day:not(:disabled)").count() > 0
        page.keyboard.press("Escape")
        visible.fill("")
        visible.press_sequentially("1404/01/01")
        assert "بعد از" in visible.evaluate("e => e.validationMessage")

    def test_keyboard_navigation_inside_the_grid(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        visible.focus()
        visible.press("ArrowDown")  # opens and focuses the selected day (26 Tir)
        focused = page.evaluate("document.activeElement.getAttribute('data-d')")
        assert focused == "26"
        page.keyboard.press("ArrowLeft")  # RTL: left is the next day
        assert page.evaluate("document.activeElement.getAttribute('data-d')") == "27"
        page.keyboard.press("ArrowDown")  # next week
        assert page.evaluate("document.activeElement.getAttribute('data-d')") == "3"  # wrapped into Mordad
        page.keyboard.press("Enter")
        expect(visible).to_have_value("۱۴۰۴/۰۵/۰۳")
        assert hidden_value(page, "event_date") == "2025-07-25"

    def test_escape_closes_and_returns_focus(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="event_date"]')
        visible.click()
        expect(page.locator(".jd-pop")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator(".jd-pop")).to_have_count(0)
        assert page.evaluate("document.activeElement.getAttribute('data-name')") == "event_date"

    def test_today_and_clear_buttons(self, make_page):
        page = make_page()
        visible = page.locator('[data-name="birth_date"]')
        visible.click()
        page.get_by_role("button", name="پاک کردن").click()
        expect(visible).to_have_value("")
        assert hidden_value(page, "birth_date") == ""
        page.locator('[data-name="event_date"]').click()
        page.get_by_role("button", name="امروز").click()
        iso = hidden_value(page, "event_date")
        assert len(iso) == 10 and iso.startswith("20")

    def test_picker_is_a_bottom_sheet_on_phones(self, make_page):
        page = make_page(PHONE, touch=True)
        page.locator('[data-name="event_date"]').tap()
        pop = page.locator(".jd-pop")
        expect(pop).to_be_visible()
        page.wait_for_timeout(400)  # let the slide-up animation finish
        box = pop.bounding_box()
        assert box["x"] == 0 and box["width"] == PHONE["width"]
        assert abs(box["y"] + box["height"] - PHONE["height"]) <= 1, "sheet is docked to the bottom"
        assert page.locator(".jd-scrim").count() == 1
        for day in page.locator(".jd-day").all()[:5]:
            assert day.bounding_box()["height"] >= 44
        page.locator(".jd-scrim").tap(position={"x": 20, "y": 20})
        expect(pop).to_have_count(0)

    def test_outside_click_closes_the_popover(self, make_page):
        page = make_page()
        page.locator('[data-name="event_date"]').click()
        expect(page.locator(".jd-pop")).to_be_visible()
        page.mouse.click(5, 300)
        expect(page.locator(".jd-pop")).to_have_count(0)

    def test_form_submission_carries_iso_dates(self, make_page):
        page = make_page()
        data = page.evaluate("() => Object.fromEntries(new FormData(document.querySelector('[data-name=\"event_date\"]').form))")
        assert data["event_date"] == "2025-07-17" and data["birth_date"] == ""


# --------------------------------------------------------------------------
# stepper, accordion, tabs, confirm
# --------------------------------------------------------------------------


class TestStepper:
    def test_bounds_and_buttons(self, make_page):
        page = make_page()
        field = page.locator('[data-name="weight"]')
        dec = page.locator('.stepper:has([data-name="weight"]) [data-stepper-dec]')
        inc = page.locator('.stepper:has([data-name="weight"]) [data-stepper-inc]')
        expect(field).to_have_value("۱")
        expect(dec).to_be_disabled()
        inc.click()
        inc.click()
        expect(field).to_have_value("۳")
        assert hidden_value(page, "weight") == "3"
        dec.click()
        dec.click()
        expect(field).to_have_value("۱")
        expect(dec).to_be_disabled()
        for _ in range(25):
            if inc.is_enabled():
                inc.click()
        expect(field).to_have_value("۲۰")
        expect(inc).to_be_disabled()

    def test_typing_clamps_and_blank_falls_back_to_default(self, make_page):
        page = make_page()
        field = page.locator('[data-name="weight"]')
        field.fill("")
        field.press_sequentially("99")
        expect(field).to_have_value("۲۰")
        field.fill("")
        field.press("Tab")
        expect(field).to_have_value("۱")
        field.focus()
        field.press("ArrowUp")
        expect(field).to_have_value("۲")

    def test_bare_input_is_wrapped(self, make_page):
        page = make_page()
        page.evaluate(
            """() => { const d = document.createElement('div');
                       d.innerHTML = '<input type="number" id="bare" name="bare" value="3" min="0" max="5" data-stepper>';
                       document.body.appendChild(d); Atelier.init(d); }"""
        )
        expect(page.locator("#bare")).to_have_value("۳")
        assert page.locator(".stepper:has(#bare) button").count() == 2
        assert hidden_value(page, "bare") == "3"


class TestAccordion:
    def test_closed_by_default_and_slides_open(self, make_page):
        page = make_page()
        panel = page.locator("#sg-invoice-panel")
        expect(panel).to_be_hidden()
        assert page.locator("#sg-invoice-btn").get_attribute("aria-expanded") == "false"
        page.locator("#sg-invoice-btn").click()
        expect(panel).to_be_visible()
        assert page.locator("#sg-invoice-btn").get_attribute("aria-expanded") == "true"
        assert panel.evaluate("e => getComputedStyle(e).gridTemplateRows") != "0px"
        page.locator("#sg-invoice-btn").click()
        expect(panel).to_be_hidden()

    def test_external_button_toggles_it_and_keeps_aria_in_sync(self, make_page):
        page = make_page()
        external = page.locator('button[data-accordion-toggle="sg-invoice"][aria-controls]').first
        external.click()
        expect(page.locator("#sg-invoice-panel")).to_be_visible()
        assert external.get_attribute("aria-expanded") == "true"
        assert page.locator("#sg-invoice-btn").get_attribute("aria-expanded") == "true"

    def test_open_attribute_is_respected(self, make_page):
        page = make_page()
        expect(page.locator("#sg-acc2-panel")).to_be_visible()

    def test_closed_content_is_not_focusable(self, make_page):
        page = make_page()
        page.evaluate(
            "document.querySelector('#sg-invoice-panel .accordion__body').insertAdjacentHTML('beforeend', '<button id=\"inner\">x</button>')"
        )
        page.locator("#inner").focus()
        assert page.evaluate("document.activeElement.id") != "inner", "hidden panel content must not take focus"


class TestTabs:
    def test_client_tabs_switch_panels_and_support_arrow_keys(self, make_page):
        page = make_page()
        tab_a = page.locator("#sg-tabs-tab-a")
        tab_b = page.locator("#sg-tabs-tab-b")
        expect(page.locator("#sg-tabs-panel-a")).to_be_visible()
        expect(page.locator("#sg-tabs-panel-b")).to_be_hidden()
        tab_b.click()
        expect(page.locator("#sg-tabs-panel-b")).to_be_visible()
        expect(page.locator("#sg-tabs-panel-a")).to_be_hidden()
        assert tab_b.get_attribute("aria-selected") == "true" and tab_a.get_attribute("aria-selected") == "false"
        tab_b.focus()
        page.keyboard.press("ArrowRight")  # RTL: right is the previous tab
        assert page.evaluate("document.activeElement.id") == "sg-tabs-tab-a"
        page.keyboard.press("End")
        assert page.evaluate("document.activeElement.id") == "sg-tabs-tab-c"
        expect(page.locator("#sg-tabs-panel-c")).to_be_visible()


class TestConfirm:
    def _open(self, page):
        page.locator("button[data-confirm]").first.scroll_into_view_if_needed()
        page.locator("button[data-confirm]").first.click()
        dialog = page.locator("#atelier-confirm")
        expect(dialog).to_be_visible()
        return dialog

    def test_cancel_does_not_post(self, make_page, server):
        page = make_page()
        before = len(server.posts)
        dialog = self._open(page)
        assert "حذف مراسم" in page.locator("#atelier-confirm-title").inner_text()
        assert page.evaluate("document.activeElement.hasAttribute('data-confirm-cancel')"), "the safe choice is focused first"
        dialog.locator("[data-confirm-cancel]").click()
        expect(dialog).to_be_hidden()
        page.wait_for_timeout(100)
        assert len(server.posts) == before

    def test_escape_cancels(self, make_page, server):
        page = make_page()
        before = len(server.posts)
        dialog = self._open(page)
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        assert len(server.posts) == before

    def test_confirm_posts_the_form_with_csrf_token(self, make_page, server):
        page = make_page()
        before = len(server.posts)
        dialog = self._open(page)
        with page.expect_navigation():
            dialog.locator("[data-confirm-ok]").click()
        assert len(server.posts) == before + 1
        path, info = server.posts[-1]
        assert path == "/sg"
        assert "csrf_token=styleguide-csrf-token" in info["raw"]

    def test_programmatic_confirm_resolves_a_promise(self, make_page):
        page = make_page()
        page.evaluate("() => { window.__r = null; Atelier.confirm('مطمئنید؟', {confirmLabel: 'بله'}).then(v => { window.__r = v; }); }")
        page.locator("#atelier-confirm [data-confirm-ok]").click()
        page.wait_for_function("window.__r !== null")
        assert page.evaluate("window.__r") is True
        page.evaluate("() => { window.__r = null; Atelier.confirm('x').then(v => { window.__r = v; }); }")
        page.locator("#atelier-confirm [data-confirm-cancel]").click()
        page.wait_for_function("window.__r !== null")
        assert page.evaluate("window.__r") is False


class TestDoubleSubmitLock:
    def test_second_submit_is_blocked_and_button_shows_loading(self, make_page, server):
        page = make_page()
        page.evaluate(
            """() => { document.body.insertAdjacentHTML('beforeend',
                   '<form id="lockme" method="post" action="/lock"><button id="lockbtn" class="btn" type="submit">ارسال</button></form>'); }"""
        )
        page.locator("#lockbtn").click()
        page.wait_for_url("**/lock")
        assert [p for p, _ in server.posts].count("/lock") == 1


# --------------------------------------------------------------------------
# modal
# --------------------------------------------------------------------------


class TestModal:
    def test_open_close_focus_trap_and_scroll_lock(self, make_page):
        page = make_page()
        opener = page.locator('[data-modal-open="invoice-modal"]')
        opener.scroll_into_view_if_needed()
        opener.click()
        modal = page.locator("#invoice-modal")
        expect(modal).to_be_visible()
        assert page.evaluate("document.body.classList.contains('has-modal')")
        assert page.evaluate("document.querySelector('#invoice-modal').contains(document.activeElement)")
        for _ in range(70):
            page.keyboard.press("Tab")
            assert page.evaluate("document.querySelector('#invoice-modal').contains(document.activeElement)"), "focus escaped the modal"
        for _ in range(10):
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("document.querySelector('#invoice-modal').contains(document.activeElement)")
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
        assert not page.evaluate("document.body.classList.contains('has-modal')")
        assert page.evaluate("document.activeElement.getAttribute('data-modal-open')") == "invoice-modal", "focus returns to the opener"

    def test_backdrop_click_and_close_button(self, make_page):
        page = make_page()
        page.locator('[data-modal-open="small-modal"]').click()
        modal = page.locator("#small-modal")
        expect(modal).to_be_visible()
        page.mouse.click(8, 8)  # on the backdrop, outside the dialog
        expect(modal).to_be_hidden()
        page.locator('[data-modal-open="small-modal"]').click()
        page.locator("#small-modal .modal__header [data-modal-close]").click()
        expect(modal).to_be_hidden()

    def test_clicking_inside_the_dialog_does_not_close_it(self, make_page):
        page = make_page()
        page.locator('[data-modal-open="small-modal"]').click()
        page.locator("#small-modal .modal__body").click()
        expect(page.locator("#small-modal")).to_be_visible()

    def test_phone_gets_a_full_screen_sheet_with_sticky_footer(self, make_page):
        page = make_page(PHONE, touch=True)
        page.locator('[data-modal-open="invoice-modal"]').scroll_into_view_if_needed()
        page.locator('[data-modal-open="invoice-modal"]').tap()
        dialog = page.locator("#invoice-modal .modal__dialog")
        expect(dialog).to_be_visible()
        page.wait_for_timeout(350)
        box = dialog.bounding_box()
        assert (box["x"], box["y"], box["width"], box["height"]) == (0, 0, PHONE["width"], PHONE["height"])
        footer = page.locator("#invoice-modal .modal__footer").bounding_box()
        assert footer["y"] + footer["height"] <= PHONE["height"] + 1, "footer buttons stay on screen"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    def test_desktop_dialog_is_centered_and_limited_in_height(self, make_page):
        page = make_page()
        page.locator('[data-modal-open="invoice-modal"]').click()
        page.wait_for_timeout(350)
        box = page.locator("#invoice-modal .modal__dialog").bounding_box()
        assert box["width"] <= 820 + 1 and box["height"] <= DESKTOP["height"] * 0.9 + 1
        assert abs((box["x"] + box["width"] / 2) - DESKTOP["width"] / 2) <= 1

    def test_stacked_modals_close_one_at_a_time(self, make_page):
        page = make_page()
        page.locator('[data-modal-open="small-modal"]').click()
        page.evaluate("() => { Atelier.confirm('x'); }")
        expect(page.locator("#atelier-confirm")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#atelier-confirm")).to_be_hidden()
        expect(page.locator("#small-modal")).to_be_visible()
        assert page.evaluate("document.body.classList.contains('has-modal')")
        page.keyboard.press("Escape")
        expect(page.locator("#small-modal")).to_be_hidden()
        assert not page.evaluate("document.body.classList.contains('has-modal')")

    def test_modal_events_and_api(self, make_page):
        page = make_page()
        page.evaluate(
            "window.__ev = []; ['open','close'].forEach(n => document.addEventListener('atelier:modal-' + n, e => window.__ev.push(n + ':' + e.detail.id)));"
            "Atelier.openModal('small-modal')"
        )
        expect(page.locator("#small-modal")).to_be_visible()
        page.evaluate("Atelier.closeModal('small-modal')")
        expect(page.locator("#small-modal")).to_be_hidden()
        assert page.evaluate("window.__ev") == ["open:small-modal", "close:small-modal"]


# --------------------------------------------------------------------------
# toasts, fetchJSON, theme
# --------------------------------------------------------------------------


class TestToastsAndFetch:
    def test_toast_roles_and_limit(self, make_page):
        page = make_page()
        page.locator("#sg-toast-success").click()
        page.locator("#sg-toast-error").click()
        assert page.locator(".toast--success").get_attribute("role") == "status"
        assert page.locator(".toast--error").get_attribute("role") == "alert"
        for i in range(6):
            page.evaluate(f"Atelier.toast('m{i}', 'info', {{timeout: 0}})")
        assert page.locator(".toast").count() <= 4

    def test_toast_close_button_and_text_is_not_html(self, make_page):
        page = make_page()
        page.evaluate("Atelier.toast('<b>x</b>', 'info', {timeout: 0})")
        toast = page.locator(".toast").last
        assert toast.locator(".toast__text").inner_text() == "<b>x</b>"
        assert toast.locator("b").count() == 0
        toast.get_by_role("button").click()
        expect(page.locator(".toast")).to_have_count(0)

    def test_fetch_json_sends_csrf_and_json_and_parses_the_reply(self, make_page, server):
        page = make_page()
        reply = page.evaluate("Atelier.fetchJSON('/api/echo', {method: 'POST', json: {a: 1, fa: 'سلام'}})")
        assert reply["csrf"] == "styleguide-csrf-token"
        assert reply["content_type"].startswith("application/json") and reply["accept"] == "application/json"
        assert reply["body"] == {"a": 1, "fa": "سلام"}
        reply = page.evaluate("Atelier.fetchJSON('/api/echo', {body: {plain: true}})")  # plain object body -> JSON POST
        assert reply["body"] == {"plain": True}

    def test_fetch_json_get_and_empty_responses(self, make_page):
        page = make_page()
        assert page.evaluate("Atelier.fetchJSON('/api/ok')") == {"ok": True, "n": 3}
        assert page.evaluate("Atelier.fetchJSON('/api/empty')") is None

    def test_fetch_json_errors_have_persian_messages(self, make_page):
        page = make_page()
        expected = {403: "دسترسی", 404: "پیدا نشد", 422: "معتبر نیست", 429: "تعداد درخواست", 500: "سرور", 401: "منقضی"}
        for status, word in expected.items():
            result = page.evaluate(
                f"Atelier.fetchJSON('/api/fail/{status}').then(() => null, e => [e.name, e.status, e.message])"
            )
            assert result[0] == "AtelierError" and result[1] == status and word in result[2], (status, result)

    def test_fetch_json_prefers_the_server_message(self, make_page):
        page = make_page()
        result = page.evaluate(
            "Atelier.fetchJSON('/api/fail/400', {headers: {'X-Want-Message': '1'}}).then(() => null, e => [e.message, e.data])"
        )
        assert result[0] == "پیام سرور" and result[1] == {"message": "پیام سرور"}

    def test_fetch_json_network_failure(self, make_page):
        page = make_page()
        result = page.evaluate("Atelier.fetchJSON('http://127.0.0.1:1/x', {timeout: 3000}).then(() => null, e => [e.status, e.message])")
        assert result[0] == 0 and "اتصال" in result[1]

    def test_csrf_token_is_not_sent_to_other_origins(self, make_page):
        page = make_page()
        seen = {}

        def handler(route, request):
            seen.update(request.headers)
            route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Content-Type": "application/json"}, body="{}")

        page.route("http://other.test/**", handler)
        page.evaluate("Atelier.fetchJSON('http://other.test/x', {method: 'POST', json: {}})")
        assert "x-csrftoken" not in seen


class TestThemeAndShell:
    def test_theme_toggle_persists_and_applies_before_paint(self, make_page, server):
        page = make_page()
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") is None
        page.locator("[data-theme-toggle]").first.click()
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert dark_bg == "rgb(21, 18, 15)"
        page.reload()
        # theme-init.js runs synchronously in <head>: the attribute is there at DOMContentLoaded
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
        page.locator("[data-theme-toggle]").first.click()
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(247, 243, 236)"

    def test_follows_the_system_dark_scheme(self, make_page):
        page = make_page(scheme="dark")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(21, 18, 15)"
        assert page.evaluate("Atelier.theme.effective()") == "dark"
        page.evaluate("Atelier.theme.set('light')")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(247, 243, 236)"
        page.evaluate("Atelier.theme.set('auto')")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(21, 18, 15)"

    def test_document_is_rtl_with_sidebar_on_the_right(self, make_page):
        page = make_page()
        assert page.evaluate("[document.documentElement.dir, document.documentElement.lang]") == ["rtl", "fa"]
        box = page.locator(".sidebar").bounding_box()
        assert abs(box["x"] + box["width"] - DESKTOP["width"]) <= 1, "sidebar sits at the right edge"
        assert page.evaluate("getComputedStyle(document.body).fontFamily").lower().startswith('vazirmatn')

    def test_print_and_history_back_hooks(self, make_page):
        page = make_page()
        page.evaluate("() => { window.__printed = 0; window.print = () => { window.__printed += 1; }; }")
        page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button id=\"pb\" data-print>p</button>')")
        page.locator("#pb").click()
        assert page.evaluate("window.__printed") == 1

    def test_ids_are_unique_and_aria_references_resolve(self, make_page):
        for path in ("/", "/landing", "/app404"):
            page = make_page(path=path)
            problems = page.evaluate(
                """() => {
                  const out = [];
                  const seen = {};
                  for (const el of document.querySelectorAll('[id]')) { if (seen[el.id]) out.push('duplicate id ' + el.id); seen[el.id] = 1; }
                  for (const attr of ['aria-controls', 'aria-labelledby', 'aria-describedby']) {
                    for (const el of document.querySelectorAll('[' + attr + ']')) {
                      for (const id of el.getAttribute(attr).split(/\\s+/).filter(Boolean)) {
                        if (!document.getElementById(id)) out.push(attr + ' -> missing #' + id);
                      }
                    }
                  }
                  for (const label of document.querySelectorAll('label[for]')) {
                    if (!document.getElementById(label.htmlFor)) out.push('label for missing #' + label.htmlFor);
                  }
                  return out;
                }"""
            )
            assert problems == [], (path, problems)

    def test_sidebar_collapse_is_remembered(self, make_page):
        page = make_page()
        full = page.locator(".sidebar").bounding_box()["width"]
        page.locator("[data-sidebar-toggle]").click()
        page.wait_for_timeout(350)
        collapsed = page.locator(".sidebar").bounding_box()["width"]
        assert collapsed < full / 2
        page.reload()
        assert page.evaluate("document.documentElement.classList.contains('sb-collapsed')")

    def test_phone_drawer_opens_with_the_menu_tab_and_closes_with_escape(self, make_page):
        page = make_page(PHONE, touch=True)
        sidebar = page.locator("#sidebar")
        expect(sidebar).to_be_hidden()
        assert page.locator(".tabbar").is_visible()
        page.locator("[data-drawer-open]").tap()
        expect(sidebar).to_be_visible()
        assert page.locator("[data-drawer-open]").get_attribute("aria-expanded") == "true"
        page.wait_for_function("document.querySelector('#sidebar').contains(document.activeElement)")
        page.keyboard.press("Escape")
        expect(sidebar).to_be_hidden()
        assert page.evaluate("document.activeElement.hasAttribute('data-drawer-open')")

    def test_phone_drawer_closes_on_scrim_tap(self, make_page):
        page = make_page(PHONE, touch=True)
        page.locator("[data-drawer-open]").tap()
        expect(page.locator("#sidebar")).to_be_visible()
        page.locator(".scrim").tap(position={"x": 20, "y": 400})
        expect(page.locator("#sidebar")).to_be_hidden()

    def test_flashed_messages_become_toasts_and_the_source_list_is_removed(self, make_page):
        page = make_page(path="/flash")
        expect(page.locator(".toast")).to_have_count(3)
        assert page.locator(".toast--success .toast__text").inner_text() == "مراسم ذخیره شد"
        assert page.locator(".toast--error").get_attribute("role") == "alert"
        assert page.locator(".toast--warning .toast__text").inner_text() == "<b>x</b> نباید HTML شود"
        assert page.locator(".toast b").count() == 0, "flash text is never interpreted as HTML"
        assert page.locator("#flash-source").count() == 0
        assert page.errors == []

    def test_service_worker_registers_and_only_caches_static_assets(self, make_page, server):
        page = make_page()
        info = """async () => { const r = await navigator.serviceWorker.getRegistration();
          return r ? {installing: r.installing && r.installing.state, waiting: r.waiting && r.waiting.state,
                      active: r.active && r.active.state, controller: !!navigator.serviceWorker.controller, scope: r.scope} : null; }"""

        def wait_for(predicate_js, what):
            try:
                page.wait_for_function(predicate_js, timeout=15000)
            except sync_api.TimeoutError:
                pytest.fail(f"service worker never {what}: {page.evaluate(info)} errors={page.errors}")

        wait_for("navigator.serviceWorker.getRegistration().then(r => !!(r && r.active && r.active.state === 'activated'))", "activated")
        scope = page.evaluate("navigator.serviceWorker.getRegistration().then(r => r.scope)")
        assert scope.endswith("/")
        # Chromium persists a fresh registration a moment after "activated"; a navigation inside that window
        # is not controlled yet, so allow a few reloads (a browser timing detail, not application behaviour).
        for _ in range(4):
            page.reload()
            if page.evaluate("navigator.serviceWorker.controller !== null"):
                break
            page.wait_for_timeout(500)
        wait_for("navigator.serviceWorker.controller !== null", "took control after reloads")
        page.evaluate("fetch('/static/css/app.css').then(r => r.text())")
        page.wait_for_timeout(300)
        cached = page.evaluate(
            "caches.keys().then(async ks => { const out = []; for (const k of ks) { const c = await caches.open(k); out.push(...(await c.keys()).map(r => new URL(r.url).pathname)); } return out; })"
        )
        assert "/static/css/app.css" in cached
        assert all(p.startswith("/static/") for p in cached), f"only static files may be cached: {cached}"
        assert "/" not in cached and "/api/ok" not in cached


# --------------------------------------------------------------------------
# responsive layout, accessibility, print
# --------------------------------------------------------------------------


class TestResponsive:
    @pytest.mark.parametrize("path", ["/", "/landing", "/404", "/500", "/app404"])
    @pytest.mark.parametrize("viewport,touch", [(PHONE, True), (DESKTOP, False)])
    def test_no_horizontal_scroll(self, make_page, path, viewport, touch):
        page = make_page(viewport, path=path, touch=touch)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            path,
            page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]"),
        )
        assert page.errors == []

    @pytest.mark.parametrize("path", ["/", "/landing"])
    def test_no_horizontal_scroll_in_dark_mode(self, make_page, path):
        page = make_page(PHONE, path=path, touch=True, scheme="dark")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    def test_table_becomes_cards_on_phones_and_a_table_on_desktop(self, make_page):
        phone = make_page(PHONE, touch=True)
        thead = phone.locator(".table--stack thead")
        assert thead.bounding_box()["width"] <= 1, "header row is visually hidden on phones"
        row = phone.locator(".table--stack tbody tr").first
        assert row.evaluate("e => getComputedStyle(e).display") == "block"
        label = phone.locator(".table--stack tbody tr").first.locator("td").first.evaluate("e => getComputedStyle(e, '::before').content")
        assert "مراسم" in label
        desktop = make_page(DESKTOP)
        assert desktop.locator(".table--stack thead").bounding_box()["width"] > 100
        assert desktop.locator(".table--stack tbody tr").first.evaluate("e => getComputedStyle(e).display") == "table-row"

    def test_calendar_shows_chips_on_desktop_and_agenda_on_phones(self, make_page):
        desktop = make_page(DESKTOP)
        assert desktop.locator(".cal__events").first.is_visible() and not desktop.locator(".cal__agenda").is_visible()
        phone = make_page(PHONE, touch=True)
        assert phone.locator(".cal__agenda").is_visible() and not phone.locator(".cal__events").first.is_visible()
        assert phone.locator(".cal__dots").first.is_visible()

    def test_calendar_columns_run_saturday_to_friday_right_to_left(self, make_page):
        page = make_page(DESKTOP)
        boxes = [h.bounding_box() for h in page.locator(".cal__dow").all()]
        names = page.locator(".cal__dow .hide-sm").all_inner_texts()
        assert names[0] == "شنبه" and names[-1] == "جمعه"
        xs = [b["x"] for b in boxes]
        assert xs == sorted(xs, reverse=True), "Saturday is the rightmost column"

    def test_pagination_is_compact_on_phones(self, make_page):
        phone = make_page(PHONE, touch=True)
        box = phone.locator(".pagination").bounding_box()
        assert box["height"] < 70, "no wrapping onto several rows"
        assert phone.locator(".pagination__num:visible").count() == 1  # only the current page


class TestAccessibility:
    def test_tap_targets_are_at_least_44px_on_phones(self, make_page):
        page = make_page(PHONE, touch=True)
        small = page.evaluate(
            """() => {
              const out = [];
              window.__measured = 0;
              const sel = 'button, a.btn, a.icon-btn, a.nav__link, a.tabbar__item, a.tabs__tab, a.pagination__item, input:not([type=hidden]):not([type=radio]):not([type=checkbox]), select, textarea, .check, .choice span, label.check, [role=tab], summary';
              for (const el of document.querySelectorAll(sel)) {
                if (el.closest('.modal[hidden], .sprite, .visually-hidden, .skip-link, .sidebar')) continue;
                const cs = getComputedStyle(el);
                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                window.__measured++;
                if (r.height < 43.5 || (r.width < 43.5 && !el.matches('input, select, textarea, .check'))) {
                  out.push((el.className || el.tagName) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
                }
              }
              return out;
            }"""
        )
        assert page.evaluate("window.__measured") > 40, "the audit must actually measure the page's controls"
        assert small == [], f"interactive elements below the 44px tap target: {small}"

    def test_the_tap_target_audit_can_fail(self, make_page):
        page = make_page(PHONE, touch=True)
        page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button class=\"tiny\" style=\"height:30px\">x</button>')")
        height = page.locator("button.tiny").bounding_box()["height"]
        assert height < 44  # a 30px control is indeed detectable by the same measurement

    def test_drawer_nav_links_are_tap_sized(self, make_page):
        page = make_page(PHONE, touch=True)
        page.locator("[data-drawer-open]").tap()
        page.wait_for_timeout(350)
        for link in page.locator(".sidebar .nav__link").all():
            assert link.bounding_box()["height"] >= 44

    def test_focus_ring_is_visible_on_keyboard_focus(self, make_page):
        page = make_page()
        page.keyboard.press("Tab")  # skip link
        assert page.evaluate("document.activeElement.classList.contains('skip-link')")
        assert page.evaluate("getComputedStyle(document.activeElement).transform") == "none", "skip link slides into view"
        page.keyboard.press("Tab")
        outline = page.evaluate("(() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, parseFloat(s.outlineWidth)]; })()")
        assert outline[0] == "solid" and outline[1] >= 2

    def test_inputs_use_16px_font_so_ios_does_not_zoom(self, make_page):
        page = make_page(PHONE, touch=True)
        sizes = page.evaluate("[...document.querySelectorAll('.input, .select, .textarea')].map(e => parseFloat(getComputedStyle(e).fontSize))")
        assert sizes and min(sizes) >= 16

    def test_landmarks_and_labels(self, make_page):
        page = make_page()
        assert page.locator("main#main").count() == 1 and page.locator("nav[aria-label]").count() >= 2
        assert page.locator("a.skip-link[href='#main']").count() == 1
        unlabeled = page.evaluate(
            """() => [...document.querySelectorAll('input:not([type=hidden]), select, textarea')]
               .filter(e => !e.closest('.modal[hidden]'))
               .filter(e => !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'))
               .map(e => e.outerHTML.slice(0, 80))"""
        )
        assert unlabeled == [], f"form controls without an accessible name: {unlabeled}"
        icon_only = page.evaluate(
            """() => [...document.querySelectorAll('button, a')]
               .filter(e => !e.closest('.modal[hidden], .sprite'))
               .filter(e => !e.textContent.trim() && !e.getAttribute('aria-label') && !e.getAttribute('title'))
               .map(e => e.outerHTML.slice(0, 80))"""
        )
        assert icon_only == [], f"icon-only controls without a name: {icon_only}"

    def test_reduced_motion_disables_transitions(self, make_page):
        page = make_page(reduced_motion="reduce")
        duration = page.evaluate("getComputedStyle(document.querySelector('.collapse')).transitionDuration")
        assert duration.startswith("1e-05") or duration in ("0s", "0.00001s"), duration
        page.locator("#sg-invoice-btn").click()
        expect(page.locator("#sg-invoice-panel")).to_be_visible()

    def test_font_fallback_stack_is_defined(self, make_page):
        page = make_page()
        stack = page.evaluate("getComputedStyle(document.body).fontFamily")
        assert "Vazirmatn" in stack and "Tahoma" in stack

    def test_vendored_font_actually_loads(self, make_page):
        page = make_page()
        page.wait_for_function("document.fonts.check('16px Vazirmatn')")
        assert page.evaluate("[...document.fonts].some(f => f.family.includes('Vazirmatn') && f.status === 'loaded')")


class TestPrint:
    def test_print_hides_chrome_and_uses_black_on_white(self, make_page):
        page = make_page(DESKTOP)
        page.emulate_media(media="print")
        for selector in (".sidebar", ".topbar", ".tabbar", ".toasts", ".btn", ".pagination"):
            assert page.locator(selector).first.evaluate("e => getComputedStyle(e).display") == "none", selector
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
        assert page.evaluate("getComputedStyle(document.body).color") == "rgb(0, 0, 0)"
        assert page.locator(".sheet").first.is_visible()

    def test_print_ignores_dark_mode(self, make_page):
        page = make_page(DESKTOP, scheme="dark")
        page.emulate_media(media="print", color_scheme="dark")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"

    def test_print_pdf_is_a4(self, make_page):
        page = make_page(DESKTOP)
        pdf = page.pdf(prefer_css_page_size=True)
        assert pdf.startswith(b"%PDF")
        # A4 portrait = 595 x 842 pt
        import re

        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert box and abs(float(box.group(1)) - 595) < 2 and abs(float(box.group(2)) - 842) < 2
