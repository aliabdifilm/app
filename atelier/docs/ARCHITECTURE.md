# Architecture and module contract — Atelier Sayyar (اتلیه سیار)

This document is **the contract for module authors**. Seven feature modules are built concurrently
against it (`accounting`, `ateliers`, `halls`, `marketplace`, `staff`, `inventory`, `admin`); if you
follow it, they plug into the shell without touching each other's files.

Business rules live in `/AGENTS.md` (roles, pay formula, accounting, print rules, assumptions A1–A8).
The relational model is in `docs/DATA_MODEL.md`, the design system in `docs/UI_GUIDE.md`.
This file says **how the application is put together and how a module hooks in**.

Contents: [1 Overview](#1-overview) · [2 The module contract](#2-the-module-contract) ·
[3 URLs](#3-url-prefixes-and-reserved-urls) · [4 Permissions](#4-permissions-role--ownership) ·
[5 Requests and transactions](#5-requests-forms-and-transactions) · [6 JSON and CSRF](#6-json-csrf-and-fetchjson) ·
[7 Lists and pagination](#7-lists-and-pagination) · [8 Flash, notify, audit, event bus](#8-flash-notifications-audit-and-the-event-bus) ·
[9 Templates and assets](#9-templates-assets-and-csp) · [10 Uploads](#10-uploads) ·
[11 Tests](#11-tests) · [12 Persian copy](#12-persian-copy-guidelines) · [13 Authentication](#13-authentication-assumption-a6) ·
[14 Configuration](#14-configuration-and-deployment) · [15 CLI](#15-command-line) · [16 Checklist](#16-module-author-checklist) ·
[17 Limits and open questions](#17-known-limits-and-open-questions)

---

## 1. Overview

```
atelier/
├── wsgi.py                      flask --app wsgi …  /  gunicorn wsgi:app
├── app/
│   ├── __init__.py              create_app(): config, extensions, filters, context, headers, modules, CLI
│   ├── config.py                Development / Testing / Production + environment variables
│   ├── extensions.py            db, login_manager, csrf, get_state()
│   ├── cli.py                   init-db, create-admin, seed-demo, routes-matrix
│   ├── models/                  SQLAlchemy models (docs/DATA_MODEL.md)
│   ├── services/                framework-light logic shared by modules
│   │   ├── constants.py         enums + Persian labels (Role, Skill, EventStatus …)
│   │   ├── jalali.py money.py payroll.py print_yield.py     pure Python (no Flask / SQLAlchemy)
│   │   ├── staffing.py notifications.py audit.py eventbus.py     domain services (never commit)
│   │   ├── permissions.py       role_required, ownership helpers (404 for foreign objects)
│   │   ├── nav.py               menu / home / onboarding registry
│   │   ├── forms.py             Form: parse plain HTML forms (digits, Toman, Jalali dates)
│   │   ├── http.py              json_ok / json_error / wants_json / paginate
│   │   ├── security.py          safe redirects, rate limiter, passwords, clock, CSP headers
│   │   ├── sms.py               console + Kavenegar providers
│   │   ├── uploads.py           safe image upload + visibility for /media
│   │   └── testing.py           login_client, FrozenClock, IsolatedClient (tests / QA only)
│   ├── modules/                 one package per feature (auto-discovered)
│   │   ├── main/                landing, /dashboard, /notifications, /healthz, /media, PWA files, error pages
│   │   └── auth/                mobile + OTP sign-in, registration, password, logout
│   ├── templates/               base.html, components/ui.html, <module>/…, errors/…
│   └── static/                  css/ js/ img/ fonts/   (no CDN, no build step)
├── tests/                       conftest.py (fixtures), test_<module>*.py
└── docs/
```

Stack: Python 3.11+, Flask 3 application factory, Flask-SQLAlchemy 3 / SQLAlchemy 2.x, Flask-Login,
Flask-WTF **for CSRF only** (forms are plain HTML + Jinja), SQLite in development, PostgreSQL-portable,
server-rendered Jinja + vanilla JS, everything self-contained (runs offline on a cheap VPS).

**Layers.** Routes (`modules/*/routes.py`) parse input, call permission helpers, call services,
commit, and render. Services (`app/services`) hold the rules; the ones that touch the database
`add` + `flush` and **never `commit`** — the route owns the transaction. Models hold data and
validation. Templates only present.

**Per-app state.** Anything that must not leak between two apps (tests, `routes-matrix`) lives in
`app.extensions["atelier"]` (`get_state()`): the SMS provider, the rate limiter, the clock, the list
of loaded modules and their import errors.

---

## 2. The module contract

A module is a package `app/modules/<name>/`. `create_app()` imports every such package
(`main`, `auth`, then alphabetical, `admin` last) and registers it. Nothing is registered by hand.

### Layout

```
app/modules/halls/
├── __init__.py       bp = Blueprint("halls", __name__, url_prefix="/hall"); nav registration; imports routes
├── routes.py         the views (@bp.route …)
├── services.py       optional: module logic that is more than a few lines (no request objects in it)
└── forms.py          optional: parsing/validation helpers built on app.services.forms.Form
app/templates/halls/            templates of this module (extend base.html)
app/static/css/halls.css        optional, tokens only
app/static/js/halls.js          optional, ES2015, no inline script
tests/test_halls.py             tests (test_halls_<topic>.py for more files)
```

### `__init__.py`

```python
"""``halls`` - halls (تالار) and their events."""

from flask import Blueprint

from app.services import eventbus, nav
from app.services.constants import Role

bp = Blueprint("halls", __name__, url_prefix="/hall")      # name == package name (enforced)

# menu entries of this module: (roles, label, endpoint, icon, order)
nav.register(Role.HALL_MANAGER, "تقویم مراسم‌ها", "halls.calendar", icon="calendar", order=20)
nav.register(Role.HALL_MANAGER, "تالارهای من", "halls.index", icon="building", order=30)
nav.register_home(Role.HALL_MANAGER, "halls.calendar")                 # where /dashboard sends this role
nav.register_onboarding(Role.HALL_MANAGER, "halls.setup",              # "create your hall" step
                        is_complete=lambda user: bool(user.halls))

from app.modules.halls import routes  # noqa: E402,F401   (attach the routes to bp - keep at the bottom)


def register(app):               # optional hook, runs after the blueprint is registered
    eventbus.subscribe("invoice.saved", on_invoice_saved)   # event-bus subscriptions, CLI commands, filters ...
```

Rules:

* `bp.name` **must equal the package name**; endpoints are therefore `halls.calendar`, which is also how
  `url_for`, `nav.register` and the `routes-matrix` report them. A mismatch fails the module load.
* Routes are attached when `routes.py` is imported (bottom of `__init__.py`). Do not add routes to a
  blueprint after import time.
* **Import-time registration is global** (nav, event-bus subscribers): registering the same endpoint /
  function twice is harmless (`nav.register` replaces, `eventbus.subscribe` is idempotent).
* A module may depend on `app.models`, `app.services.*` and the **public helpers** of other modules'
  services; never import another module's `routes.py`. To react to what another module did, use the
  event bus (section 8).
* A module that raises on import aborts startup in development/production (`MODULES_STRICT`).
  Tests record the traceback instead; `tests/test_shell_modules.py` fails with it.
* Disable a module on a deployment with `DISABLED_MODULES=inventory,marketplace`.

### Navigation (`app.services.nav`)

| call | purpose |
|---|---|
| `nav.register(roles, label, endpoint, icon="list", order=100, badge=None, values=None, active=())` | one sidebar / tab-bar entry. `roles`: a `Role`, several, or `None` (every signed-in user). `badge(user)` returns an int or short string (`0`/`None`/`''` hides it; keep it a cheap query). `values` are `url_for` kwargs. `active=("event_*",)` lights the entry up for extra endpoints. |
| `nav.register_home(roles, endpoint)` | where `/dashboard` redirects the role (first registration per role wins) |
| `nav.register_onboarding(roles, endpoint, is_complete)` | the "complete your profile" page of the role; `is_complete(user) -> bool` |

Conventions: **order** 10 home, 20–60 daily work (calendar, events, staff, accounting), 70–80 marketplace
and inventory, 90+ settings; at most ~6 entries per role (phones show the first four plus "منو").
Icons are names from the sprite (`docs/styleguide.html#icons`). The shell computes `active`:
an endpoint match wins, else the entry with the longest URL prefix of the current path.

The context processor puts `nav_items`, `unread_count`, `platform_name`, `today`, `asset_version` and the
auth/notification URLs in every template (`docs/UI_GUIDE.md` section 2) — you never pass them.

Onboarding: the user is sent to the first unfinished step right after registration; `/dashboard` shows a
banner while it is incomplete; `get_my_atelier()` / `get_my_hall()` raise `OnboardingRequired` when there is
nothing yet, which the app turns into a redirect to that page (flash «ابتدا اطلاعات اتلیهٔ خود را ثبت کنید.»).
**The onboarding page itself must use `required=False`** (otherwise it redirects to itself) and the
decorator `@role_required(…, allow_pending=True)` if pending accounts should reach it.

---

## 3. URL prefixes and reserved URLs

| module | blueprint | prefix | owner of |
|---|---|---|---|
| `main` | `main` | — | `/`, `/dashboard`, `/notifications`, `/notifications/read`, `/healthz`, `/media/<path>`, `/manifest.webmanifest`, `/sw.js`, `/robots.txt`, `/favicon.ico`, error handlers |
| `auth` | `auth` | `/auth` | login, verify, register, password, logout, account, pending |
| `accounting` | `accounting` | `/accounting` | invoices (فاکتور), payroll sheet, reports |
| `ateliers` | `ateliers` | `/atelier` | the atelier manager's panel: calendar, events, afish (staffing), roster, settings |
| `halls` | `halls` | `/hall` | the hall manager's panel: halls, events, offers |
| `marketplace` | `marketplace` | none (routes start with `/jobs` and `/talent`) | job posts, applications, talent search |
| `staff` | `staff` | `/me` | the staff member's panel: profile, availability, my afish, earnings |
| `inventory` | `inventory` | `/inventory` | paper/ink/frames stock, equipment |
| `admin` | `admin` | `/admin` | site admin panel (users, approvals, announcements, settings, audit log) |

Do not define URLs outside your prefix. `/static`, `/media` and the URLs above are reserved. API-style JSON
endpoints live under your own prefix (`/hall/api/...`); a path starting with `/api/` is always answered
with JSON errors.

---

## 4. Permissions: role + ownership

Every protected view needs **both**:

1. **Role** — *who may call the route at all*.
2. **Ownership** — *which rows they may touch*. A role is never enough: an atelier manager must not be
   able to open another atelier's event by editing the id in the URL.

```python
from app.services.permissions import (
    role_required, login_required, admin_required,                 # decorators
    get_my_atelier, get_my_hall, get_my_ateliers, get_my_halls,    # "the current user's X" (+ set_current_atelier)
    hall_or_404, atelier_or_404, event_or_404, assignment_or_404,  # object loaders (404 for foreign objects)
    roster_member_or_404, invoice_or_404, stock_item_or_404,
    equipment_or_404, job_post_or_404, job_application_or_404,
    owned_or_404,                                                  # generic: you supply the ownership rule
    my_atelier_ids, my_hall_ids, scope_to_my_ateliers, scope_to_my_halls,   # scoping list queries
    event_access, is_admin, OnboardingRequired,
)
```

### Decorators

```python
@bp.route("/events")
@role_required(Role.ATELIER_MANAGER)                       # one role
def events(): ...

@bp.route("/events/<int:event_id>")
@role_required(Role.ATELIER_MANAGER, Role.HALL_MANAGER)    # several roles
def event(event_id): ...

@bp.route("/setup", methods=["GET", "POST"])
@role_required(Role.ATELIER_MANAGER, allow_pending=True)   # onboarding / profile pages
def setup(): ...
```

| situation | response |
|---|---|
| anonymous | redirect to `/auth/login?next=<this page>` (JSON / XHR callers: **401** `{"ok": false, "code": "unauthorized"}`) |
| signed in with the wrong role | **403** page (JSON: 403 body) |
| right role but account `pending` approval (and not `allow_pending`) | redirect to `/auth/pending` (JSON: 403 `pending_approval`) |
| suspended account | treated as anonymous (the session is dropped on the next request) |

`role_required()` needs at least one role. **Admins are not implicitly allowed**: list `Role.ADMIN` where they
may enter. `login_required` = any role; `admin_required` = `role_required(Role.ADMIN)`.

### Ownership helpers: always load objects through them

They return the object, or `abort(404)` — the same answer for "does not exist" and "belongs to somebody else",
so ids cannot be probed.

```python
@bp.route("/events/<int:event_id>/afish", methods=["GET", "POST"])
@role_required(Role.ATELIER_MANAGER)
def afish(event_id):
    event = event_or_404(event_id, as_=("atelier",))            # my atelier's event only
    ...

@bp.route("/roster/<int:member_id>/delete", methods=["POST"])
@role_required(Role.ATELIER_MANAGER)
def roster_delete(member_id):
    member = roster_member_or_404(member_id)                    # a member of one of my ateliers
    ...

@bp.route("/me/afish/<int:assignment_id>/accept", methods=["POST"])
@role_required(Role.STAFF)
def accept(assignment_id):
    assignment = assignment_or_404(assignment_id, as_=("assignee",))   # my own assignment
    ...

widget = owned_or_404(Widget, widget_id,                        # anything else: write the rule
                      lambda w, user: w.atelier_id in my_atelier_ids(user))
```

Capacities for `event_or_404(as_=…)`: `"hall"` (I manage its hall), `"atelier"` (my atelier works it),
`"assignee"` (I am, or was invited as, staff on it — directly or through a roster entry linked to my account),
`"offered"` (it is open on the marketplace or offered to my atelier). **Choose the narrowest set the route needs**
(editing a ceremony: `("hall",)`; staffing it: `("atelier",)`; "my day": `("assignee",)`). Cancelled
assignments give no access. `assignment_or_404(as_=…)` knows `"atelier"`, `"assignee"`, `"hall"`;
`job_application_or_404` knows `"applicant"`, `"atelier"`. `allow_admin=True` lets site admins through.
`owned_or_404(model, pk, owner_check)` requires the check (there is no "allow everything" default).

`invoice_or_404` is atelier-only on purpose: hall managers and staff never see the books.

### The current user's atelier / hall

An atelier manager may own several ateliers (`unique(manager_id, name)`).

```python
atelier = get_my_atelier()                  # the one picked with set_current_atelier(), else my first; OnboardingRequired if none
atelier = get_my_atelier(atelier_id)        # that one, or 404 if it is not mine
atelier = get_my_atelier(required=False)    # None when I have none (onboarding pages)
set_current_atelier(atelier)                # remembers the choice in the session (404 if not mine)
```

`get_my_hall()` works the same (`session["hall_id"]` is read, a hall switcher would set it).

### Scoping lists — never filter in Python

```python
stmt = select(Event).order_by(Event.date)
stmt = scope_to_my_ateliers(stmt, Event.atelier_id)         # WHERE atelier_id IN (my ateliers)
# or: stmt.where(Event.atelier_id.in_(my_atelier_ids()))
```

`my_atelier_ids()` / `my_hall_ids()` are cached per request. `staffing.conflicts_for()` can return
assignments of **other** ateliers: show them as "مشغول" without names or details.

### Other rules

* State changes are **POST** (never GET), CSRF-protected, and destructive ones go through `confirm_button`.
* Never trust an id from a form body either: re-load it through a helper and check it belongs to the same atelier/event
  (`roster_member_or_404(request.form["member_id"])` plus `member.atelier_id == event.atelier_id`).
* Admin views use `@role_required(Role.ADMIN)`; admin actions are audited.
* `routes-matrix` (section 15) prints every route × role status so QA can spot a route that is open to the wrong role.

---

## 5. Requests, forms and transactions

**The route owns the transaction.** Services `add` + `flush`; the route calls `db.session.commit()` once at the end
of a successful POST. Anything not committed is rolled back when the request ends (a forgotten `commit()` silently
loses data — the test client reproduces this, section 11).

**Post → Redirect → Get.** After a successful POST: `flash(...)`, `commit`, `return redirect(url_for(...))`.
For an invalid POST: re-render the same page with the submitted values and the errors, status **422**.

```python
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Hall
from app.modules.halls import bp
from app.services import audit
from app.services.constants import Role
from app.services.forms import Form
from app.services.permissions import get_user, hall_or_404, role_required


@bp.route("/new", methods=["GET", "POST"])
@role_required(Role.HALL_MANAGER)
def hall_new():
    if request.method == "GET":
        return render_template("halls/form.html", form={}, errors={})

    form = Form(request.form)                                       # digits, Toman, Jalali dates, Persian messages
    name = form.text("name", "نام تالار", required=True, max_len=150)
    city = form.text("city", "شهر", required=True, max_len=80)
    rent = form.money("rent", "اجارهٔ پیش‌فرض")                      # integer Toman or None
    if not form.ok:
        return render_template("halls/form.html", **form.context()), 422

    hall = Hall(manager=get_user(), name=name, city=city)
    db.session.add(hall)
    try:
        db.session.flush()
    except IntegrityError:                                          # unique (manager, name)
        db.session.rollback()
        form.error("name", "تالاری با این نام قبلاً ثبت کرده‌اید.")
        return render_template("halls/form.html", **form.context()), 422
    audit.log(get_user(), "hall.created", hall, {"name": name})
    db.session.commit()
    flash("تالار ذخیره شد.", "success")
    return redirect(url_for("halls.hall_show", hall_id=hall.id))
```

### `app.services.forms.Form`

`Form(request.form)` reads plain HTML forms. Getters return clean Python values (`None` / the default for an empty
optional field; after an error the message is recorded and the next fields can still be read):

| getter | accepts | returns |
|---|---|---|
| `text(name, label, required, min_len, max_len, default, digits, multiline)` | trims, drops bidi marks (keeps ZWNJ), Arabic ی/ک → Persian | `str` |
| `integer(name, label, required, min, max, default)` | `۵`, `5`, `1,234` | `int` |
| `money(name, label, required, min, max, default)` | `۱٬۸۷۰٬۰۰۰`, `1,870,000 تومان`, `1870000`; **never floats** | `int` Toman |
| `date(name, label, required, min, max)` | ISO `2025-07-17` (what `jdate_input` posts) or Jalali `۱۴۰۴/۰۴/۲۶` | `datetime.date` |
| `time(name, label, required)` | `۱۸:۳۰` | `datetime.time` |
| `choice(name, label, options, required, default)` / `enum(name, label, EnumCls, …)` | one of the options (compared as strings) | the option / enum member |
| `boolean(name)` | checkbox (`1`, `on`, `true`) | `bool` |
| `mobile(name, label, required)` | any Iranian format | `09xxxxxxxxx` |
| `getlist(name)`, `int_list(name)` | repeated fields (`name[]` rows, multi-selects) | `list` |

`form.ok`, `form.errors` (`{field: Persian message}`), `form.values` (to redisplay), `form.error(name, msg)`,
`form.context()` → `{"form": values, "errors": errors}`. The templates use
`form_field(…, value=form.get('name', ''), error=errors.get('name'))` (`docs/UI_GUIDE.md` section 5).

Money rule: **Toman are integers everywhere** (columns `BIGINT`); display with `|toman`, parse with `form.money` /
`app.services.money.parse_toman`. Date rule: stored Gregorian (UTC timestamps), shown Jalali with `|jdate`,
`|jdatetime`, `|jweekday`.

### Time

Use `from app.services import security` → `security.now()` (naive UTC, the storage convention) and
`security.today()` (today in Iran time) instead of `datetime.utcnow()` / `date.today()`: tests freeze them with the
`clock` fixture. Model `created_at` defaults use the real clock; set explicit timestamps when a test depends on them.

---

## 6. JSON, CSRF and `fetchJSON`

### CSRF

Flask-WTF `CSRFProtect` is on for every `POST/PUT/PATCH/DELETE`:

* HTML forms: `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` (the form macros and
  `confirm_button` already include it).
* JavaScript: `Atelier.fetchJSON(url, {method: "POST", json: {...}})` reads `<meta name="csrf-token">`
  (present in `base.html`) and sends `X-CSRFToken` — you write nothing.
* A missing/invalid token → **400** (HTML: friendly page «اعتبار این فرم تمام شده است…», JSON: error body).
  Never use `@csrf.exempt`.

### JSON shape

```python
from app.services.http import json_ok, json_error, json_body

return json_ok({"id": event.id, "status": event.status.value})           # {"ok": true,  "data": {...}}              200
return json_ok(item, status=201)
return json_error("تاریخ نامعتبر است.", 422, fields={"date": "نامعتبر"})   # {"ok": false, "error": "...", "code": "validation_error", "fields": {...}}
return json_error("این مراسم قبلاً فاکتور شده است.", 409, code="already_invoiced")
```

`error` is always a **plain Persian string** (that is what `Atelier.fetchJSON` shows as the toast; objects are
ignored). `code` is a stable machine word (defaults per status: `bad_request`, `unauthorized`, `forbidden`,
`not_found`, `conflict`, `validation_error`, `rate_limited`, `server_error`). Use `json_body()` for the parsed
request object. `wants_json()` is true for `/api/` paths, JSON bodies, `X-Requested-With: XMLHttpRequest`
and `Accept: application/json`; the shell's error handlers, `role_required` and the login redirect use it,
so a view called from `fetchJSON` automatically gets JSON 401/403/404/409 responses.

Status codes: 200/201 success · 400 malformed request / CSRF · 401 not signed in · 403 wrong role · 404 missing *or not
yours* · 409 conflict (also `StaleDataError` from optimistic locking, mapped globally) · 413 too large · 422 validation ·
429 rate limit (`Retry-After` header).

---

## 7. Lists and pagination

Query-string convention: `?page=N` (1-based) and optional `?per_page=M` (default 20, capped at 100). Filters are
plain GET parameters so every filtered page is linkable.

```python
from app.services.http import paginate
from app.services.permissions import scope_to_my_ateliers

@bp.route("/events")
@role_required(Role.ATELIER_MANAGER)
def events():
    status = request.args.get("status", "")
    stmt = scope_to_my_ateliers(select(Event), Event.atelier_id).order_by(Event.date.desc())
    if EventStatus.is_valid(status):
        stmt = stmt.where(Event.status == status)
    page = paginate(stmt)                      # Flask-SQLAlchemy Pagination; a page past the end is just empty
    return render_template("ateliers/events.html", page=page, status=status)
```

```jinja
{{ data_table(columns, page.items) }}
{{ pagination(page, 'ateliers.events', status=status) }}      {# keeps the filters in the page links #}
```

`page_args()` returns `(page, per_page)` if you need to slice manually. Aggregations (sums, counts) are computed in SQL
(`func.sum`, `func.count`), not by loading rows.

---

## 8. Flash, notifications, audit and the event bus

* **Flash** — `flash("ذخیره شد.", "success")` (`success|error|warning|info`); `base.html` turns flashes into toasts.
* **Notifications** (the bell, `/notifications`) — `from app.services import notifications`:
  `notifications.notify(user_or_id, kind, title, body="", url=None, entity=obj)`; `None` as user is a silent no-op (roster
  member without an account). Kinds: `NotificationKind` in `constants.py` (free strings allowed). Give a `url`
  that is a **site-relative path** (off-site links are never rendered). Titles are short Persian sentences.
* **Audit** — `audit.log(actor, "invoice.submitted", invoice, {"revision": 2})` for every action with legal or
  money consequences (create/submit/void invoice, change pay terms, approve/suspend a user, mark payroll paid, delete).
  Dotted lower-case verbs; `data` small JSON; secrets are redacted by key name; never put full mobile numbers or
  passwords in it. Rows are append-only.
* **Event bus** — decouple modules (`docs/DATA_MODEL.md` section 6 lists the events and payloads):

```python
# accounting/routes.py — after saving an invoice
db.session.commit()
eventbus.publish("invoice.saved", invoice_id=invoice.id, event_id=event.id,
                 atelier_id=atelier.id, actor_id=user.id, created=True)

# inventory/__init__.py
@eventbus.subscribe("invoice.saved")
def deduct_paper(invoice_id, **_):               # declare only the payload keys you need
    with db.session.begin_nested():              # protect the publisher's transaction
        ...
```

Handlers run synchronously in the publisher's thread/transaction, must not `commit`/`rollback`, and their failures
are logged and swallowed. **Publish after you have flushed** (ids exist) and, for things other code reads back, after
the commit. `staffing.*` already publishes `assignment.changed`, `event.status_changed`, `equipment.assigned`.

---

## 9. Templates, assets and CSP

* Extend `base.html`, import macros from `components/ui.html`; wrap content in `<div class="page">`
  (`docs/UI_GUIDE.md` sections 1–8). Templates live in `app/templates/<module>/`.
* Persian, RTL, **390 px first**; test at 390 and 1280 px.
* Filters: `toman`, `fa`, `jdate(fmt)`, `jdatetime`, `jweekday`, `label_fa(group)`. Globals: every enum from
  `constants.py` by class name (`Role.choices()`, `Skill`, `EventStatus.DONE`), `media_url(path)`, `csrf_token()`,
  `label_fa(group, value)`. Context: `current_user`, `nav_items`, `unread_count`, `platform_name`, `today`.
* **CSP**: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self';
  object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`. Therefore: **no inline `<script>`, no
  `style="…"`, no `onclick=`**, no external hosts. Behaviour goes in `app/static/js/<module>.js` loaded with
  `defer` from `{% block scripts %}`, using `data-*` hooks; styling goes in `app/static/css/<module>.css` using tokens
  (`var(--accent)`), linked from `{% block head %}`. A test (`test_shell_app.py::test_pages_ship_no_inline_script_or_external_assets`)
  checks the shell pages.
* Escaping: never mark user text `|safe`. Pass HTML only for macro arguments you built yourself.
* Every HTML response is sent `Cache-Control: no-store`; static files are cached (version them with `?v={{ asset_version }}`).
* Security headers on everything: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, HSTS in production over HTTPS.

---

## 10. Uploads

Avatars, atelier logos and portfolio pictures use one pipeline (`app/services/uploads.py`; EXIF is **not** stripped):

```python
from app.services import uploads

try:
    stored = uploads.save_image(request.files.get("avatar"), "avatar")      # kind: lowercase word
except uploads.UploadError as exc:                                          # Persian message, safe to show
    form.error("avatar", str(exc))
else:
    old, user.avatar_path = user.avatar_path, stored.path                   # store the RELATIVE path
    db.session.commit()
    uploads.delete_image(old)                                               # after the commit
```

`<img src="{{ media_url(user.avatar_path) }}">`. Files are validated by extension **and** magic bytes (JPEG, PNG, WebP only;
no SVG/GIF/HEIC), capped by `MAX_UPLOAD_BYTES` (5 MiB) and `MAX_IMAGE_PIXELS`, stored under `UPLOAD_DIR`
(outside `static/`) with random names, and served by `/media/<path>` with `nosniff` and a sandbox CSP. Visibility is decided
per kind: `avatar` (signed-in users; anonymous only for public profiles), `logo` (public), `portfolio` (owner/admin, else public
items of public profiles or signed-in users). For a new kind register a checker:

```python
uploads.register_kind("receipt", lambda relpath, user: row_for(relpath) and row_for(relpath).atelier_id in my_atelier_ids(user))
```

Unknown kinds are visible to signed-in users only. Forms that upload need `enctype="multipart/form-data"`.

---

## 11. Tests

Run: `python -m pytest` (whole suite) or `python -m pytest tests/test_halls*.py`. PostgreSQL:
`ATELIER_TEST_DB_URL=postgresql+psycopg2://… python -m pytest`.

**Naming:** `tests/test_<module>.py`, `tests/test_<module>_<topic>.py` (e.g. `test_accounting_invoice.py`).
Shell tests are `test_shell*.py` and `test_auth*.py`.

### Fixtures (`tests/conftest.py`)

| fixture | what you get |
|---|---|
| `app` | a fresh app: in-memory SQLite, `TESTING`, CSRF off, console SMS, all modules; an **app context is pushed for the whole test**, so `db.session` and the models just work |
| `app_config` | dict of config overrides for `app` — redefine it in your module/class (`@pytest.fixture def app_config(): return {"REQUIRE_APPROVAL_ROLES": ("hall_manager",)}`) |
| `client` | `app.test_client()`; **each request runs in its own app context and database session** (like production, uncommitted work rolled back), and your session is expired afterwards. A view that forgets `commit()` fails your test. |
| `login_as(client, user)` | signs the client in without OTP (real session cookie, Flask-Login `strong` protection stays on); returns the client |
| `client_for(user)` | a new client already signed in as `user` — for multi-role tests |
| `csrf_app`, `csrf_client`, `get_csrf(client, url)` | the same app with CSRF ON; `get_csrf` scrapes the token from a page |
| `clock` | `FrozenClock` installed as the app clock (`clock.advance(minutes=3)`, `clock.set("2025-07-17 12:00")`) |
| `outbox` | the console SMS provider: `outbox.last_code("0912…")`, `outbox.outbox` |
| `bus` | isolated event bus recording `(event_name, payload)` |
| `empty_nav` | an empty nav registry for the test |
| `make_user(role="staff", *, mobile=None, full_name=None, status="active", password=None, **fields)` | unique mobile numbers; commits |
| `make_hall(manager=None, **fields)` | creates a hall manager if none given |
| `make_atelier(manager=None, **fields)` | creates an atelier manager if none given (`atelier.manager`) |
| `make_event(hall=None, atelier=None, *, date=None, status=None, **fields)` | `confirmed` when an atelier is given, else `draft`; consecutive dates from 2025-07-17 |
| `make_roster_member(atelier=None, *, user=None, display_name=None, skills=("photographer",), **fields)` | fixed staff (A7) |
| `make_assignment(event=None, role="photographer", *, user=None, roster_member=None, status="confirmed", base_pay=1_000_000, commission_per_unit=9_000, bonus_tiers=None)` | the AGENTS.md test vector terms by default; built directly (no notifications) — use `staffing.assign_person` to test the service |
| `make_staff_profile`, `make_job_post`, `make_notification` | marketplace / bell data |

Pattern:

```python
def test_atelier_manager_cannot_open_foreign_event(client, make_atelier, make_event, login_as):
    mine, theirs = make_atelier(), make_atelier()
    foreign = make_event(atelier=theirs)
    login_as(client, mine.manager)
    assert client.get(f"/atelier/events/{foreign.id}").status_code == 404


def test_anonymous_is_sent_to_login(client):
    response = client.get("/atelier/events")
    assert response.status_code == 302 and "/auth/login" in response.headers["Location"]


def test_save_invoice_computes_pay(client, make_atelier, make_event, make_assignment, login_as, bus):
    ...
    response = client.post(f"/accounting/events/{event.id}/invoice", data={...})
    assert response.status_code == 302
    db.session.expire_all()                                   # the request used its own session
    assert db.session.scalar(select(Invoice)).net_profit == ...
    assert ("invoice.saved", ...) in bus
```

Write for every module: **(1)** anonymous → login, **(2)** wrong role → 403, **(3)** foreign object → 404 (this is the one that finds
real bugs), **(4)** validation error → 422 with the message and the typed values kept, **(5)** the happy path commits,
**(6)** JSON endpoints return `{ok, data|error}`, **(7)** money is an integer end to end (use the 1,870,000 vector from AGENTS.md).
Use `tests/test_shell_permissions.py` as an example of testing ownership helpers, `routes-matrix` for a quick sweep.

---

## 12. Persian copy guidelines

* All UI text is **Persian**; code, identifiers, comments and docs are English.
* Address the user formally but warmly with «شما». Buttons are verbs: «ذخیره»، «ثبت فاکتور»، «افیش نیرو»، «ارسال دعوت».
  Primary action first, «انصراف» second. Avoid «بله/خیر» buttons: say what happens («بله، حذف شود»).
* Messages: success «ذخیره شد.», «افیش شد.»; error = what went wrong + what to do: «تاریخ مراسم نامعتبر است؛ مثلاً ۱۴۰۴/۰۴/۲۶.»
  Never blame the user, never show exception text or English.
* Required-field message: «{label} را وارد کنید.» (the `Form` helpers produce these).
* **Digits:** show Persian digits (`|fa`, `|toman`, `|jdate`); accept all three digit systems on input.
  Phone numbers, codes and signed amounts inside `.num` / `<bdo dir="ltr">` so they read left-to-right.
* **Half-space (ZWNJ, نیم‌فاصله):** «می‌شود»، «گل‌های»، «خانه‌ها»، «نیروها»؛ `Form.text` keeps it.
  Use Persian ی and ک (not Arabic ي ك).
* Money: «۱٬۸۷۰٬۰۰۰ تومان» (thousands separator ٬). Dates: Jalali `۱۴۰۴/۰۴/۲۶` or «۲۶ تیر ۱۴۰۴»; weekday names with `|jweekday`.
* **Glossary** (use exactly): مدیر تالار، مدیر اتلیه (کارفرما)، نیرو، عکاس، ادیتور، پرزنتور، فروشنده، مراسم، افیش (تخصیص نیرو
  به مراسم)، فاکتور، پورسانت، پایه، پاداش، کارتخوان، کارت‌به‌کارت، نقد، اجارهٔ باغ/تالار، انبار، اتلیه سیار. Enum values already have labels:
  `Role.STAFF.label`, `label_fa("event_status", ev.status)`, `Enum.choices()` for selects.
* Empty states explain the next step («هنوز مراسمی ثبت نشده؛ با ثبت اولین مراسم، کارت آن این‌جا نمایش داده می‌شود.»).
* Confirm every destructive action («این مراسم و فاکتور آن برای همیشه حذف می‌شود. ادامه می‌دهید؟»).

---

## 13. Authentication (assumption A6)

Mobile number + one-time code is the primary way in; a password is optional. One pipeline serves login and registration:

```
/auth/login (mobile) ──► code sent by SMS ──► /auth/verify (code)
      │                                          ├─ number has an account ........ signed in ──► next / dashboard
      └─ /auth/register (role, name, mobile) ───►├─ new number, form already filled  account created, signed in ──► onboarding
                                                 └─ new number, came from login ..... /auth/register: name + role, then created
```

* **No account enumeration:** known and unknown numbers get identical responses until the owner proved the number with a code.
* **Codes:** 6 digits (`secrets`), HMAC-hashed with the number at rest (`OtpCode.hash_code`), 2-minute expiry, 5 attempts then dead,
  constant-time compare, a new code retires the older ones, single use.
* **Limits (no Redis, counted from `otp_codes`, so exact across workers):** 60 s resend cooldown per number, 5 codes per number
  per hour, 20 codes per IP per hour (429 + `Retry-After`). Password sign-in failures are throttled per (IP, number) and per IP
  with an in-process limiter (per worker). Behind a proxy set `TRUST_PROXY_HOPS` or all clients look like one IP.
* **Sessions:** the session is cleared on every login (no fixation), `permanent` 14 days, optional 30-day "remember me",
  cookies `HttpOnly`, `SameSite=Lax`, `Secure` in production, Flask-Login `strong` protection. Logout is POST + CSRF and also
  removes the remember cookie. A suspended user is signed out on the next request and cannot sign in.
* **`next`:** only same-site paths are honoured (`security.safe_next`); `//host`, `\host`, schemes, control characters and `/auth/*` are rejected.
* **Roles:** self-registration offers staff / hall manager / atelier manager. **Admin only by CLI** (`create-admin`);
  the registration code refuses `admin` even if configured. `REQUIRE_APPROVAL_ROLES` keeps new accounts of those roles `pending`
  (admins get a notification; the admin module flips `status` to `active`); until then `role_required` sends them to `/auth/pending`.
  Admins can also close registration with the setting `registration_open = false` (`Setting.set("registration_open", False)`).
* **Audit:** `auth.otp_sent`, `auth.otp_failed`, `auth.login`, `auth.login_failed`, `auth.login_blocked`, `auth.logout`,
  `auth.password_changed`, `user.registered`, `user.admin_created|promoted` (numbers masked: `0912***4567`; codes never).
* **Password:** optional, ≥ 8 characters, not only digits / not common / not containing the number; setting a first password requires a
  fresh code (so a stolen session cannot plant a password); changing it requires the current one.
* **SMS:** `SMS_PROVIDER=console` logs the message (and keeps it in `provider.outbox`); `kavenegar` uses only `urllib` with a 5 s timeout
  (`KAVENEGAR_OTP_TEMPLATE` selects the "verify lookup" API). `OTP_DEBUG_SHOW` shows the code on the verify page — **development only**
  (production refuses to start with it). Add a gateway by implementing `SmsProvider`.

For tests use `login_as` (bypasses OTP) or drive the real flow with `outbox.last_code(mobile)`.

---

## 14. Configuration and deployment

`ATELIER_ENV` = `development` (default) | `testing` | `production`. Everything is an environment variable (see the table at the top of
`app/config.py`): `SECRET_KEY` (**required**, ≥ 32 chars in production), `DATABASE_URL` (default `sqlite:///instance/atelier.db`; `postgres://` is
rewritten), `UPLOAD_DIR`, `MAX_CONTENT_LENGTH`, `MAX_UPLOAD_BYTES`, `SMS_PROVIDER`, `KAVENEGAR_API_KEY`, `KAVENEGAR_OTP_TEMPLATE`, `KAVENEGAR_SENDER`,
`OTP_DEBUG_SHOW` (dev only), `REGISTRATION_ROLES`, `REQUIRE_APPROVAL_ROLES`, `TRUST_PROXY_HOPS`, `SESSION_COOKIE_SECURE`, `SITE_DOMAIN`,
`DISABLED_MODULES`, `LOG_LEVEL`. The testing environment ignores ambient variables (hermetic).
`create_app()` accepts `create_app("production")`, `create_app("testing", PLATFORM_NAME="x")` or a mapping of overrides.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") ATELIER_ENV=production
export DATABASE_URL=postgresql+psycopg2://atelier@localhost/atelier SMS_PROVIDER=kavenegar KAVENEGAR_API_KEY=… KAVENEGAR_OTP_TEMPLATE=atelier-login
export TRUST_PROXY_HOPS=1
flask --app wsgi init-db
flask --app wsgi create-admin --mobile 09121234567 --name "نام مدیر"
gunicorn -w 3 -b 127.0.0.1:8000 wsgi:app           # behind nginx / caddy with HTTPS
```

Development: `flask --app wsgi run --debug` (the dev database and a persistent dev secret are created in `instance/`, git-ignored; the OTP
shows on the verify page). Health check: `GET /healthz` → `{"status": "ok", "database": "ok"}` (503 when the database is down).

**Database.** Development and tests call `db.create_all()` at startup (`AUTO_CREATE_DB`); production runs `flask init-db` once.
There are **no Alembic migrations yet**: adding a column to an existing deployment needs a manual `ALTER TABLE` until Flask-Migrate is wired
(documented future step — a baseline migration generated now would drift while the models are still moving). The schema is PostgreSQL-portable
(no SQLite-only SQL). SQLite file databases run in WAL mode with a 15 s busy timeout.

**PWA.** `/manifest.webmanifest` (absolute icon URLs) and `/sw.js` (scope `/`) are served by `main`; the worker caches only `/static/` assets.

---

## 15. Command line

| command | purpose |
|---|---|
| `flask --app wsgi routes` | Flask's own route list |
| `flask --app wsgi init-db [--drop --yes]` | create the tables |
| `flask --app wsgi create-admin --mobile 0912… --name "…" [--ask-password] [--promote]` | the only way to get an admin; `--promote` upgrades an existing account |
| `flask --app wsgi seed-demo` | runs `main()` of `scripts/seed_demo.py` (`main(app)` or `main()`), inside the app context |
| `flask --app wsgi routes-matrix [--format table\|json\|csv] [--methods GET,POST] [--seed]` | every route × (anonymous, admin, hall_manager, atelier_manager, staff) → status code, on a throw-away in-memory database |

Reading `routes-matrix`: protected routes show `302->/auth/login` for anonymous and `403` for the wrong roles; `404` for a sample id (`1`) is expected
(use `--seed` to get real rows); a `200` where you expect `403`, or any `EXC…`/`5xx` cell, is a bug. The command exits 1 when a cell crashed.

---

## 16. Module author checklist

- [ ] `bp = Blueprint("<package>", __name__, url_prefix=…)`; routes imported at the bottom of `__init__.py`; nav (+ home + onboarding) registered.
- [ ] Every view has `@role_required(...)` / `@login_required`; every object loaded through an ownership helper (or `owned_or_404`); lists scoped.
- [ ] State changes are POST with CSRF; destructive ones use `confirm_button`; validation errors return 422 with typed values kept.
- [ ] One `db.session.commit()` per successful POST; services never commit; `IntegrityError` → rollback + form error.
- [ ] Money is `int` Toman (`form.money`, `|toman`); dates Gregorian in the database, Jalali on screen; times via `security.now()`.
- [ ] Money/pay/print formulas come from `app.services.payroll` / `print_yield` — never re-implemented.
- [ ] Audit for money / permission changes; notify the people affected; publish bus events others may care about.
- [ ] No inline script/style, no CDN; CSS uses tokens; 390 px checked; Persian copy per section 12.
- [ ] Tests: anonymous → login, wrong role → 403, foreign object → 404, validation 422, happy path, JSON shape.

---

## 17. Known limits and open questions

* **A8 (licence)** is undecided; nothing in the shell depends on it.
* The in-process rate limiter (password sign-in) is per worker: with N workers the effective limit is up to N × the configured one. OTP sending limits are exact (database).
* A session cookie cannot be revoked server-side before it expires (no server session store); suspending a user takes effect on their next request,
  a password change does not sign out other devices.
* `is_verified` on `User` is not touched by registration (OTP proves the number, `status` controls access); the admin module may use it as a trust badge.
* Uploads keep EXIF metadata (phone-photo orientation depends on it); do not promise location privacy.
* `flask run` resets `DEBUG` from `FLASK_DEBUG`; use `--debug` (templates auto-reload in the development config regardless).
* No Alembic migrations yet (section 14).
