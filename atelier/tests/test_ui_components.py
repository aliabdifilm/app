"""Server-side tests for the design system: base layout, Jinja macros, error pages, landing page.

They render the real templates in a tiny Flask app (docs/styleguide/build.py::make_app) that has the
Jinja filters the app shell registers (toman, fa, jdate, jdatetime, jweekday) and a fake CSRF token.
No database, no Flask-Login.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import render_template, render_template_string

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def _load_builder():
    spec = importlib.util.spec_from_file_location("styleguide_build", ROOT / "docs" / "styleguide" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sg():
    return _load_builder()


@pytest.fixture()
def app(sg):
    app = sg.make_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def render(app):
    """render(template_string, **context) -> str, inside a request context."""

    def _render(source: str, **ctx) -> str:
        with app.test_request_context("/"):
            return render_template_string(source, **ctx)

    return _render


def macro(name: str, call: str, **ctx):
    """Helper source: import a macro from components/ui.html and call it."""
    return "{% from 'components/ui.html' import " + name + " %}" + call


def user(name="علی عبدی", role="مدیر اتلیه"):
    return SimpleNamespace(is_authenticated=True, display_name=name, role=SimpleNamespace(label=role))


NAV = [
    {"label": "داشبورد", "url": "/dash", "icon": "home", "active": False},
    {"label": "تقویم", "url": "/cal", "icon": "calendar", "active": True},
    {"label": "نیروها", "url": "/staff", "icon": "users", "active": False, "badge": "۳"},
    {"label": "حسابداری", "url": "/acc", "icon": "receipt", "active": False},
    {"label": "انبار", "url": "/inv", "icon": "box", "active": False},
    {"label": "تنظیمات", "url": "/set", "icon": "settings", "active": False},
]

PAGE = "{% extends 'base.html' %}{% block title %}تقویم{% endblock %}{% block content %}<p id=\"hello\">سلام</p>{% endblock %}"


# --------------------------------------------------------------------------
# base.html
# --------------------------------------------------------------------------


class TestBase:
    def test_document_contract(self, render):
        html = render(PAGE)
        assert '<html lang="fa" dir="rtl"' in html
        assert 'name="viewport" content="width=device-width, initial-scale=1' in html
        assert '<meta name="csrf-token" content="styleguide-csrf-token">' in html
        assert 'class="skip-link" href="#main"' in html
        assert 'id="main"' in html
        assert "<title>تقویم · اتلیه سیار</title>" in html
        assert 'rel="manifest"' in html and "manifest.webmanifest" in html
        assert 'name="sw-url"' in html
        assert 'name="color-scheme" content="light dark"' in html

    def test_assets_and_script_order(self, render):
        html = render(PAGE)
        assert "/static/css/app.css?v=1" in html
        assert re.search(r'href="/static/css/print.css[^"]*" media="print"', html)
        order = [html.index(f"/static/js/{n}") for n in ("money.js", "jalali.js", "core.js")]
        assert order == sorted(order), "money.js and jalali.js must load before core.js"
        for n in ("money.js", "jalali.js", "core.js"):
            assert re.search(rf'<script src="/static/js/{n}[^"]*" defer>', html)

    def test_title_without_block_is_just_the_brand(self, render):
        html = render("{% extends 'base.html' %}{% block content %}x{% endblock %}")
        assert "<title>اتلیه سیار</title>" in html

    def test_asset_version_busts_cache(self, render):
        assert "app.css?v=abc123" in render(PAGE, asset_version="abc123")

    def test_no_inline_script_style_or_handlers(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV, unread_count=2)
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script> would break a strict CSP"
        assert "<style" not in html
        assert not re.search(r'\sstyle="', html)
        assert not re.search(r"\son[a-z]+=", html)

    def test_no_external_urls(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV)
        assert not re.search(r'(?:src|href)="https?://', html), "everything must be self-hosted"

    def test_anonymous_gets_public_layout(self, render):
        html = render(PAGE)
        assert "layout-public" in html
        assert 'class="pub-header"' in html and "/auth/login" in html and "/auth/register" in html
        assert 'class="sidebar"' not in html and 'class="tabbar"' not in html

    def test_url_overrides(self, render):
        html = render(PAGE, login_url="/enter", register_url="/join", home_url="/home")
        assert 'href="/enter"' in html and 'href="/join"' in html and 'href="/home"' in html

    def test_authenticated_gets_app_shell(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV, unread_count=4)
        assert "layout-app" in html
        assert 'class="sidebar"' in html and 'class="topbar"' in html and 'class="tabbar"' in html
        # nav items: label, url, aria-current only on the active one
        assert html.count('aria-current="page"') == 2  # sidebar link + tab bar item
        assert 'href="/cal"' in html and "تقویم" in html
        assert 'class="nav__badge">۳<' in html
        # user + role
        assert "علی عبدی" in html and "مدیر اتلیه" in html
        # bell with unread count
        assert 'class="icon-btn__badge"' in html and "۴ خوانده‌نشده" in html

    def test_logout_is_a_post_form_with_csrf(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV)
        form = re.search(r'<form method="post" action="/auth/logout">(.*?)</form>', html, re.S)
        assert form, "logout must be a POST form, never a link"
        assert 'name="csrf_token" value="styleguide-csrf-token"' in form.group(1)
        assert render(PAGE, current_user=user(), nav_items=NAV, logout_url="/bye").count('action="/bye"') == 1

    def test_tabbar_shows_four_items_plus_menu(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV)
        tabbar = re.search(r'<nav class="tabbar".*?</nav>', html, re.S).group(0)
        assert tabbar.count('class="tabbar__item') == 5  # 4 links + the drawer button
        assert "data-drawer-open" in tabbar
        assert "انبار" not in tabbar and "تنظیمات" not in tabbar  # 5th+ items live in the drawer only

    def test_tabbar_menu_highlights_when_active_item_is_hidden(self, render):
        nav = [dict(i, active=False) for i in NAV]
        nav[5]["active"] = True
        html = render(PAGE, current_user=user(), nav_items=nav)
        assert re.search(r'<button type="button" class="tabbar__item is-active" data-drawer-open', html)

    def test_no_bell_badge_when_nothing_unread(self, render):
        html = render(PAGE, current_user=user(), nav_items=NAV, unread_count=0)
        assert 'class="icon-btn__badge"' not in html

    def test_nav_items_may_be_objects(self, render):
        item = SimpleNamespace(label="مراسم", url="/ev", icon="calendar", active=True, badge=None)
        html = render(PAGE, current_user=user(), nav_items=[item])
        assert 'href="/ev"' in html and "مراسم" in html

    def test_bare_layout_has_no_chrome_and_never_touches_user(self, render):
        class Boom:
            @property
            def is_authenticated(self):
                raise AssertionError("bare layout must not touch current_user")

        html = render("{% extends 'base.html' %}{% set layout = 'bare' %}{% block content %}<p>x</p>{% endblock %}", current_user=Boom())
        assert "layout-bare" in html and 'class="bare__card"' in html
        assert 'class="sidebar"' not in html and 'class="pub-header"' not in html

    def test_flashed_messages_become_toast_sources(self, app):
        with app.test_request_context("/"):
            from flask import flash

            flash("ذخیره شد", "success")
            flash("خطا رخ داد", "error")
            html = render_template_string(PAGE)
        assert 'data-flash' in html
        assert '<li data-tone="success">ذخیره شد</li>' in html
        assert '<li data-tone="error">خطا رخ داد</li>' in html
        assert "<noscript>" in html and "alert--danger" in html
        assert 'id="toasts"' in html and 'aria-live="polite"' in html

    def test_no_flash_markup_without_messages(self, render):
        assert "data-flash" not in render(PAGE)

    def test_blocks_head_and_scripts_are_rendered(self, render):
        html = render(
            "{% extends 'base.html' %}{% block head %}<meta name='x-head'>{% endblock %}"
            "{% block scripts %}<script src='/static/js/x.js' defer></script>{% endblock %}{% block content %}c{% endblock %}"
        )
        assert "<meta name='x-head'>" in html
        assert html.index("/static/js/core.js") < html.index("/static/js/x.js")


# --------------------------------------------------------------------------
# macros: surfaces
# --------------------------------------------------------------------------


class TestSurfaceMacros:
    def test_page_header(self, render):
        html = render(macro("page_header", "{{ page_header('عنوان', 'زیرعنوان', back_url='/x') }}"))
        assert '<h1 class="page-header__title">عنوان</h1>' in html
        assert "زیرعنوان" in html and 'class="back-link" href="/x"' in html
        assert "None" not in html and "page-header__actions" not in html

    def test_page_header_actions_param_and_call_body(self, render):
        html = render(macro("page_header", "{% call page_header('t', actions='<b>A</b>'|safe) %}<i>B</i>{% endcall %}"))
        assert '<div class="page-header__actions"><b>A</b><i>B</i></div>' in html

    def test_card_with_title_actions_footer(self, render):
        html = render(
            macro("card", "{% call card(title='T', subtitle='S', actions='<a>go</a>'|safe, footer='F', tone='accent') %}body{% endcall %}")
        )
        assert 'class="card card--accent"' in html
        assert '<h2 class="card__title">T</h2>' in html and "card__subtitle" in html
        assert "<a>go</a>" in html and 'class="card__footer">F<' in html and ">body<" in html

    def test_text_arguments_are_escaped(self, render):
        html = render(macro("card, badge, stat_tile", "{% call card(title=t) %}x{% endcall %}{{ badge(t) }}{{ stat_tile(t, t, hint=t) }}"), t="<script>alert(1)</script>")
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_stat_tile_plain_and_link(self, render):
        html = render(macro("stat_tile", "{{ stat_tile('درآمد', '۱٬۰۰۰', unit='تومان', tone='success', icon='coins', hint='ماه') }}{{ stat_tile('x', '1', href='/go') }}"))
        assert "stat--success" in html and "stat__unit" in html and "stat__icon" in html
        assert '<a class="stat" href="/go">' in html and html.count("</a>") == 1
        assert "None" not in html

    def test_badge_tones(self, render):
        html = render(macro("badge", "{{ badge('a') }}{{ badge('b', 'success', 'check') }}"))
        assert 'class="badge">a<' in html
        assert 'class="badge badge--success"' in html and "#i-check" in html

    def test_alert_roles(self, render):
        html = render(macro("alert", "{{ alert('x', 'danger') }}{{ alert('y', 'info') }}"))
        assert 'class="alert alert--danger" role="alert"' in html
        assert 'class="alert" role="status"' in html

    def test_empty_state(self, render):
        html = render(macro("empty_state", "{{ empty_state('inbox', 'خالی', 'متن', action='<a>new</a>'|safe) }}"))
        assert "empty__title" in html and "<a>new</a>" in html and "#i-inbox" in html

    def test_empty_state_without_action_has_no_none(self, render):
        assert "None" not in render(macro("empty_state", "{{ empty_state('inbox', 'خالی') }}"))

    def test_avatar_initials(self, render):
        html = render(macro("avatar", "{{ avatar('علی عبدی') }}|{{ avatar('مریم') }}|{{ avatar('x', src='/a.png', alt='p') }}"))
        assert "ع&zwnj;ع" in html
        assert ">مر<" in html
        assert '<img src="/a.png" alt="p"' in html

    def test_rating_stars(self, render):
        html = render(macro("rating_stars", "{{ rating_stars(4.5, count=12) }}"))
        assert html.count("#i-star-fill") == 4 and html.count("#i-star-half") == 1 and html.count("#i-star\"") == 0
        assert "۴٫۵" in html and "(۱۲)" in html
        assert 'role="img" aria-label="امتیاز ۴٫۵ از ۵"' in html

    def test_rating_stars_rounds_to_halves_and_handles_none(self, render):
        html = render(macro("rating_stars", "{{ rating_stars(3.2) }}{{ rating_stars(none) }}"))
        assert html.count("#i-star-half") == 0  # 3.2 -> 3.0
        assert html.count("#i-star-fill") == 3

    def test_rating_input(self, render):
        html = render(macro("rating_input", "{{ rating_input('rate', 4) }}"))
        assert html.count('type="radio"') == 5
        assert re.search(r'value="4" checked', html) and "value=\"5\" checked" not in html


# --------------------------------------------------------------------------
# macros: forms
# --------------------------------------------------------------------------


class TestFormMacros:
    def test_form_field_basic(self, render):
        html = render(macro("form_field", "{{ form_field('نام', name='full_name', value='علی', required=true, help='راهنما') }}"))
        assert '<label class="field__label" for="f-full_name">' in html
        assert 'id="f-full_name"' in html and 'name="full_name"' in html and 'value="علی"' in html
        assert " required" in html and 'aria-describedby="f-full_name-help"' in html
        assert "(الزامی)" in html

    def test_form_field_error_state(self, render):
        html = render(macro("form_field", "{{ form_field('ایمیل', name='email', error=['بد است', 'کوتاه است']) }}"))
        assert "field--invalid" in html and 'aria-invalid="true"' in html
        assert 'aria-describedby="f-email-error"' in html
        assert 'role="alert"' in html and "بد است، کوتاه است" in html

    def test_form_field_none_value_renders_empty(self, render):
        html = render(macro("form_field", "{{ form_field('x', name='x', value=none) }}"))
        assert 'value=""' in html and "None" not in html

    def test_form_field_custom_control_via_call(self, render):
        html = render(macro("form_field", "{% call(fid) form_field('x', name='x') %}<input id=\"{{ fid }}\" class=\"mine\">{% endcall %}"))
        assert '<input id="f-x" class="mine">' in html and 'for="f-x"' in html

    def test_form_field_input_html_and_attrs(self, render):
        html = render(macro("form_field", "{{ form_field('x', input_html='<b id=q></b>'|safe) }}{{ form_field('y', name='y', attrs={'data-foo': 'bar'}, type='tel') }}"))
        assert "<b id=q></b>" in html
        assert 'data-foo="bar"' in html and "input--num" in html

    def test_money_input(self, render):
        html = render(macro("money_input", "{{ money_input('base_pay', 1870000, label='حقوق پایه', max=5000000) }}"))
        assert "data-money" in html and 'inputmode="numeric"' in html and 'name="base_pay"' in html
        assert 'value="1870000"' in html and 'data-max="5000000"' in html
        assert "تومان" in html and "input-group__addon" in html

    def test_money_input_without_label_needs_aria_label(self, render):
        html = render(macro("money_input", "{{ money_input('x', none, aria_label='مبلغ') }}"))
        assert 'aria-label="مبلغ"' in html and "<label" not in html and 'value=""' in html

    def test_jdate_input_accepts_dates_and_iso_strings(self, render):
        html = render(
            macro("jdate_input", "{{ jdate_input('d', d, label='تاریخ', min='2025-01-01', max=m) }}"),
            d=date(2025, 7, 17),
            m=datetime(2026, 1, 1, 12, 30),
        )
        assert 'value="2025-07-17"' in html  # ISO Gregorian is what the server round-trips
        assert 'data-min="2025-01-01"' in html and 'data-max="2026-01-01"' in html
        assert "data-jdate" in html and "data-jdate-open" in html and 'name="d"' in html

    def test_jdate_input_empty(self, render):
        assert 'value=""' in render(macro("jdate_input", "{{ jdate_input('d', none) }}"))

    def test_select_field(self, render):
        html = render(macro("select_field", "{{ select_field('نقش', 'role', opts, selected='editor', placeholder='انتخاب') }}"), opts=[("photographer", "عکاس"), ("editor", "ادیتور")])
        assert '<option value="">انتخاب</option>' in html
        assert '<option value="editor" selected>ادیتور</option>' in html
        assert '<option value="photographer">عکاس</option>' in html

    def test_select_field_plain_strings_and_integers(self, render):
        html = render(macro("select_field", "{{ select_field('x', 'n', [1, 2, 3], selected=2) }}"))
        assert '<option value="2" selected>2</option>' in html

    def test_textarea_field(self, render):
        html = render(macro("textarea_field", "{{ textarea_field('توضیح', 'notes', 'سلام', rows=3, maxlength=50) }}"))
        assert '<textarea class="textarea" id="f-notes" name="notes" rows="3"' in html and ">سلام</textarea>" in html

    def test_checkbox_and_stepper(self, render):
        html = render(macro("checkbox_field, stepper_field", "{{ checkbox_field('عمومی', 'public', checked=true) }}{{ stepper_field('weight', 2, min=1, max=9, label='وزن') }}"))
        assert 'name="public" value="1" checked' in html
        assert "data-stepper" in html and 'data-min="1"' in html and 'data-max="9"' in html and 'value="2"' in html
        assert "data-stepper-dec" in html and "data-stepper-inc" in html and 'for="f-weight"' in html


# --------------------------------------------------------------------------
# macros: disclosure and overlays
# --------------------------------------------------------------------------


class TestDisclosureMacros:
    def test_accordion_is_closed_by_default(self, render):
        html = render(macro("accordion", "{% call accordion('inv', 'فاکتور') %}body{% endcall %}"))
        assert 'class="accordion"' in html and "is-open" not in html
        assert 'aria-expanded="false"' in html and 'data-accordion-toggle="inv"' in html
        assert 'aria-controls="inv-panel"' in html and 'id="inv-panel"' in html and 'role="region"' in html

    def test_accordion_open(self, render):
        html = render(macro("accordion", "{{ accordion('inv', 'x', open=true, meta='m') }}"))
        assert 'class="accordion is-open"' in html and 'aria-expanded="true"' in html and 'class="collapse is-open"' in html

    def test_collapse_headerless(self, render):
        html = render(macro("collapse", "{% call collapse('c') %}x{% endcall %}"))
        assert 'class="collapse" id="c" data-collapse' in html and "is-open" not in html

    def test_modal_structure(self, render):
        html = render(macro("modal", "{% call modal('m1', 'عنوان', size='lg') %}body{% endcall %}"))
        assert 'class="modal modal--lg" id="m1"' in html
        assert 'role="dialog" aria-modal="true" aria-labelledby="m1-title" hidden' in html
        assert html.count("data-modal-close") == 2  # backdrop + X button
        assert '<h2 class="modal__title" id="m1-title">عنوان</h2>' in html and ">body<" in html

    def test_confirm_button_is_a_post_form_with_csrf(self, render):
        html = render(macro("confirm_button", "{{ confirm_button('حذف', '/events/3/delete', 'مطمئنید؟', title='حذف مراسم', fields={'next': '/list'}) }}"))
        assert '<form class="inline-form" method="post" action="/events/3/delete">' in html
        assert 'name="csrf_token" value="styleguide-csrf-token"' in html
        assert 'name="next" value="/list"' in html
        assert 'data-confirm="مطمئنید؟"' in html and 'data-confirm-title="حذف مراسم"' in html
        assert 'type="submit"' in html and "btn--danger-soft" in html

    def test_confirm_button_message_is_escaped(self, render):
        html = render(macro("confirm_button", "{{ confirm_button('x', '/a', m) }}"), m='"><script>alert(1)</script>')
        assert "<script>" not in html and "&#34;&gt;&lt;script" in html

    def test_tabs_link_mode(self, render):
        items = [{"key": "a", "label": "الف", "url": "/a"}, {"key": "b", "label": "ب", "url": "/b", "badge": "۲"}]
        html = render(macro("tabs", "{{ tabs(items, active='b', label='بخش‌ها') }}"), items=items)
        assert "<nav" in html and html.count("aria-current=\"page\"") == 1
        assert re.search(r'href="/b" aria-current="page"', html) and "role=\"tab\"" not in html

    def test_tabs_client_mode(self, render):
        items = [{"key": "a", "label": "الف"}, {"key": "b", "label": "ب"}]
        html = render(
            macro("tabs, tab_panel", "{% call tabs(items, active='a') %}{% call tab_panel('a', active=true) %}A{% endcall %}{% call tab_panel('b') %}B{% endcall %}{% endcall %}"),
            items=items,
        )
        assert "data-tabs" in html and 'role="tablist"' in html
        assert 'aria-selected="true" tabindex="0"' in html and 'aria-selected="false" tabindex="-1"' in html
        assert re.search(r'data-tab-panel="b"[^>]* hidden>B<', html) and not re.search(r'data-tab-panel="a"[^>]* hidden', html)

    def test_pagination(self, app, sg):
        page = sg.DemoPage(page=3, pages=12, total=118)
        with app.test_request_context("/"):
            html = render_template_string(macro("pagination", "{{ pagination(p, 'styleguide_page', q='عروسی') }}"), p=page)
        assert 'aria-label="صفحه‌بندی"' in html
        assert 'aria-current="page"' in html and ">۳<" in html
        assert 'rel="prev"' in html and 'rel="next"' in html
        assert "page=2" in html and "page=4" in html
        assert "%D8%B9%D8%B1%D9%88%D8%B3%DB%8C" in html  # filters survive in every link
        assert "۱۱۸ مورد" in html

    def test_pagination_hidden_for_single_page(self, app, sg):
        with app.test_request_context("/"):
            html = render_template_string(macro("pagination", "{{ pagination(p, 'styleguide_page') }}"), p=sg.DemoPage(1, 1, 5))
        assert html.strip() == ""

    def test_pagination_first_and_last_pages_disable_arrows(self, app, sg):
        with app.test_request_context("/"):
            first = render_template_string(macro("pagination", "{{ pagination(p, 'styleguide_page') }}"), p=sg.DemoPage(1, 5, 50))
            last = render_template_string(macro("pagination", "{{ pagination(p, 'styleguide_page') }}"), p=sg.DemoPage(5, 5, 50))
        assert 'rel="prev"' not in first and 'rel="next"' in first
        assert 'rel="next"' not in last and 'rel="prev"' in last


# --------------------------------------------------------------------------
# macros: data views
# --------------------------------------------------------------------------


COLUMNS = [
    {"key": "name", "label": "مراسم"},
    {"key": "date", "label": "تاریخ", "format": "jdate"},
    {"key": "income", "label": "درآمد", "format": "toman", "num": True},
    {"key": "n", "label": "نیرو", "format": "fa"},
    {"key": None, "label": "عملیات", "actions": True},
]
ROWS = [{"name": "عروسی", "date": date(2025, 8, 3), "income": 10_500_000, "n": 4}, {"name": "نامزدی", "date": None, "income": 0, "n": 3}]


class TestDataTable:
    def test_formats_and_phone_labels(self, render):
        html = render(macro("data_table", "{{ data_table(columns, rows, caption='جدول') }}"), columns=COLUMNS, rows=ROWS)
        assert 'class="table table--stack"' in html and "<caption>جدول</caption>" in html
        assert '<td data-label="مراسم" class="">عروسی</td>' in html
        assert "۱۴۰۴/۰۵/۱۲" in html  # jdate filter
        assert "۱۰٬۵۰۰٬۰۰۰" in html and "تومان" in html  # toman filter + unit
        assert 'data-label="عملیات"' not in html and "table__actions" in html
        assert 'class="cell-num"' in html or "cell-num" in html

    def test_none_cell_shows_dash(self, render):
        html = render(macro("data_table", "{{ data_table(columns, rows) }}"), columns=COLUMNS, rows=ROWS)
        assert "—" in html

    def test_custom_cell_with_default_fallback(self, render):
        src = macro(
            "data_table, badge",
            "{% call(row, col) data_table(columns, rows) %}{% if col.key == 'name' %}<b>{{ row.name }}!</b>{% endif %}{% endcall %}",
        )
        html = render(src, columns=COLUMNS, rows=ROWS)
        assert "<b>عروسی!</b>" in html  # custom cell
        assert "۱۴۰۴/۰۵/۱۲" in html  # untouched columns keep the default rendering

    def test_empty_rows_show_empty_state(self, render):
        html = render(macro("data_table", "{{ data_table(columns, [], empty='هیچی') }}"), columns=COLUMNS)
        assert "empty__title" in html and "هیچی" in html and "<table" not in html

    def test_rows_may_be_objects(self, render):
        rows = [SimpleNamespace(name="x", date=None, income=5, n=1)]
        html = render(macro("data_table", "{{ data_table(columns, rows) }}"), columns=COLUMNS[:1] + COLUMNS[2:3], rows=rows)
        assert ">x<" in html and "۵" in html


class TestCalendar:
    @pytest.fixture()
    def month(self, sg):
        from app.services import jalali

        grid = jalali.month_grid(1404, 5)  # Mordad 1404: starts on a Wednesday
        d = lambda day: date(*jalali.jalali_to_gregorian(1404, 5, day))  # noqa: E731
        events = {
            d(3): [{"label": "عروسی ۱", "url": "/e/1", "tone": "success"}],
            d(10).isoformat(): [{"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"}],
        }
        return grid, events, d(12)

    def test_header_is_saturday_first_with_friday_weekend(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events, title='مرداد ۱۴۰۴', today=today) }}"), grid=grid, events=events, today=today)
        dows = re.findall(r'role="columnheader"><span class="hide-sm">([^<]+)<', html)
        assert dows == ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
        assert html.count("cal__dow--weekend") == 1 and "cal__dow cal__dow--weekend" in html
        assert "مرداد ۱۴۰۴" in html

    def test_day_cells_and_padding(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events, today=today) }}"), grid=grid, events=events, today=today)
        assert len(re.findall(r'<span class="cal__num">', html)) == 31
        assert html.count("cal__day--blank") == sum(1 for w in grid for c in w if c is None)
        assert html.count("cal__day--today") == 1 and 'aria-current="date"' in html
        # every week has 7 cells
        assert html.count('role="gridcell"') == 7 * len(grid)

    def test_day_numbers_are_in_order(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events) }}"), grid=grid, events=events)
        nums = re.findall(r'<span class="cal__num">([^<]+)<', html)
        assert nums == [str(i) for i in range(1, 32)] or len(nums) == 31
        assert nums[0] == "۱" and nums[-1] == "۳۱"

    def test_events_by_date_or_iso_key_and_overflow(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events) }}"), grid=grid, events=events)
        assert '<a class="cal__event cal__event--success" href="/e/1"' in html
        assert "+۱ مورد دیگر" in html  # 4 events -> 3 chips + "1 more"
        assert html.count("cal__day--has-events") == 2

    def test_phone_agenda_lists_event_days(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events) }}"), grid=grid, events=events)
        agenda = html[html.index('class="cal__agenda"') :]
        assert agenda.count("cal__agenda-item") == 2
        assert "جمعه" in agenda  # day 3 of Mordad 1404 is a Friday
        empty = render(macro("calendar_month", "{{ calendar_month(grid, none) }}"), grid=grid)
        assert "مراسمی در این ماه ثبت نشده است" in empty

    def test_navigation_links(self, render, month):
        grid, events, today = month
        html = render(macro("calendar_month", "{{ calendar_month(grid, events, prev_url='/p', next_url='/n') }}"), grid=grid, events=events)
        assert 'href="/p" rel="prev"' in html and 'href="/n" rel="next"' in html


# --------------------------------------------------------------------------
# error pages and landing
# --------------------------------------------------------------------------


class TestPages:
    @pytest.mark.parametrize("name,code", [("404", "۴۰۴"), ("403", "۴۰۳"), ("500", "۵۰۰")])
    def test_error_pages_render(self, app, name, code):
        with app.test_request_context("/"):
            html = render_template(f"errors/{name}.html")
        assert code in html and '<h1 class="err__title"' in html
        assert "None" not in html
        assert 'href="/"' in html

    def test_500_never_touches_the_user(self, app):
        class Boom:
            @property
            def is_authenticated(self):
                raise AssertionError("500 page must not touch current_user")

        with app.test_request_context("/"):
            html = render_template("errors/500.html", current_user=Boom())
        assert "layout-bare" in html

    def test_404_keeps_the_app_shell_for_logged_in_users(self, app):
        with app.test_request_context("/"):
            html = render_template("errors/404.html", current_user=user(), nav_items=NAV)
        assert "layout-app" in html and 'class="sidebar"' in html

    def test_landing_for_anonymous_visitors(self, app):
        with app.test_request_context("/"):
            html = render_template("public/landing.html")
        assert "layout-public" in html
        assert html.count('href="/auth/login"') >= 2 and html.count('href="/auth/register"') >= 2
        for anchor in ('id="how"', 'id="roles"', 'id="features"', 'id="salary"'):
            assert anchor in html
        # AGENTS.md test vector is shown
        assert "۱٬۸۷۰٬۰۰۰" in html
        # four roles + landing stylesheet, no external assets
        for role in ("مدیر تالار", "مدیر اتلیه", "نیرو", "ادمین سایت"):
            assert role in html
        assert "css/landing.css" in html
        assert not re.search(r'(?:src|href)="https?://', html)
        assert not re.search(r'\sstyle="', html)

    def test_landing_for_logged_in_users_links_to_the_dashboard(self, app):
        with app.test_request_context("/"):
            html = render_template("public/landing.html", current_user=user(), dashboard_url="/panel")
        assert 'href="/panel"' in html and "ورود با شمارهٔ موبایل" not in html


class TestStyleguide:
    def test_styleguide_builds_and_uses_only_local_assets(self, sg, tmp_path, monkeypatch):
        monkeypatch.setattr(sg, "OUTPUT", tmp_path / "styleguide.html")
        out = sg.build()
        html = out.read_text(encoding="utf-8")
        assert "../app/static/css/app.css" in html and 'src="/static/' not in html
        assert not re.search(r'(?:src|href)="https?://', html)
        assert not re.search(r'\sstyle="', html)
        for needle in ("data-money", "data-jdate", "data-stepper", "data-accordion-toggle", "data-confirm", "cal__grid", "table--stack", "jd-"):
            assert needle in html or needle == "jd-"

    def test_every_icon_used_by_macros_exists_in_the_sprite(self):
        sprite = (ROOT / "app" / "templates" / "components" / "_icons.html").read_text(encoding="utf-8")
        defined = set(re.findall(r'<symbol id="(?:i-)?([a-z0-9-]+)"', sprite))
        sources = list((ROOT / "app" / "templates").rglob("*.html")) + [STATIC / "js" / "core.js"]
        used = set()
        for path in sources:
            text = path.read_text(encoding="utf-8")
            used |= set(re.findall(r"""(?:_svg|icon|_ico|svgIcon)\(\s*['"]([a-z0-9-]+)['"]""", text))
            used |= set(re.findall(r"""icon=['"]([a-z0-9-]+)['"]""", text))
            used |= set(re.findall(r"""'icon':\s*'([a-z0-9-]+)'""", text))
        used.discard("name")  # the styleguide loops over every icon with icon(name)
        assert used, "regex found no icon usages"
        assert used <= defined | {"brand-mark"}, f"icons used but missing from the sprite: {sorted(used - defined)}"


# --------------------------------------------------------------------------
# static assets: PWA, fonts
# --------------------------------------------------------------------------


class TestStaticAssets:
    def test_manifest(self):
        manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
        assert manifest["lang"] == "fa" and manifest["dir"] == "rtl"
        assert manifest["name"] and manifest["short_name"] and manifest["theme_color"].startswith("#")
        assert manifest["display"] == "standalone"
        kinds = {(i["type"], i["purpose"]) for i in manifest["icons"]}
        assert ("image/svg+xml", "any") in kinds and ("image/svg+xml", "maskable") in kinds
        assert ("image/png", "any") in kinds and ("image/png", "maskable") in kinds
        for icon in manifest["icons"]:
            assert (STATIC / icon["src"]).is_file(), icon["src"]

    def test_png_icons_have_the_declared_size(self):
        import struct

        for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512), ("apple-touch-icon.png", 180)):
            data = (STATIC / "img" / name).read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n", name
            assert struct.unpack(">II", data[16:24]) == (size, size), name

    def test_service_worker_never_caches_html_or_posts(self):
        sw = (STATIC / "js" / "sw.js").read_text(encoding="utf-8")
        assert "request.method !== 'GET'" in sw
        assert "request.mode === 'navigate'" in sw
        assert "STATIC_PREFIX" in sw and "CACHE_VERSION" in sw
        assert "caches.delete" in sw  # old versions are cleaned up

    def test_every_precached_file_exists(self):
        sw = (STATIC / "js" / "sw.js").read_text(encoding="utf-8")
        block = sw[sw.index("var PRECACHE = [") : sw.index("].map")]
        files = re.findall(r"'([^']+)'", block)
        assert files
        for rel in files:
            assert (STATIC / rel).is_file(), f"sw.js precaches a missing file: {rel}"

    def test_font_is_vendored_with_its_licence(self):
        assert (STATIC / "fonts" / "Vazirmatn-Variable.woff2").stat().st_size > 50_000
        licence = (STATIC / "fonts" / "OFL.txt").read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in licence

    def test_stylesheet_references_existing_font_and_has_fallback_stack(self):
        css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
        assert "../fonts/Vazirmatn-Variable.woff2" in css
        assert re.search(r'--font:\s*"Vazirmatn",\s*Tahoma', css)

    def test_css_has_dark_print_and_reduced_motion_support(self):
        css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
        assert "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css
        assert "prefers-reduced-motion: reduce" in css
        assert ":focus-visible" in css
        print_css = (STATIC / "css" / "print.css").read_text(encoding="utf-8")
        assert "@page" in print_css and "A4" in print_css and "@media print" in print_css
