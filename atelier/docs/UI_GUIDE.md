# UI guide — Atelier Sayyar design system

Everything a module author needs to build a page that looks and behaves like the rest of the app:
Persian, right-to-left, phone-first, accessible, no CDN, no build step.

- Live catalogue of every component (light/dark, 390px and 1280px): open `docs/styleguide.html`
  (rebuild it with `python docs/styleguide/build.py`).
- Rule of thumb: **use a macro or a class from here before writing your own HTML/CSS.** If something
  is missing, add it to the design system (section 15) instead of a one-off in your module.

Contents: [1 Quick start](#1-quick-start) · [2 Layouts and context](#2-layouts-and-context-variables) ·
[3 Tokens](#3-design-tokens) · [4 Layout utilities](#4-layout-utilities-and-classes) ·
[5 Components](#5-components-macros) · [6 Widgets](#6-declarative-widgets-no-js-to-write) ·
[7 JavaScript API](#7-javascript-api-windowatelier) · [8 Recipes](#8-recipes) ·
[9 Printing](#9-printing-and-invoices) · [10 RTL and numbers](#10-rtl-persian-digits-and-dates) ·
[11 Accessibility](#11-accessibility-checklist) · [12 PWA](#12-pwa-and-the-service-worker) ·
[13 CSP](#13-content-security-policy) · [14 Testing](#14-testing) · [15 Contributing](#15-changing-the-design-system)

---

## 1. Quick start

A page is a template that extends `base.html`, imports the macros it needs and fills `content`:

```jinja
{% extends 'base.html' %}
{% from 'components/ui.html' import page_header, card, badge, empty_state %}

{% block title %}مراسم‌ها{% endblock %}          {# becomes "مراسم‌ها · اتلیه سیار" #}

{% block content %}
<div class="page stack stack--lg">
  {{ page_header('مراسم‌ها', 'فهرست مراسم‌های این ماه',
       actions='<a class="btn btn--primary" href="/events/new">ثبت مراسم</a>'|safe) }}

  {% call card(title='پنجشنبه ۲۶ تیر') %}
    <p>محتوای کارت</p>
  {% endcall %}
</div>
{% endblock %}

{% block scripts %}
  <script src="{{ url_for('static', filename='js/events.js') }}" defer></script>   {# your module's JS #}
{% endblock %}
```

* Wrap page content in `<div class="page">` (max-width 1200px, centred). Use `page page--narrow` for forms (760px).
* The app shell already provides the sidebar, top bar, phone tab bar, toasts and the icon sprite.
* Module CSS goes in `app/static/css/<module>.css`, linked from `{% block head %}`. Use tokens (section 3), never hex colours.
* Module JS goes in `app/static/js/<module>.js` and is loaded with `defer` from `{% block scripts %}`
  (it then runs *after* `core.js`). An inline `<script>` runs too early: wrap it in
  `document.addEventListener('DOMContentLoaded', …)` or, better, avoid inline scripts (section 13).

## 2. Layouts and context variables

`base.html` has three layouts. It picks one automatically; override with `{% set layout = '…' %}`
at the top of the child template (outside any block) or by passing `layout=` from the view.

| layout   | what you get                                                    | default when             |
|----------|------------------------------------------------------------------|--------------------------|
| `app`    | sidebar (drawer on phones) + top bar + bottom tab bar            | user is logged in        |
| `public` | marketing header (login/register buttons) + footer               | visitor is anonymous     |
| `bare`   | a centred card with the brand; no navigation, **no DB access**   | never — set it yourself (login/OTP screens, the 500 page) |

Blocks: `title`, `head`, `content`, `scripts`; plus `body_class`, `public_nav`, `public_footer`, `bare_brand`.

Context variables read by `base.html` (all optional, all have safe defaults):

| variable            | default            | meaning |
|---------------------|--------------------|---------|
| `nav_items`         | `[]`               | list of `{label, url, icon, active, badge?}` (dicts **or** objects). `icon` is a name from the sprite (`docs/styleguide.html#icons`). `active` marks the current section; `badge` is a short string such as `'۳'`. |
| `current_user`      | —                  | Flask-Login user. Uses `.is_authenticated`, `.display_name`, `.role.label`. |
| `unread_count`      | `0`                | notification bell badge (`99+` after 99) |
| `asset_version`     | `'1'`              | appended as `?v=` to app CSS/JS to bust caches after a deploy |
| `home_url`          | `/`                | brand link |
| `login_url`         | `/auth/login`      | |
| `register_url`      | `/auth/register`   | |
| `logout_url`        | `/auth/logout`     | target of the logout **POST** form (with CSRF) |
| `notifications_url` | `/notifications`   | bell link |
| `dashboard_url`     | `/dashboard`       | "ورود به پنل" button for logged-in visitors of the landing page |
| `sw_url`, `sw_scope`| static `sw.js`, `/`| see section 12 |

The app shell is expected to provide these through a context processor (`nav_items` with `active`
computed from `request.endpoint`, `unread_count`, and the URLs if its blueprints use other paths).

Every page must work from **390px wide** first. Test with the browser's device toolbar.

## 3. Design tokens

All in `app/static/css/app.css` (section 2 of the file). Light values; dark mode redefines the colour tokens
automatically (`prefers-color-scheme: dark`, or `data-theme="dark|light"` on `<html>` set by the theme toggle).

| group | tokens |
|-------|--------|
| surfaces | `--bg` page · `--surface` cards/inputs · `--surface-2` table heads, footers, hover · `--surface-3` tracks · `--border` hairlines · `--border-strong` control borders |
| text | `--text` · `--text-muted` · `--text-faint` (placeholders) |
| accent (warm copper) | `--accent` fills · `--accent-hover` · `--accent-active` · `--accent-text` (links; AA on every surface) · `--on-accent` · `--accent-soft` · `--accent-ink` (text on soft) |
| semantic | `--success` `--warning` `--danger` `--info`, each with `-soft` (background) and `-ink` (text on soft) |
| focus | `--focus` (3px ring, offset 2px) |
| spacing (4/8px) | `--sp-1` 4 · `--sp-2` 8 · `--sp-3` 12 · `--sp-4` 16 · `--sp-5` 24 · `--sp-6` 32 · `--sp-7` 48 · `--sp-8` 64 |
| type | `--font` (Vazirmatn, Tahoma, Segoe UI, system-ui) · `--fs-xs` 13 · `--fs-sm` 14 · `--fs-md` 16 · `--fs-lg` 18 · `--fs-xl` 22 · `--fs-2xl` 28 · `--fs-3xl` 36 |
| shape | `--r-sm` 8 · `--r-md` 12 · `--r-lg` 16 · `--r-xl` 24 · `--tap` 44px |
| elevation | `--shadow-sm` `--shadow-md` `--shadow-lg` |

Rules: **never write a hex colour in module CSS**; use `var(--…)`. Pairs are contrast-checked (WCAG AA) by
`tests/test_ui_contrast.py`: put `--text` / `--text-muted` on `--bg`/`--surface`/`--surface-2`, `-ink` on `-soft`,
`--on-accent` on `--accent`. Do not put `--text-muted` on a coloured background.

The font is Vazirmatn (SIL OFL), vendored as a variable woff2 in `app/static/fonts/`; the licence is next to it.
If it ever fails to load, Tahoma / system UI is used.

## 4. Layout utilities and classes

```html
<div class="page stack stack--lg">…</div>                 <!-- vertical rhythm: stack, stack--sm, stack--lg, stack--xl -->
<div class="cluster cluster--between">…</div>              <!-- flex row that wraps: cluster, --sm, --between, --end, --center -->
<div class="grid grid--stats">…</div>                      <!-- responsive grid: grid, grid--2, grid--3, grid--4, grid--stats, grid--fit -->
<div class="form-grid form-grid--2">…</div>                <!-- 1 column on phones, 2 (or --3) from 640px; .span-all for a full row -->
<span class="num">۰۹۱۲۳۴۵۶۷۸۹</span>                       <!-- phone numbers, codes, signed amounts: always read left-to-right -->
<p class="text-muted text-sm">…</p>                        <!-- text-muted/-faint/-accent/-success/-danger, text-sm/-xs/-lg, text-center/-end, fw-bold -->
<div class="hide-sm">…</div> <div class="show-sm">…</div>  <!-- hide/show below 640px -->
<dl class="kv"><dt>درآمد</dt><dd>…</dd></dl>               <!-- label/value list -->
<div class="progress"><span></span></div>
```

Buttons (always give icon-only buttons an `aria-label`):

```html
<button class="btn btn--primary">ذخیره</button>
<a class="btn btn--secondary" href="…">ثانویه</a>
<button class="btn btn--ghost">شفاف</button>
<button class="btn btn--danger">حذف</button>   <button class="btn btn--danger-soft">انصراف از مراسم</button>
<button class="btn btn--primary btn--lg btn--block">تمام‌عرض</button>      <!-- also btn--sm, btn--icon -->
<button class="icon-btn" aria-label="ویرایش">{{ icon('edit') }}</button>
```
Every `btn` is 44px tall. Forms with `method="post"` automatically get double-submit protection (the
submit button shows a spinner and the form cannot be sent twice); opt out with `data-no-lock`.

## 5. Components (macros)

Import only what you use: `{% from 'components/ui.html' import card, badge %}`.
Arguments that hold HTML (`actions`, `action`, `footer`, `input_html`) must be marked `|safe` — or use
`{% call %}` blocks — everything else is escaped.

### icon(name, size=None, cls='')
`{{ icon('calendar') }}` — inline SVG from the built-in sprite (about 75 icons; the catalogue is in the styleguide).
Icons are decorative (`aria-hidden`); put the meaning in text or an `aria-label`.
Raw HTML: `<svg class="icon" aria-hidden="true"><use href="#i-calendar"/></svg>`.

### page_header(title, subtitle=None, actions=None, back_url=None, back_label='بازگشت')
```jinja
{% call page_header('ویرایش مراسم', 'عروسی تالار گل‌های سپید', back_url=url_for('events.index')) %}
  <a class="btn" href="…">چاپ</a><button class="btn btn--primary" form="ev-form">ذخیره</button>
{% endcall %}
```

### card(title=None, subtitle=None, actions=None, footer=None, tone=None, flat=False, padded=True, cls='', id=None, tag='h2')
`tone`: `accent | success | warning | danger` (coloured start border). Body goes in the `{% call %}`.
```jinja
{% call card(title='نیروها', actions='<a class="btn btn--sm" href="#">افیش</a>'|safe, tone='accent') %}…{% endcall %}
```
Use `padded=False` for a table/list that should touch the card edges.

### stat_tile(label, value, hint=None, tone=None, icon=None, href=None, unit=None)
```jinja
<div class="grid grid--stats">
  {{ stat_tile('درآمد', income|toman, unit='تومان', tone='success', icon='coins', hint='مرداد ۱۴۰۴') }}
  {{ stat_tile('اعلان‌ها', unread|fa, icon='bell', href=url_for('notifications.index')) }}
</div>
```
`value` is shown as given — format it first (`|toman`, `|fa`).

### badge(text, tone='neutral', icon=None) · alert(text=None, tone='info', title=None)
Tones: `neutral accent success warning danger info solid`. `{{ badge('فاکتور شد', 'success', 'check') }}`.
`alert` tones: `info success warning danger`; body via `text` or `{% call %}`. Danger/warning alerts are `role="alert"`.

### empty_state(icon, title, text=None, action=None)
```jinja
{{ empty_state('inbox', 'هنوز مراسمی ثبت نشده', 'با ثبت اولین مراسم، کارت آن این‌جا نمایش داده می‌شود.',
               action='<a class="btn btn--primary" href="/events/new">ثبت مراسم</a>'|safe) }}
```

### avatar(initials, size=None, tone=None, src=None, alt='') · rating_stars(value, max=5, count=None) · rating_input(name, value=0)
`avatar('علی عبدی')` → «ع‌ع» (first letters of two words) or the first two letters of a single word; `size`: `sm|lg|xl`.
`rating_stars(4.5, count=18)` shows half stars; `rating_input('rating', 4)` is a CSS-only 1–5 radio group.

### Forms: form_field, money_input, jdate_input, select_field, textarea_field, checkbox_field, stepper_field
All render `label` + control + help + error with correct `for`/`id`/`aria-describedby`/`aria-invalid`.
`error` may be a string or a list (a WTForms `field.errors`).

```jinja
<form method="post" action="{{ url_for('staff.save') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div class="form-grid form-grid--2">
    {{ form_field('نام و نام خانوادگی', name='full_name', value=form.full_name, required=true,
                  error=errors.get('full_name'), autocomplete='name') }}
    {{ form_field('شمارهٔ موبایل', name='mobile', type='tel', inputmode='numeric', dir='ltr', value=form.mobile) }}
    {{ select_field('نقش', 'role', Role.choices(), selected=form.role, required=true, placeholder='انتخاب کنید') }}
    {{ money_input('base_pay', form.base_pay, label='حقوق پایه', help='به تومان') }}
    {{ jdate_input('start_date', form.start_date, label='تاریخ شروع', min=today, required=true) }}
    <div class="span-all">{{ textarea_field('توضیحات', 'notes', form.notes, rows=3) }}</div>
    {{ checkbox_field('نمایش در بازارچهٔ کاریابی', 'is_public', checked=form.is_public) }}
  </div>
  <div class="form-actions"><button class="btn btn--primary" type="submit">ذخیره</button></div>
</form>
```

* `form_field(label, name, type='text', value='', help, error, required, placeholder, autocomplete, inputmode, id, input_html, attrs, disabled, readonly, dir, maxlength, min, max, step, pattern)`.
  For a custom control use a call block that receives the generated id:
  `{% call(fid) form_field('رنگ', name='x') %}<input id="{{ fid }}" …>{% endcall %}`. Extra attributes: `attrs={'data-foo': 'bar'}`.
* `money_input(name, value=None, label=None, required, help, error, id, placeholder='۰', unit='تومان', aria_label, min, max, negative, attrs)`
  — `value` is an integer. Pass `aria_label` instead of `label` inside tables.
* `jdate_input(name, value=None, label=None, min, max, required, help, error, id, aria_label, attrs)` — `value/min/max` may be
  a `date`, a `datetime` or `'YYYY-MM-DD'`. The submitted value is always ISO Gregorian.
* `select_field(label, name, options, selected=None, …, placeholder=None)` — `options` is `[(value, label), …]`
  (e.g. `Role.choices()`), or plain strings/numbers.
* `stepper_field(name, value=1, min=0, max=999, label=None, default=None, step=1, id=None, aria_label=None)`.

### modal(id, title, size='md')
Sizes `sm|md|lg|xl`. Full-screen sheet on phones, centred dialog from 640px. Put buttons in `.modal__footer` (sticky on phones).
```jinja
<button class="btn btn--primary" data-modal-open="invoice-modal">ثبت فاکتور</button>

{% call modal('invoice-modal', 'ثبت فاکتور مراسم', size='lg') %}
  <form method="post" action="{{ url_for('accounting.save_invoice', event_id=event.id) }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    …fields…
    <div class="modal__footer">
      <button type="submit" class="btn btn--primary">ثبت فاکتور</button>
      <button type="button" class="btn" data-modal-close>انصراف</button>
    </div>
  </form>
{% endcall %}
```
Esc, the X button, `data-modal-close` and a click on the backdrop close it; Tab is trapped inside; scroll is locked; focus returns to the opener.
Put modals **outside** any `<form>` that is not their own (they are positioned `fixed`, but form ownership follows the DOM).
Open from JS with `Atelier.openModal('id')`.

### accordion(id, title, open=False, meta=None, level=3) · collapse(id, open=False)
**Closed by default**; the panel slides open. Any control with `data-accordion-toggle="ID"` toggles it, so the event card's
«فاکتور» button can open the invoice that sits below the card:
```jinja
{% call card(title=event.title, actions='<button type="button" class="btn btn--secondary btn--sm" data-accordion-toggle="inv-%d" aria-expanded="false" aria-controls="inv-%d-panel">فاکتور</button>'|format(event.id, event.id)|safe) %}
  …crew badges…
  {% call accordion('inv-%d'|format(event.id), 'فاکتور مراسم', meta=('سود ' ~ (profit|toman) ~ ' تومان')) %}
    …invoice details…
  {% endcall %}
{% endcall %}
```
(`aria-expanded` on external toggles is kept in sync by `core.js`.) `data-accordion-group="name"` on several accordions
makes them exclusive. `collapse('id')` is the same panel without a header. Events: `atelier:toggle` (`detail.open`).

### confirm_button(label, action, message, title=None, confirm_label='تأیید', cancel_label='انصراف', tone='danger', fields=None, cls=None, icon=None)
A POST form (with CSRF) whose button asks first. For **every** destructive or irreversible action:
```jinja
{{ confirm_button('حذف مراسم', url_for('events.delete', event_id=event.id),
                  'این مراسم و فاکتور آن برای همیشه حذف می‌شود. ادامه می‌دهید؟',
                  title='حذف مراسم', confirm_label='بله، حذف شود', icon='trash') }}
```
Hand-written: `<button type="submit" data-confirm="مطمئنید؟" data-confirm-title="…" data-confirm-label="…">` inside any form (or on a link).
A whole `<form data-confirm="…">` also works. From JS: `if (await Atelier.confirm('…')) …` (Promise<boolean>).

### tabs(items, active=None, label=None, id='tabs') · tab_panel(key, active=False, id='tabs')
Link tabs (each item has a `url`; the server decides what is active):
```jinja
{{ tabs([{'key': 'open', 'label': 'باز', 'url': url_for('jobs.index', s='open'), 'badge': '۵'},
         {'key': 'done', 'label': 'انجام‌شده', 'url': url_for('jobs.index', s='done')}], active=status, label='فیلتر وضعیت') }}
```
Client-side tabs (no `url`): wrap the panels in a `{% call %}`; arrow keys, Home and End work (visual direction in RTL).
```jinja
{% call tabs([{'key': 'crew', 'label': 'نیروها'}, {'key': 'sales', 'label': 'فروش'}], active='crew', id='ev') %}
  {% call tab_panel('crew', active=true, id='ev') %}…{% endcall %}
  {% call tab_panel('sales', id='ev') %}…{% endcall %}
{% endcall %}
```

### pagination(page_obj, endpoint, **filters)
`page_obj` is what `db.paginate(...)` returns. Extra keyword arguments are passed to `url_for`, so the current filters survive:
```jinja
{{ pagination(events, 'events.index', q=q, status=status) }}
```
Renders nothing for a single page; on phones only «‹ current ›» is shown.

### data_table(columns, rows, empty='…', caption=None, stack=True, empty_icon='inbox')
```jinja
{% set columns = [
  {'key': 'title',  'label': 'مراسم'},
  {'key': 'date',   'label': 'تاریخ',  'format': 'jdate'},
  {'key': 'income', 'label': 'درآمد',  'format': 'toman', 'num': True},
  {'key': 'status', 'label': 'وضعیت'},
  {'key': None,     'label': 'عملیات', 'actions': True},
] %}
{% call(row, col) data_table(columns, events, caption='مراسم‌های ماه', empty='مراسمی ثبت نشده است.') %}
  {% if col.key == 'status' %}{{ badge(row.status.label, 'success') }}
  {% elif col.actions %}<a class="btn btn--sm btn--secondary" href="{{ url_for('events.show', event_id=row.id) }}">مشاهده</a>
  {% endif %}
{% endcall %}
```
* `format`: `toman` (adds «تومان»), `jdate`, `jdatetime`, `jweekday`, `fa`. `None` values show «—».
* A column your call block renders nothing for falls back to the default cell.
* **Phones (< 640px): each row becomes a labelled card** (the label comes from `data-label`). Hand-written tables:
  `<table class="table table--stack"><td data-label="درآمد">…</td></table>` inside `<div class="table-wrap">`; use `class="cell-num"` for numeric cells.

### calendar_month(grid, events=None, title=None, prev_url=None, next_url=None, today=None)
```python
# view
from app.services import jalali
grid = jalali.month_grid(1404, 5)                          # weeks of 7 cells, Saturday first; date or None
events = {event.date: [{'label': event.title, 'url': url_for('events.show', event_id=event.id), 'tone': 'success'}]}
```
```jinja
{{ calendar_month(grid, events, title='مرداد ۱۴۰۴', prev_url=prev, next_url=next, today=today) }}
```
`events` is keyed by `date` or `'YYYY-MM-DD'`; `tone`: `accent success warning danger info neutral`. Wide screens show event chips (3 per day plus
«+n مورد دیگر»); phones show dots and an agenda list below the grid. Friday is the red weekend column.

### error_page(code, title, text=None, icon='camera', actions=None)
Used by `errors/403|404|500.html`; reuse it for other status pages.

## 6. Declarative widgets (no JS to write)

`core.js` finds these attributes at load. For HTML you add later, call `Atelier.init(container)`
(clone from a `<template>`, not from an already-initialised node — though clones are tolerated).

| markup | result |
|--------|--------|
| `<input data-money name="amount" value="1870000">` (or `money_input`) | Live 3-digit grouping with Persian digits (`۱٬۸۷۰٬۰۰۰`), numeric keyboard, caret-safe editing, paste of `۱,۸۷۰,۰۰۰ تومان` works. A **hidden input with the original name carries the raw integer** (`1870000`); the visible field has no name. Optional `data-min`, `data-max` (native validation message), `data-negative`. |
| `<input data-jdate name="date" value="2025-07-17">` (or `jdate_input`) | Shows `۱۴۰۴/۰۴/۲۶`; a picker opens on click / ↓ / the calendar button (popover on desktop, bottom sheet on phones). The **ISO date is submitted** in a hidden input with the original name. Typing a Jalali date in any digits works; invalid text blocks the form with a Persian message. `data-min`/`data-max` (ISO), `data-years-back`/`data-years-ahead`. Arrow keys move by day/week (RTL aware), PageUp/PageDown by month, Esc closes. |
| `<div class="stepper" data-stepper data-min="1" data-max="20" data-default="1"><button data-stepper-dec>…<input name="weight" value="1">…<button data-stepper-inc>…</div>` or a bare `<input data-stepper min="1" max="20" value="1">` (or `stepper_field`) | Number with − / + buttons (press and hold repeats), arrow keys, clamping, Persian digits. Hidden raw value like money. The invoice popup's «تعداد محاسبه برای حقوق نیرو» is exactly this (default 1). |
| `<button data-modal-open="id">` / `data-modal-close` | open / close the `.modal` (see `modal`) |
| `<button data-accordion-toggle="id">` | toggle an `.accordion` or `.collapse` |
| `<button data-confirm="message">` | ask before the click proceeds |
| `<button data-theme-toggle>` | switch light/dark (remembered in `localStorage`) |
| `<button data-history-back>` | `history.back()` |
| `<button data-print>` | `window.print()` (add `no-print` to hide the button on paper) |

Events (bubbling `CustomEvent`s): `atelier:input` (`detail.name`, `detail.value` — an integer for money/stepper,
an ISO string for dates), `atelier:modal-open` / `atelier:modal-close` (`detail.id`), `atelier:toggle`, `atelier:tab`, `atelier:ready`.
Prefer `atelier:input` over `input`/`change` for money, stepper and date fields (the visible text is formatted).

## 7. JavaScript API (`window.Atelier`)

```js
// HTTP — adds X-CSRFToken (same-origin only), sends/receives JSON, throws AtelierError with a Persian message
const data = await Atelier.fetchJSON('/api/events/3/status', {method: 'POST', json: {status: 'done'}});
try { await Atelier.fetchJSON(url, {method: 'DELETE'}); }
catch (err) { Atelier.toast(err.message, 'error'); /* err.status (0 = network), err.data = parsed body */ }

Atelier.toast('ذخیره شد', 'success');            // success | error | warning | info; opts {timeout: ms, 0 = sticky}
Atelier.openModal('invoice-modal'); Atelier.closeModal('invoice-modal');
if (await Atelier.confirm('حذف شود؟', {title: 'حذف', confirmLabel: 'بله', tone: 'danger'})) { … }

Atelier.toFaDigits('1234');                      // '۱۲۳۴'
Atelier.toLatinDigits('۱٬۲۳۴');                  // '1,234'  (also Arabic-Indic digits; ٬ ، -> ,)
Atelier.formatToman(1870000);                    // '۱٬۸۷۰٬۰۰۰'   (Persian digits by default)
Atelier.formatToman(1870000, {fa: false, suffix: true});   // '1,870,000 تومان'
Atelier.parseMoney('۱٬۸۷۰٬۰۰۰ تومان');           // 1870000 — blank -> 0, invalid -> null (never a float)

Atelier.jalali.toJalali(2025, 7, 17);            // [1404, 4, 26]  (also .jy/.jm/.jd)
Atelier.jalali.toGregorian(1404, 4, 26);         // [2025, 7, 17]  (also .gy/.gm/.gd)
Atelier.jalali.monthLength(1403, 12);            // 30
Atelier.jalali.isLeap(1403);                     // true
// more: weekdayIndex(gy,gm,gd) (Saturday=0), todayJalali(), format(jy,jm,jd,'long',{fa:true}), parse('۱۴۰۴/۰۴/۲۶'), monthGrid(jy,jm)

Atelier.init(containerEl);                       // initialise widgets inside HTML you inserted
Atelier.setValue('#amount', 1870000);            // money / stepper / jdate by element, selector, id or field name
Atelier.getValue('amount');                      // 1870000 (money, stepper) | '2025-07-17' (jdate) | null
Atelier.toggleCollapse('inv-12');                // or (id, true|false)
Atelier.theme.set('dark' | 'light' | 'auto');
```
`jalali.js` and `money.js` are plain scripts that also run under node. They are tested against the Python services
(`app/services/jalali.py`, `app/services/money.py`) on every date of Jalali years 1300–1500, every leap year and Nowruz 1–3000, and
a table of tricky money strings (`tests/test_jalali_js_parity.py`) — so the browser and the server always agree.

**Money rule:** amounts are integers (Toman). Never `parseFloat` a money field; use `Atelier.getValue` / `parseMoney`.
**Date rule:** the database and every request carry ISO Gregorian dates; the screen shows Jalali. Convert only for display.

## 8. Recipes

### Event card with a hidden invoice (AGENTS.md "کارت مراسم")
See the accordion example in section 5. The pattern: the card shows name, date and crew by role; the «فاکتور» button
(`data-accordion-toggle`) opens the invoice that is **closed by default**; «ثبت فاکتور» opens `modal('invoice-…')`.

### Invoice popup rows with «+ ابعاد دیگر»
```jinja
<div id="other-sizes" class="stack stack--sm"></div>
<button type="button" class="btn btn--sm" id="add-size">{{ icon('plus') }}ابعاد دیگر</button>

<template id="size-row">
  <div class="card card--flat"><div class="card__body"><div class="form-grid form-grid--3">
    <input class="input" name="size_label[]" placeholder="مثلاً ۵۰×۷۰" aria-label="ابعاد">
    <div class="stepper" data-stepper data-min="0" data-max="500" data-default="1">
      <button type="button" class="stepper__btn" data-stepper-dec aria-label="کاهش">−</button>
      <input class="stepper__input" name="size_qty[]" value="1" aria-label="تعداد فروش">
      <button type="button" class="stepper__btn" data-stepper-inc aria-label="افزایش">+</button>
    </div>
    <div class="stepper" data-stepper data-min="0" data-max="50" data-default="1">…<input name="size_weight[]" value="1" aria-label="تعداد محاسبه برای حقوق نیرو">…</div>
  </div></div></div>
</template>
```
```js
document.getElementById('add-size').addEventListener('click', () => {
  const row = document.getElementById('size-row').content.firstElementChild.cloneNode(true);
  document.getElementById('other-sizes').appendChild(row);
  Atelier.init(row);                                  // makes the steppers live
});
```
Live totals: listen to `atelier:input` on the form and read values with `Atelier.getValue(name)`; display with `Atelier.formatToman(total)`.
(The real totals are always recomputed on the server.)

### List page with filters and pagination
```jinja
{{ page_header('نیروها', actions='<a class="btn btn--primary" href="…">دعوت نیرو</a>'|safe) }}
<form class="cluster" method="get" role="search">
  {{ form_field('جستجو', name='q', value=q, type='search') }}
  {{ select_field('نقش', 'skill', Skill.choices(), selected=skill, placeholder='همه') }}
  <button class="btn" type="submit">{{ icon('search') }}جستجو</button>
</form>
{% call(row, col) data_table(columns, page.items, empty='نیرویی پیدا نشد.') %}…{% endcall %}
{{ pagination(page, 'staff.index', q=q, skill=skill) }}
```

### Saving through AJAX with feedback
```js
button.addEventListener('click', async () => {
  button.classList.add('is-loading');
  try {
    const res = await Atelier.fetchJSON(`/api/assignments/${id}/accept`, {method: 'POST'});
    Atelier.toast('پذیرفته شد', 'success');
  } catch (err) {
    Atelier.toast(err.message, 'error');
  } finally {
    button.classList.remove('is-loading');
  }
});
```
Flask `flash('…', 'success'|'error'|'warning'|'info')` is shown as a toast automatically after the redirect.

## 9. Printing and invoices

`app/static/css/print.css` (loaded with `media="print"`) hides all chrome (sidebar, bars, buttons, modals, toasts),
forces black-on-white even from dark mode, sets `@page { size: A4; margin: 12mm }`, repeats table headers and never splits a row.
Helpers: `.no-print`, `.print-only`, `.print-break` (page break before), `.collapse--print-open` (an accordion panel that must print expanded).

Use the `.sheet` block for an invoice or receipt (a ready-made, A4-friendly document layout):

```html
<article class="sheet">
  <div class="sheet__header"><div><div class="sheet__title">فاکتور مراسم</div><small>اتلیه ستاره</small></div><div>شمارهٔ {{ n|fa }}</div></div>
  <dl class="sheet__meta"><div><dt>تاریخ</dt><dd>{{ event.date|jdate }}</dd></div>…</dl>
  <table class="sheet__table"><thead><tr><th>محصول</th><th class="cell-num">تعداد</th></tr></thead>…</table>
  <div class="sheet__totals">
    <div><span>کارتخوان</span><b>{{ card|toman }}</b></div>
    <div class="is-total"><span>سود مراسم</span><b>{{ profit|toman }} تومان</b></div>
  </div>
  <div class="sheet__sign"><div>امضای مدیر اتلیه</div><div>امضای مدیر تالار</div></div>
</article>
<button type="button" class="btn no-print" data-print>{{ icon('printer') }}چاپ</button>
```
Wrap negative amounts in `.num` (`<b class="num">−{{ x|toman }}</b>`) so the minus sign stays on the left.

## 10. RTL, Persian digits and dates

* The document is `lang="fa" dir="rtl"`. Write **logical** CSS (`margin-inline-start`, `padding-inline`, `inset-inline-end`,
  `text-align: start|end`, `border-start-start-radius`), never `left`/`right`. The only physical values in the system are the
  deliberate right-aligned LTR number inputs and the slide-in transforms of the drawer.
* Persian is shown with Persian digits via the `fa`, `toman`, `jdate`, `jdatetime`, `jweekday` filters. Inputs accept Persian,
  Arabic and Latin digits; the server normalises with `app.services.money.normalize_digits`.
* Phone numbers, codes and amounts with signs go in `.num` (left-to-right isolate). Latin words inside Persian text are fine.
* Icons that imply direction: «next» points **left**, «back» points **right** (`chevron-left`, `arrow-right`).
* Never call `.toLocaleString()` on money; use `Atelier.formatToman`.

## 11. Accessibility checklist

- [ ] One `<h1>` per page (`page_header`); headings go down one level at a time.
- [ ] Every control has a label (`form_field` and friends do it for you); icon-only buttons have `aria-label`.
- [ ] Tap targets ≥ 44×44px (`.btn`, `.icon-btn`, nav items and inputs already are); keep ≥ 8px between adjacent targets.
- [ ] Never remove the focus ring; never convey state by colour alone (badges carry text, errors carry an icon and text).
- [ ] Use `badge`/`alert` tones from the system (they are AA-checked), not custom colours.
- [ ] Motion: respect `prefers-reduced-motion` (the system does; do not add JS-driven animation without checking `matchMedia`).
- [ ] Destructive actions go through `confirm_button`.
- [ ] Test with the keyboard only, and at 390px wide with 200% text size.

## 12. PWA and the service worker

* `app/static/manifest.webmanifest` (name, `dir: rtl`, `theme_color`, SVG + PNG icons incl. maskable) and `apple-touch-icon.png`.
* `app/static/js/sw.js` caches **only** `/static/*` GET requests (fonts and images cache-first, CSS/JS network-first with an offline
  fallback). It never touches HTML pages, JSON/API calls or any non-GET request, so nobody sees stale data or a replayed POST.
  Bump `CACHE_VERSION` when the precache list changes.
* A worker only controls pages inside its scope. The file lives at `/static/js/sw.js`, so serve it with the header
  **`Service-Worker-Allowed: /`** (e.g. in an `after_request` hook for that path) or expose it at `/sw.js` and set `sw_url`.
  Without it the registration fails quietly and the site works exactly as before, just without the asset cache.
* Registration happens only on HTTPS or `localhost`.

## 13. Content Security Policy

The design system emits **no inline `<script>`, no `<style>`, no `style=""` and no `on…=` handlers**, loads nothing from other
origins, and sets dynamic geometry through the CSSOM only. A strict policy works:
`default-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'`.
Keep modules inline-free too: put behaviour in a `.js` file and use `data-*` hooks. (`tests/test_ui_components.py` fails if base
markup gains inline script/style.)

## 14. Testing

```bash
python -m pytest tests/test_ui_components.py tests/test_ui_contrast.py tests/test_jalali_js_parity.py   # no browser needed (node for the parity file)
python -m pytest tests/test_ui_browser.py                                                               # real Chromium
node --test tests/js/unit.test.mjs                                                                      # JS helpers alone
python docs/styleguide/build.py                                                                         # regenerate docs/styleguide.html
```

* `test_ui_components.py` renders `base.html`, every macro, the error pages and the landing page in a tiny Flask app and checks
  structure, escaping, CSRF fields, closed-by-default accordions, calendar columns, no inline styles/scripts, manifest and icons.
* `test_ui_contrast.py` parses the real tokens and enforces WCAG AA in light and dark, and keeps the two dark blocks identical.
* `test_jalali_js_parity.py` compares `jalali.js`/`money.js` with the Python services (skipped with a reason if node is missing).
* `test_ui_browser.py` drives Chromium through Playwright: money caret behaviour, the Jalali picker (Saturday-first grid, leap years,
  keyboard), modal focus trap and scroll lock, accordion, confirm → POST with CSRF, stepper, fetchJSON errors, service worker caching only
  static files, theme, drawer, no horizontal scroll at 390px/1280px, 44px tap targets, stacked tables, print and reduced motion.
  It needs `pip install playwright` (the wheel only — do **not** run `playwright install`) and a Chromium binary; it looks in
  `$CHROMIUM_BIN`, `/opt/pw-browsers/chromium-*/` and `~/.cache/ms-playwright/chromium-*/`, and skips with a reason otherwise.
  Use `page.evaluate("() => { … }")` for calls returning a Promise you do not want awaited (e.g. `Atelier.confirm`).

Manual visual check: open `docs/styleguide.html`, toggle the theme, narrow the window to 390px.

## 15. Changing the design system

1. Components live in `app/templates/components/ui.html` (macros) and `app/static/css/app.css` (sections are numbered). Add the macro,
   its CSS and a block in `docs/styleguide/styleguide_page.html`; add a test in `tests/test_ui_components.py`.
2. New colours = new **tokens** in both dark blocks (the contrast test will tell you if a pair fails AA).
3. New behaviour = a `data-*` hook handled by delegation in `core.js` (works for HTML inserted later) and, if it builds DOM,
   an `init` function so `Atelier.init(root)` covers it. Keep the syntax ES2015-level (cheap Android WebViews); no `?.`, `??`, `async`.
4. Money is an integer and dates are ISO until display. `jalali.js`/`money.js` must keep matching the Python services: change both together.
5. Bump `CACHE_VERSION` in `sw.js` when you rename/add precached files, and `asset_version` in the app config for each release.
