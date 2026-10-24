# `halls` - the hall manager's panel

URL prefix `/hall`, role `hall_manager`. Persian UI, mobile first (390 px), no JavaScript framework, no CDN.

| Page | Endpoint | What it does |
|---|---|---|
| Dashboard | `halls.dashboard` | upcoming events, offers waiting for an answer, recent answers, events waiting for a rating, occupancy of the Jalali month per hall |
| Halls | `index`, `setup`, `hall_new`, `hall_show`, `hall_edit`, `hall_photo_*`, `hall_delete` | a manager may own several halls: name, city, address, phone, map link, notes, default ceremony types, default rent, "one atelier per day", logistics for the atelier team (loading, power, Wi-Fi, parking, set-up spot, on-site contact), up to 12 photos, activate / deactivate, delete (only without events) |
| Calendar and list | `calendar`, `events` | Jalali month calendar and a paginated list (tabs: upcoming, free, offered, confirmed, past, cancelled; filters: hall, text, Jalali month), bulk actions on the selection (offer, open to all, cancel, delete drafts) |
| Events | `event_new`, `bulk_new`, `event_show`, `event_edit`, `event_cancel/reactivate/delete` | one event or a whole season with the multi-date picker (calendar clicks, typed Jalali / ISO dates, weekly or monthly rules expanded by the server, one or two start times per day); exact duplicates of (hall, date, time) are refused; the date of an event that already has an atelier is fixed |
| Offering | `offers`, `offer_new`, `offer_withdraw`, `offer_reoffer`, `event_open`, `event_close_open` | step 1 pick dates, step 2 pick ateliers from the searchable directory, send; or open to every atelier; withdraw, re-offer, see who accepted; history timeline per event |
| Directory | `directory`, `atelier_show` | active ateliers with rating (average of the halls' ratings), city, verified badge, history with my halls, other halls' comments; phone only after a first cooperation or an agreement |
| After the event | `event_show` (`#review`) | head-count per role and the event status - **never** the atelier's invoice, pay or profit - and a 1-5 star rating of the atelier (editable, one per event) |
| Agreements | `agreements`, `agreement_new`, `agreement_edit`, `agreement_end` | period, exclusivity, rent per event or revenue share, deposit, contact; both managers are notified; a reminder command warns before the end |
| Reports | `reports` | events per Jalali month, acceptance rate, response time (median / average), lost dates, per-atelier table |
| JSON | `api_dates_expand` | `POST /hall/api/dates/expand`: a weekly / monthly rule -> ISO dates (the same code the form uses on submit) |

## Where the rules live

* `app/services/event_offers.py` - offer / withdraw / re-offer / open to all / **accept / decline / claim** / exclusivity. Shared with the `ateliers` module (see below). One conditional `UPDATE ... WHERE atelier_id IS NULL` decides a race, so the second claimant always loses.
* `rules.py` - events (create, duplicates, edit, cancel, reactivate, delete), ratings, photos, agreements.
* `dates.py` - pure date maths for bulk adding. Esfand has 29 days and 30 only in a leap year: `1404/12/30` is *rejected with an explanation*, never moved; "the 30th of every month" can `clamp` to the last day or `skip` the month.
* `queries.py`, `search.py`, `stats.py` - read side (no N+1: related rows are joined or fetched with one grouped query per page).
* Status changes that need a cascade (confirm, cancel) go through `app.services.staffing.change_event_status`.

## Business rules worth knowing

* An event is `draft` (nobody asked) -> `offered` (direct offers pending) or `open` (every atelier may claim it) -> `confirmed` (an atelier took it) -> `done` -> `invoiced`; `cancelled` can be reactivated as a draft. When the last pending offer is declined or withdrawn the event becomes `open` with `offer_mode=none`.
* `Hall.single_atelier_per_day` and exclusive `HallAgreement`s are checked when offering, accepting and claiming. An exclusive agreement also withdraws pending offers to other ateliers in its period and blocks "open to all" for those dates.
* `Atelier.rent_for_hall` pre-fills the invoice's venue rent: atelier's own per-hall price > active agreement's `rent_per_event` > `Hall.default_rent` > atelier default. The invoice line stays editable (AGENTS.md).
* The hall sees only head-counts of the staff. Ratings are `Review` rows with `event_id` + `atelier_id` and no assignment.
* Idempotence is by natural key: re-sending an offer, re-submitting a bulk form or double-tapping "create" never creates a second row.

## Integration notes

* `app/modules/ateliers/offers.py` still carries its own copy of accept / decline / claim (written before `event_offers` existed). It behaves the same but knows nothing of exclusivity, actor checks, `offered_at`, or reopening the event after the last decline. Replace its body with `from app.services.event_offers import *` (same names: `OfferError`, `OfferUnavailable`, `TAKEABLE_STATUSES`, `accept_offer`, `decline_offer`, `claim_open_event`) to get the full behaviour.
* `flask --app wsgi halls-agreement-reminders [--days 14]` - run daily from cron.
* New columns / tables: see `docs/DATA_MODEL.md` (section 8, items 11-14). There are no migrations yet.

## Tests

`tests/test_halls_*.py`: `dates` (parsing, rules, Esfand 30), `offers` (service, race, exclusivity), `events` (CRUD, duplicates, bulk, calendar), `profiles`, `flow` (hall + atelier screens end to end, ratings, no finances), `pages` (dashboard, directory, reports, agreements, rent, reminders), `access` (roles, IDOR, CSRF, GET sweep), `browser` (Playwright: the picker, selection bar, 390 px, tap targets; skipped without Chromium).
