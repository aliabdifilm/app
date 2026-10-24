"""Call-time reminders and unanswered-invitation escalation (run from cron).

``flask --app wsgi ateliers-reminders`` (every 30-60 minutes) sends, at most once each:

* ``reminder_day_before``      to a person who accepted / was confirmed: "tomorrow you work at ..."
* ``reminder_same_day``        to the same people a few hours before the call time
* ``invite_unanswered``        to the atelier manager: an invitation is still open after 48 hours
* ``unconfirmed_day_before``   to the atelier manager: the event is tomorrow and somebody has not answered

Idempotence comes from the ``notifications`` table itself: before sending, the command looks for an
existing ``(user, kind, assignment)`` row, so re-running it (or restarting the scheduler) never
repeats a message.  Times: events store a local date and time; the clock is Iran time
(``jalali.to_iran_time``), timestamps in the database are UTC.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import sqlalchemy as sa
from flask import current_app, has_request_context, url_for
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Assignment, Event, Notification
from app.services import jalali, notifications, security
from app.services.constants import (
    CLOSED_EVENT_STATUSES,
    AssignmentStatus,
)
from app.services.money import to_persian_digits

KIND_DAY_BEFORE = "reminder_day_before"
KIND_SAME_DAY = "reminder_same_day"
KIND_UNANSWERED = "invite_unanswered"
KIND_UNCONFIRMED = "unconfirmed_day_before"
ALL_KINDS = (KIND_DAY_BEFORE, KIND_SAME_DAY, KIND_UNANSWERED, KIND_UNCONFIRMED)

UNANSWERED_AFTER = dt.timedelta(hours=48)
SAME_DAY_LEAD = dt.timedelta(hours=4)
SAME_DAY_GRACE = dt.timedelta(hours=3)
MORNING = dt.time(7, 0)  # same-day reminder time for events without a call time


@dataclass
class ReminderResult:
    sent: dict[str, int] = field(default_factory=lambda: {kind: 0 for kind in ALL_KINDS})

    @property
    def total(self) -> int:
        return sum(self.sent.values())


def _time_text(value: Optional[dt.time]) -> str:
    return to_persian_digits(value.strftime("%H:%M")) if value else ""


def _already_sent(assignment_ids: list[int]) -> set[tuple[int, str, int]]:
    if not assignment_ids:
        return set()
    rows = db.session.execute(
        sa.select(Notification.user_id, Notification.kind, Notification.entity_id).where(
            Notification.entity_type == Assignment.__tablename__,
            Notification.kind.in_(ALL_KINDS),
            Notification.entity_id.in_(assignment_ids),
        )
    )
    return {(row.user_id, row.kind, row.entity_id) for row in rows}


def _candidates(first: dt.date, last: dt.date) -> list[Assignment]:
    return list(
        db.session.execute(
            sa.select(Assignment)
            .join(Event, Event.id == Assignment.event_id)
            .where(
                Event.date >= first,
                Event.date <= last,
                Event.atelier_id.is_not(None),
                Event.status.not_in([status.value for status in CLOSED_EVENT_STATUSES]),
                Assignment.status.in_(
                    [
                        AssignmentStatus.INVITED.value,
                        AssignmentStatus.ACCEPTED.value,
                        AssignmentStatus.CONFIRMED.value,
                    ]
                ),
            )
            .options(
                joinedload(Assignment.event).joinedload(Event.hall),
                joinedload(Assignment.event).joinedload(Event.atelier),
                joinedload(Assignment.user),
                joinedload(Assignment.roster_member),
            )
            .order_by(Assignment.id)
        )
        .scalars()
        .unique()
        .all()
    )


def _event_url(event: Event) -> str:
    """Site-relative link to the event page (works from the CLI, where there is no request)."""
    if has_request_context():
        return url_for("ateliers.event_detail", event_id=event.id)
    with current_app.test_request_context():
        return url_for("ateliers.event_detail", event_id=event.id)


def run_reminders(now: Optional[dt.datetime] = None) -> ReminderResult:
    """Create the due reminders (adds + flushes; the caller commits).  *now* is a naive UTC datetime."""
    now = now or security.now()
    local_now = jalali.to_iran_time(now, assume_utc=True)
    today = local_now.date()
    tomorrow = today + dt.timedelta(days=1)
    result = ReminderResult()

    horizon = today + dt.timedelta(days=60)  # unanswered invitations only matter for upcoming events
    assignments = _candidates(today, horizon)
    sent = _already_sent([a.id for a in assignments])

    def send(user_id: Optional[int], kind: str, assignment: Assignment, title: str, body: str, url: str) -> None:
        if user_id is None or (user_id, kind, assignment.id) in sent:
            return
        notifications.notify(user_id, kind, title, body, url, entity=assignment)
        sent.add((user_id, kind, assignment.id))
        result.sent[kind] += 1

    for assignment in assignments:
        event = assignment.event
        hall = event.hall.name if event.hall is not None else ""
        role = assignment.role.label
        call_time = assignment.call_time or event.start_time
        when = jalali.to_jalali_str(event.date, "weekday_long", persian_digits=True)
        detail = f"{role} - {hall} - {when}" + (f" - ساعت {_time_text(call_time)}" if call_time else "")
        committed = assignment.status in (AssignmentStatus.ACCEPTED, AssignmentStatus.CONFIRMED)
        manager_id = event.atelier.manager_id if event.atelier is not None else None

        if committed and event.date == tomorrow:
            send(
                assignment.person_user_id,
                KIND_DAY_BEFORE,
                assignment,
                "یادآوری: فردا مراسم دارید",
                detail,
                "/me",
            )
        if committed and event.date == today:
            start = dt.datetime.combine(today, call_time or MORNING, tzinfo=jalali.IRAN_TZ)
            lead = SAME_DAY_LEAD if call_time else dt.timedelta(0)
            if start - lead <= local_now <= start + SAME_DAY_GRACE:
                send(
                    assignment.person_user_id,
                    KIND_SAME_DAY,
                    assignment,
                    "یادآوری: امروز مراسم دارید",
                    detail,
                    "/me",
                )
        if assignment.status == AssignmentStatus.INVITED:
            age_ok = assignment.created_at is not None and now - assignment.created_at >= UNANSWERED_AFTER
            if age_ok:
                send(
                    manager_id,
                    KIND_UNANSWERED,
                    assignment,
                    f"{assignment.person_name} هنوز به دعوت پاسخ نداده است",
                    detail,
                    _event_url(event),
                )
            if event.date == tomorrow:
                send(
                    manager_id,
                    KIND_UNCONFIRMED,
                    assignment,
                    f"فردا مراسم است و {assignment.person_name} هنوز پاسخ نداده است",
                    detail,
                    _event_url(event),
                )
    db.session.flush()
    return result


def register_cli(app) -> None:
    """Add ``flask ateliers-reminders`` (called from the module's ``register`` hook)."""
    import click
    from flask.cli import with_appcontext

    @app.cli.command("ateliers-reminders")
    @with_appcontext
    def ateliers_reminders() -> None:
        """Send due call-time reminders and escalations (safe to run repeatedly)."""
        result = run_reminders()
        db.session.commit()
        click.echo("reminders sent: " + ", ".join(f"{kind}={count}" for kind, count in result.sent.items()))
