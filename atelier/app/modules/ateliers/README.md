# `ateliers` - the atelier manager's panel

URL prefix `/atelier`, role `atelier_manager`.  Persian UI, mobile first, no JavaScript framework.

| Page | Endpoint | What it does |
|---|---|---|
| Dashboard | `ateliers.dashboard` | alerts per missing role, hall offers, closing reminders, month KPIs (from `Invoice` totals), notifications |
| Setup / settings | `setup`, `settings*` | create the atelier, profile + logo, rent per hall (`AtelierHall`), default pay presets per role, double-booking policy |
| Roster (نیروهای ثابت) | `roster*` | create / edit / (de)activate fixed staff, with or without an account (linked by mobile), default terms per skill |
| Events | `events`, `calendar`, `offer_accept/decline`, `event_claim` | offers addressed to the atelier, open events (claim), upcoming / past, Jalali calendar |
| Event page | `event_detail` | facts, the afish (role slots), head-count, equipment, invoice status, hand back / mark done |
| Afish board | `afish_board`, `afish_pick`, `afish_assign`, `afish_action`, `afish_terms`, `afish_confirm_all`, `afish_copy` | the staffing board |

## Rules this module relies on (it never re-implements them)

* **Staffing** - `app.services.staffing` (`assign_person`, `transition`, `set_terms`, `change_event_status`,
  `check_assignable`, `attach_equipment`).  Terms are *snapshotted* on the assignment; presets and roster
  defaults only pre-fill the form.
* **Pay** - `app.services.payroll.compute_pay` (the "example" under the terms form calls `POST /atelier/api/pay-preview`).
* **Permissions** - every object is loaded through `app.services.permissions` (`event_or_404(as_=("atelier",))`,
  `roster_member_or_404`, `assignment_or_404`, ...), foreign ids answer 404.  Ids that arrive in a form
  are re-checked on every request.

## Afish behaviour worth knowing

* Roster members are `confirmed` at once (A7), platform users are `invited` and notified.
* A person who is busy the same day (any atelier) or marked the day "unavailable" gives a *blocking warning*
  with an explicit override checkbox; `Atelier.block_double_booking` turns the override off.  Clashes in other
  ateliers are shown as "busy" without any detail.
* "Confirm accepted" confirms only people who accepted; an unanswered invitation can be confirmed one by one
  ("قطعی (بدون انتظار)").
* Replace = assign the new person first, then cancel the old one, in one transaction.
* Copy from a previous event repeats the *snapshot* terms of the source assignment, skipping people who are
  busy, unavailable, inactive or already assigned.
* Staff directory: listed profiles can be searched by name / city / skill; any other staff account can only be
  reached by typing the exact mobile number (a signed, one-hour token then allows assigning it) and exact-number
  probes are rate limited (`LOOKUP_LIMIT` per hour).

## Links to other modules (`links.py`)

Nothing is duplicated; links are resolved defensively and hidden when the target does not exist.

* **accounting** - `accounting.invoice_form` (`event_id`) or `/accounting/events/<id>/invoice`.
* **inventory** - `inventory.index` or `/inventory`.
* **marketplace** - the "انتشار آگهی در بازار کار" button of a slot opens the create-job form
  (`marketplace.job_new` / `new_job` / `job_create` / `post_job` / `jobs_new`, else `/jobs/new`) with these query
  parameters, so the form can be pre-filled:

  | parameter | meaning |
  |---|---|
  | `event` | event id |
  | `role` | `photographer` / `editor` / `presenter` / `seller` |
  | `date` | event date, ISO (Gregorian) |
  | `city` | hall city |
  | `title` | suggested title |
  | `slots` | how many people are still needed for the role |
  | `base`, `commission` | integer Toman from the atelier preset for the role |
  | `tiers` | bonus tiers as `threshold:amount` pairs separated by commas, e.g. `70:150000,100:300000` |

## Reminders

`flask --app wsgi ateliers-reminders` (cron, every 30-60 minutes) creates in-app notifications, each at most
once per assignment (the notifications table is the idempotence record): day-before and same-day reminders for
accepted / confirmed people, an escalation to the manager for invitations unanswered after 48 hours, and an alert
the day before an event when somebody has not answered.

## Tests

`tests/test_ateliers_*.py` (shared fixtures in `tests/test_ateliers_support.py`); the browser tests
(`test_ateliers_browser.py`) need Playwright + Chromium and skip themselves otherwise.
