"""Call-time reminders and unanswered-invitation escalation: due times, recipients, idempotence, CLI."""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, day, now, world  # noqa: F401

from app.extensions import db
from app.models import Notification
from app.modules.ateliers import reminders
from app.services import staffing
from app.services.constants import AssignmentStatus as S


def utc(text):
    return dt.datetime.fromisoformat(text)


def notes(kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return db.session.scalars(stmt.order_by(Notification.id)).all()


def staffed(world, status, *, event=None, created="2025-07-01 08:00:00", **fields):
    assignment = staffing.assign_person(
        event or world.event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.manager, **fields
    )
    assignment.status = S(status)
    assignment.created_at = utc(created)
    db.session.commit()
    return assignment


def test_day_before_reminder_goes_to_committed_people_once(world):
    staffed(world, "confirmed")
    # event is 2025-07-17; the evening before in Iran time = 2025-07-16 15:00 UTC (18:30 local)
    result = reminders.run_reminders(utc("2025-07-16 15:00:00"))
    db.session.commit()
    assert result.sent[reminders.KIND_DAY_BEFORE] == 1
    (note,) = notes(reminders.KIND_DAY_BEFORE)
    assert note.user_id == world.staff.id and "فردا" in note.title and "تالار گل‌ها" in note.body and "عکاس" in note.body
    again = reminders.run_reminders(utc("2025-07-16 16:00:00"))
    db.session.commit()
    assert again.total == 0 and len(notes(reminders.KIND_DAY_BEFORE)) == 1  # idempotent


def test_nothing_is_sent_on_other_days(world):
    staffed(world, "confirmed")
    assert reminders.run_reminders(utc("2025-07-12 08:00:00")).total == 0
    assert reminders.run_reminders(utc("2025-07-18 08:00:00")).total == 0


def test_invited_people_get_no_call_reminder_but_the_manager_is_alerted(world):
    staffed(world, "invited")
    result = reminders.run_reminders(utc("2025-07-16 15:00:00"))
    db.session.commit()
    assert result.sent[reminders.KIND_DAY_BEFORE] == 0
    assert result.sent[reminders.KIND_UNCONFIRMED] == 1
    (alert,) = notes(reminders.KIND_UNCONFIRMED)
    assert alert.user_id == world.manager.id and "سارا نیرو" in alert.title and f"/atelier/events/{world.event.id}" in alert.url


def test_unanswered_invitation_escalates_after_48_hours(world):
    staffed(world, "invited", created="2025-07-10 08:00:00")
    assert reminders.run_reminders(utc("2025-07-11 20:00:00")).sent[reminders.KIND_UNANSWERED] == 0  # 36 h
    result = reminders.run_reminders(utc("2025-07-12 09:00:00"))  # 49 h
    db.session.commit()
    assert result.sent[reminders.KIND_UNANSWERED] == 1
    (alert,) = notes(reminders.KIND_UNANSWERED)
    assert alert.user_id == world.manager.id
    assert reminders.run_reminders(utc("2025-07-13 09:00:00")).sent[reminders.KIND_UNANSWERED] == 0  # only once


def test_same_day_reminder_waits_for_the_call_time_window(world):
    staffed(world, "confirmed", call_time=dt.time(18, 0))  # 18:00 Iran = 14:30 UTC on 2025-07-17
    assert reminders.run_reminders(utc("2025-07-17 05:00:00")).sent[reminders.KIND_SAME_DAY] == 0  # 08:30 local: too early
    result = reminders.run_reminders(utc("2025-07-17 11:00:00"))  # 14:30 local, 3.5 h before
    db.session.commit()
    assert result.sent[reminders.KIND_SAME_DAY] == 1
    (note,) = notes(reminders.KIND_SAME_DAY)
    assert "امروز" in note.title and "۱۸:۰۰" in note.body
    assert reminders.run_reminders(utc("2025-07-17 12:00:00")).sent[reminders.KIND_SAME_DAY] == 0


def test_same_day_reminder_without_a_call_time_goes_out_in_the_morning(world):
    staffed(world, "accepted")
    assert reminders.run_reminders(utc("2025-07-17 02:00:00")).sent[reminders.KIND_SAME_DAY] == 0  # 05:30 local
    assert reminders.run_reminders(utc("2025-07-17 04:00:00")).sent[reminders.KIND_SAME_DAY] == 1  # 07:30 local


def test_cancelled_events_and_people_who_declined_are_left_alone(world):
    declined = staffed(world, "declined")
    assert reminders.run_reminders(utc("2025-07-16 15:00:00")).total == 0
    declined.status = S.CONFIRMED
    db.session.commit()
    staffing.change_event_status(world.event, "cancelled", world.manager)
    db.session.commit()
    assert reminders.run_reminders(utc("2025-07-16 15:00:00")).total == 0


def test_roster_member_without_account_only_triggers_manager_alerts_never_errors(world):
    assignment = staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    result = reminders.run_reminders(utc("2025-07-16 15:00:00"))
    assert result.sent[reminders.KIND_DAY_BEFORE] == 0  # nobody to tell (A7), and no crash
    assert assignment.status == S.CONFIRMED


def test_cli_command_runs_and_commits(world, app):
    staffed(world, "confirmed")
    runner = app.test_cli_runner()
    from app.extensions import get_state
    from app.services.testing import FrozenClock

    get_state(app).clock = FrozenClock("2025-07-16 15:00:00")
    result = runner.invoke(args=["ateliers-reminders"])
    assert result.exit_code == 0, result.output
    assert "reminder_day_before=1" in result.output
    assert len(notes(reminders.KIND_DAY_BEFORE)) == 1
    again = runner.invoke(args=["ateliers-reminders"])
    assert "reminder_day_before=0" in again.output
