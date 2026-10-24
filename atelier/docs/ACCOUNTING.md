# Accounting (حسابداری) — event cards, invoices, payroll, reports

Source of truth for the rules is `/AGENTS.md` ("بخش مالی", the pay formula, assumptions A1–A5). This page says
how they are implemented and where. Code: `app/services/ledger.py`, `app/modules/accounting/`,
`app/templates/accounting/`, `app/static/{js/accounting.js,css/accounting.css}`.
Tests: `tests/test_accounting_*.py` (about 200 tests, incl. a real-browser file that skips without Chromium).

## 1. Who and where

Only **atelier managers**, and only for events / invoices / payroll lines of **their own ateliers**. Everything else is
`403` (role) or `404` (somebody else's id — the same answer as "does not exist"). Admins, hall managers and staff
never see the books. A manager with several ateliers picks one with `?atelier=<id>` (checked, then remembered in the session).

| URL | What |
|---|---|
| `GET /accounting` | event cards of a Jalali month/year, filters (`y`, `m` (0 = year), `status`, `q`, `page`), month summary |
| `GET /accounting/events/<id>/invoice` | the invoice form. `?fragment=1` returns only the form (what the popup loads); without it, a full page (the no-JS fallback, and where validation errors land) |
| `POST /accounting/events/<id>/invoice` | save (create or edit) |
| `POST /accounting/events/<id>/invoice/preview` | live numbers as JSON (same parser and same arithmetic as save; writes nothing) |
| `GET /accounting/invoices/<id>/print` · `export.csv` | A4 sheet (print.css, amounts in words) · Excel-safe CSV |
| `POST /accounting/invoices/<id>/reopen` · `void` | lifecycle, a reason is mandatory |
| `GET /accounting/payroll` (+ `export.csv`) | monthly payroll per person |
| `POST /accounting/payroll/pay` · `unpay` | mark lines paid (one or many) / undo |
| `GET /accounting/reports` (+ `export.csv`) | profit trend (inline SVG), per-event and per-hall profitability, staff leaderboard |

## 2. The arithmetic (never re-implemented)

```
units      = Σ qty × weight          over lines that count for commission       (payroll.units_from_lines)
commission = units × commission_per_unit; bonus = highest tier with threshold ≤ units; total = base + commission + bonus
income     = card_reader + card_to_card + cash
expenses   = venue_rent + staff_pay + equipment_rental + material + other
net_profit = income − expenses                                                    (may be negative)
```

`ledger.compute_invoice()` calls `payroll.units_from_lines` once and `payroll.compute_pay` for every person with the
terms **snapshot of that person's assignment** (`assignment.pay_terms()`), so six people can have six different deals.
All figures are integer Toman; every total is guarded against overflowing a BIGINT (`LedgerError(code="overflow")`).
JavaScript only displays what `/preview` returns — a test fails if `accounting.js` mentions `commission_per_unit`, `bonus_tiers`
or `parseFloat`.

**Who is paid:** assignments that are accepted, confirmed or completed (`payable_assignments`). Invited / declined /
cancelled people earn nothing. **Sold lines** are stored only when `qty > 0`; sizes go through
`print_yield.normalize_size` (so `۷۰ × ۵۰` → `50x70`) and must also be whole centimetres because the table stores `INTEGERxINTEGER`.

## 3. Invoice life cycle

```
(none) --save--> submitted --every payroll line paid--> settled
                   |  ^                                   |
              reopen|  |save again                        +-- a payment is undone --> submitted
                   v  |
                 draft            submitted | draft --void--> void   (terminal; kept for the audit trail)
```

* **Save** moves the event `confirmed → done → invoiced` (a `done` event goes straight to `invoiced`; unanswered invitations are
  cancelled by the `done` step, as `staffing.change_event_status` defines), writes the invoice, lines, three payments, expenses and
  payroll lines **in one transaction**, locks the pay terms of every committed assignment (`staffing.lock_event_terms`), audits
  `invoice.submitted` / `invoice.updated` (old and new values) and notifies each person with an account ("حقوق شما ثبت شد" /
  "حقوق شما به‌روز شد", only when their total changed). `invoice.saved` is published **after the commit**
  (`invoice_id, event_id, atelier_id, actor_id, created`); subscribers' own writes are committed in a second, best-effort commit.
* **Edit** replaces lines / payments / non-staff expenses and recomputes payroll **in place**, so ids, paid markers and
  the audit trail stay attached. The form carries `base_revision`; a different current revision is a `409` (never a silent overwrite).
* **Paid lines** keep `paid_at / paid_method / paid_note`. The *figures* follow the formula (the invoice always equals what the
  formula says); when a paid line's total changes, the save reports a warning, audits `payroll.recalculated`, and the payroll sheet
  shows a red «مغایرت» flag (the amount recorded at payment time is read from the `payroll.paid` audit row).
* **Reopen** (`submitted → draft`): event back to `done`, pay terms unlocked so the afish module can correct them
  (`staffing.set_terms`), revision bumped. Saving again re-submits and re-locks.
* **Void**: terminal, refused while any payroll line is paid (undo the payments first). The invoice stays in the database
  (`event_id` is unique, so a voided event cannot be invoiced again). Void and draft invoices are excluded from payroll, summaries and reports.
* **Settlement** (`submitted ⇄ settled`) is derived: settled exactly when the invoice has payroll lines and all are paid.

### Double submit and idempotency

Every form render carries a one-time `save_token`. It is stored in the audit row of the save (`data.nonce`, no schema change). A repeat of
the same token with a stale revision is answered "already saved" (302 with an info flash, or JSON `replay: true`) instead of creating a
second invoice; a stale revision with a *different* token is a conflict. A concurrent create is caught as `IntegrityError` on the unique
`invoices.event_id` and treated the same way. In the popup the submit button is also locked client-side.

## 4. The popup form (field names)

Plain `name=value` pairs (the money / stepper widgets submit raw integers; Persian and Arabic digits are accepted too):

```
qty__print__16x21  qty__print__20x30  qty__print__30x40  qty__raw_file  qty__edited_file  qty__frame__16x21 ...  qty__chassis__30x40
nocomm__<row key>                    checkbox "بدون پورسانت" = counts_for_commission false (A2)
custom__<print|frame|chassis>__<n>__size|qty|weight|nocomm     a "+ ابعاد دیگر" row; weight = «تعداد محاسبه برای حقوق نیرو» (default 1)
pay__card_reader  pay__card_to_card  pay__cash
venue_rent         absent = the default (AtelierHall.rent_amount, else Atelier.default_venue_rent); blank = 0
equip__<usage id>__amount            suggested rental from EquipmentUsage (only ids of this event are accepted)
extra__<n>__label|amount|kind        free expense lines, kind ∈ equipment_rental | material | other
notes  base_revision  save_token
```

Limits: qty ≤ 5 000 per row, weight ≤ 50, money ≤ 100 000 000 000 Toman per field, ≤ 15 "other size" rows per category, ≤ 30 extra rows.
Errors come back as `{field name: Persian message}` — the page re-renders the form (422) with the typed values and a list of all messages;
inside the popup the same HTML arrives as JSON (`{"ok": false, "fields": {...}, "html": "<form…>"}`) so the user never leaves it.

`/preview` returns `json_ok` with `units`, `income{…, total}`, `expenses{venue_rent, staff_pay, equipment_rental, material, other, non_staff, total}`,
`net_profit` and `people[{assignment_id, name, role, units, base, commission, bonus, total, applied_tier, paid}]` plus `warnings`.

Unsent drafts are kept in `localStorage` (per event and revision, three days) and offered back with a "بازیابی پیش‌نویس" banner.

## 5. Payroll and reports

* **Payroll sheet**: per person — events, units (a second role at the same event adds none), base, commission, bonus, total, paid / unpaid; people with a roster
  entry linked to an account are one person. Lines are marked paid one by one or in bulk with a date (not in the future), method
  (`PayoutMethod`) and note; every line is audited (`payroll.paid` with the amount), the person is notified once ("حقوق شما پرداخت شد"),
  and an id that is not yours makes the whole request `404`.
* **Reports**: yearly trend as an inline SVG (no library; geometry in `reports.trend_chart`, a data table below it for screen readers), per-event table with whole-percent
  margin (paginated), per-hall profitability, staff leaderboard by units. Reports default to the whole year, the invoice page to the current Jalali month.
* **CSV**: UTF-8 **with BOM**, ASCII digits, a Gregorian and a Jalali date column, and every cell that starts with `= + - @` is prefixed with `'`.

## 6. Audit actions written here

`invoice.submitted`, `invoice.updated`, `invoice.reopened`, `invoice.voided` (data: `before`, `after`, `reason`, `nonce`, `warnings`),
`payroll.paid`, `payroll.unpaid`, `payroll.recalculated`; plus what `staffing` writes (`assignment.terms_locked|unlocked`, `event.<status>`).

## 7. Decisions where AGENTS.md is silent

1. Paid lines are updated in place and flagged (not frozen): the invoice must always equal the formula, and the difference is
   what the owner has to settle with the person.
2. `commission_weight` is a whole number ≥ 0 (a fractional weight would make commission a fraction of a Toman).
3. A person with an accepted / confirmed / completed assignment is paid; invited people are not.
4. Saving an invoice for a `confirmed` event implies the ceremony took place (`confirmed → done → invoiced`).
5. `void` is terminal; correct a mistake with the edit form, or `reopen` when pay terms must change.
6. The month summary and reports count only `submitted` / `settled` invoices.
