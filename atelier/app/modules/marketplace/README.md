# `marketplace` - the online match between staff and atelier managers

Routes start with `/jobs` and `/talent` (blueprint without a URL prefix). Business rules are in `/AGENTS.md`;
this module owns **no** pay maths (`app.services.payroll`) and **no** staffing rules (`app.services.staffing`).

| URL | endpoint | who | what |
|---|---|---|---|
| `/jobs` | `jobs` | everybody | public board: filters (city, role, Jalali date range, minimum pay), sort (date / pay / newest), pagination |
| `/jobs/<id>` | `job_detail` | everybody | the call with terms in plain Persian, a worked example and scenarios; apply form for staff |
| `/jobs/<id>/apply` | `job_apply` | staff | apply with a message |
| `/jobs/<id>/report` | `job_report` | signed in | report a post (audit + notification to admins; the admin module hides) |
| `/jobs/applications` | `my_applications` | staff | **the staff module links here**: my applications, withdraw, contact after acceptance |
| `/jobs/invitations` | `my_invitations` | staff | direct invitations: accept / decline |
| `/jobs/manage` | `manage_jobs` | atelier manager | my posts by state; **`ateliers.links.job_posts_url()` finds it by this name** |
| `/jobs/new`, `/jobs/<id>/edit` | `job_new`, `job_edit` | atelier manager | the post form; **`job_new` takes the query contract of `ateliers.links.marketplace_job_url`** (`event, role, date, city, title, slots, base, commission, tiers=70:150000,100:300000`) |
| `/jobs/<id>/close`, `/reopen` | `job_close`, `job_reopen` | atelier manager | close (optionally rejecting the pending applicants - "bulk close") / reopen |
| `/jobs/manage/inbox`, `/jobs/<id>/applicants` | `inbox`, `job_applicants` | atelier manager | applicants, tabs by status |
| `/jobs/manage/applications/<id>` (+ `/accept`, `/reject`) | `manage_application`, ... | atelier manager | review one applicant; accept = afish row |
| `/talent`, `/talent/<user_id>` | `talent_index`, `talent_profile` | atelier / hall managers, admins | directory (only `is_listed` staff) and profile |
| `/talent/<user_id>/invite` | `talent_invite` | atelier manager | invite to one role of one event (an assignment in `invited`) |
| `/jobs/reviews`, `/jobs/reviews/new/<assignment_id>` | `reviews`, `review_new` | atelier manager, staff | two-way reviews of completed assignments |
| `/jobs/reviews/<id>/report` | `review_report` | the reviewed person | report a review |
| `/jobs/api/pay-preview` | `pay_preview` | atelier manager | JSON: `payroll.compute_pay` for the live example of the form |

## Rules worth knowing

* **Accept creates the afish.** `services.accept_application` calls `staffing.assign_person(event, role, user=applicant,
  terms=post.terms_dict(), source=marketplace, status=confirmed, job_application=application)`. The applicant said yes by
  applying, so the row starts `confirmed`. The post needs an event: its own `event_id`, or - for a post without one - the
  atelier's only event on that date, or the one the manager picks (only events of the post's atelier on the post's date are
  accepted, ids from the form are never trusted). Double booking, duplicate role and closed events are refused by the staffing
  service; the review page warns about a busy applicant without naming the other event.
* **What the applicant saw is what they get.** While a post has pending or accepted applications its role, date, linked event
  and pay terms are frozen (the edit form shows them read-only and the server ignores forged values); the snapshot on the
  assignment is the post's terms at acceptance and never changes afterwards.
* **Fill count.** `open` <-> `filled` follows the number of accepted applications (`services.sync_post_status`). A bus subscriber
  on `assignment.changed` returns an application to `withdrawn` and reopens the post when its afish row is cancelled or declined
  elsewhere (the afish board, an event cancellation, the staff member's own page). `closed` and `hidden` are human decisions.
  `hidden` (admin moderation) is never touched by this module.
* **Privacy by design.** A mobile number is shown only for an accepted application or a committed (accepted / confirmed /
  completed) assignment between the two parties (`queries.relation_to`); hall managers get it through assignments at their halls,
  admins always. Lists never show a number, not even masked, and `common.person_name` never falls back to the number the way
  `User.display_name` does. A staff profile that is not listed can still be opened by the atelier that has an application from /
  an assignment for that person.
* **Spam protection** counted from the database (exact across workers), per 24 hours: posts, applications, invitations, reports -
  `MARKETPLACE_MAX_POSTS_PER_DAY` (10), `..._APPLICATIONS_PER_DAY` (20), `..._INVITES_PER_DAY` (20), `..._REPORTS_PER_DAY` (10).
* **"Minimum pay" and "sort by pay"** mean the total at 80 sold photos (base + commission + highest reached bonus tier, from
  `payroll.compute_pay`), the same number the card shows. Bonus tiers are JSON, so these two options are evaluated in Python on at
  most 1000 candidate rows; the default sort-by-date path is pure SQL.
* **Ratings.** Staff ratings are `StaffProfile.rating_avg/rating_count` (`refresh_rating()` after every review). Atelier ratings
  are computed on the fly from the visible reviews whose subject is the atelier's manager and whose assignment belongs to an event
  of that atelier (one grouped query per page; there is no stored atelier rating, so no schema change).
  Hidden reviews (`is_hidden`) are excluded everywhere.
* **Notifications**: new application, decision, withdrawal, review received, invitation (by `staffing`). **Audit**:
  `job_post.created|updated|closed|reopened|reported`, `job_application.created|accepted|rejected|withdrawn`, `talent.invited`,
  `review.created|reported`. **Bus events** published: `application.changed`, `review.created`.

## Files

`routes_board.py` (public), `routes_manage.py`, `routes_staff.py`, `routes_talent.py`, `routes_reviews.py`;
`services.py` (rules, never commits), `queries.py` (read side, one grouped query per page), `cards.py` (list view models),
`forms.py`, `common.py` (wording + examples + limits), `web.py` (response helpers). Templates: `app/templates/marketplace/`,
assets `app/static/css/marketplace.css`, `app/static/js/marketplace.js`.

## Not done (needs a schema change or another module)

A "would rehire" flag on reviews, blocking a user, and private review comments for admins only need new columns / tables.
Hiding a reported post or review is the admin module's job (`JobPost.status = hidden`, `Review.is_hidden = True` followed by
`StaffProfile.refresh_rating()`).
