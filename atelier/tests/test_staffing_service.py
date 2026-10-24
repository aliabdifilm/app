"""Tests for app.services.staffing (assign, conflicts, transitions, summary)."""

import datetime as dt
import itertools
import os
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from flask import Flask

from app import models as m
from app.extensions import db
from app.services import constants as c
from app.services import eventbus, notifications, staffing
from app.services.constants import AssignmentStatus as S
from app.services.constants import EventStatus as E

DAY = dt.date(2025, 6, 20)
TERMS = {
    "base": 1_000_000,
    "commission_per_unit": 9_000,
    "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
}

# --------------------------------------------------------------------------
# fixtures / factories
# --------------------------------------------------------------------------


@pytest.fixture
def staff_app():
    flask_app = Flask(__name__)
    # ATELIER_TEST_DB_URL lets CI run these tests on PostgreSQL as well.
    flask_app.config.update(
        SQLALCHEMY_DATABASE_URI=os.environ.get("ATELIER_TEST_DB_URL", "sqlite://"),
        TESTING=True,
        SECRET_KEY="test",
    )
    db.init_app(flask_app)
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def bus():
    """Isolated event bus recording every published event as (name, payload)."""
    with eventbus.isolated():
        records = []
        for name in eventbus.KNOWN_EVENTS:
            eventbus.subscribe(name, lambda _n=name, **payload: records.append((_n, payload)))
        yield records


_seq = itertools.count(1)


def user(role="staff", status="active", **kwargs):
    n = next(_seq)
    obj = m.User(mobile=f"0935{n:07d}", full_name=f"کاربر {n}", role=role, status=status, **kwargs)
    db.session.add(obj)
    db.session.flush()
    return obj


def world(date=DAY, status="confirmed", **event_kwargs):
    """A hall + atelier (each with a manager), an admin and one event."""
    hall_manager = user("hall_manager")
    boss = user("atelier_manager")
    admin = user("admin")
    hall = m.Hall(manager=hall_manager, name="تالار آرمان", city="تهران")
    atelier = m.Atelier(manager=boss, name="اتلیه سیار", city="تهران")
    event_kwargs.setdefault("atelier", atelier)
    event = m.Event(hall=hall, date=date, status=status, **event_kwargs)
    db.session.add_all([hall, atelier, event])
    db.session.flush()
    return SimpleNamespace(
        event=event, hall=hall, atelier=atelier, hall_manager=hall_manager, boss=boss, admin=admin
    )


def second_event(w, date=DAY, **kwargs):
    event = m.Event(hall=w.hall, atelier=w.atelier, date=date, status="confirmed", **kwargs)
    db.session.add(event)
    db.session.flush()
    return event


def roster(w, name="نیروی ثابت", **kwargs):
    member = m.RosterMember(atelier=w.atelier, display_name=name, **kwargs)
    db.session.add(member)
    db.session.flush()
    return member


def assign(w, who, role="photographer", event=None, **kwargs):
    """assign_person as the atelier manager with sensible defaults."""
    kwargs.setdefault("terms", TERMS)
    kwargs.setdefault("actor", w.boss)
    key = "roster_member" if isinstance(who, m.RosterMember) else "user"
    return staffing.assign_person(event or w.event, role, **{key: who}, **kwargs)


def set_status(assignment, status):
    assignment.status = status
    db.session.flush()


def audit_actions(entity_type=None):
    stmt = sa.select(m.AuditLog.action).order_by(m.AuditLog.id)
    if entity_type:
        stmt = stmt.where(m.AuditLog.entity_type == entity_type)
    return list(db.session.scalars(stmt))


def notes_for(target):
    return list(
        db.session.scalars(
            sa.select(m.Notification).where(m.Notification.user_id == target.id).order_by(m.Notification.id)
        )
    )


# --------------------------------------------------------------------------
# assign_person
# --------------------------------------------------------------------------


def test_assign_user_snapshots_terms_audits_publishes_and_notifies(staff_app, bus):
    w = world()
    staff = user()
    a = assign(w, staff, call_time=dt.time(17, 30), notes="لباس رسمی")

    assert (a.status, a.source, a.role) == ("invited", "direct", "photographer")
    assert a.terms_dict() == TERMS
    assert (a.user_id, a.roster_member_id, a.created_by_id) == (staff.id, None, w.boss.id)
    assert a.call_time == dt.time(17, 30) and a.notes == "لباس رسمی"
    assert a in w.event.assignments

    assert audit_actions("assignments") == ["assignment.created"]
    log = db.session.scalar(sa.select(m.AuditLog))
    assert log.actor_id == w.boss.id and log.entity_id == a.id
    assert log.data["terms"] == TERMS and log.data["role"] == "photographer"

    assert bus == [
        (
            "assignment.changed",
            {
                "assignment_id": a.id,
                "event_id": w.event.id,
                "change": "created",
                "old_status": None,
                "new_status": "invited",
                "actor_id": w.boss.id,
                "user_id": staff.id,
            },
        )
    ]
    (note,) = notes_for(staff)
    assert note.kind == "assignment_invited" and "عکاس" in note.title
    assert note.entity_type == "assignments" and note.entity_id == a.id


def test_assign_roster_member_without_account_is_confirmed_and_silent(staff_app, bus):
    w = world()
    member = roster(w, "علی ثابت", mobile="09121110000")
    a = assign(w, member, role="seller")
    assert (a.status, a.source, a.user_id, a.roster_member_id) == ("confirmed", "roster", None, member.id)
    assert a.person_name == "علی ثابت"
    assert db.session.scalar(sa.select(sa.func.count(m.Notification.id))) == 0  # nobody to tell
    assert bus[0][1]["user_id"] is None


def test_assign_roster_member_with_account_notifies_that_account(staff_app):
    w = world()
    account = user()
    member = roster(w, user=account)
    a = assign(w, member, role="editor")
    assert a.status == "confirmed"
    (note,) = notes_for(account)
    assert note.kind == "assignment_changed"


def test_assign_accepts_payroll_pay_terms_objects(staff_app):
    payroll = pytest.importorskip("app.services.payroll")
    w = world()
    terms = payroll.PayTerms(
        base=2_000_000, commission_per_unit=7000, bonus_tiers=(payroll.BonusTier(50, 80_000),)
    )
    a = assign(w, user(), terms=terms)
    assert a.pay_terms() == terms
    assert a.terms_dict() == payroll.terms_to_dict(terms)


def test_assign_requires_valid_terms(staff_app):
    w = world()
    staff = user()
    with pytest.raises(TypeError):
        assign(w, staff, terms=None)
    with pytest.raises(ValueError):
        assign(w, staff, terms={"base": -1})
    with pytest.raises(TypeError):
        assign(w, staff, terms={"base": 1.5})
    with pytest.raises(ValueError):
        assign(w, staff, terms={"base": 1, "bonus_tiers": [{"threshold": 5, "amount": 1}] * 2})
    assert db.session.scalar(sa.select(sa.func.count(m.Assignment.id))) == 0


def test_terms_keys_default_to_zero_and_base_pay_is_an_alias(staff_app):
    w = world()
    a = assign(w, user(), terms={"base_pay": "۲٬۵۰۰٬۰۰۰"})
    assert a.terms_dict() == {"base": 2_500_000, "commission_per_unit": 0, "bonus_tiers": []}


def test_assign_requires_exactly_one_person_and_a_valid_role(staff_app):
    w = world()
    staff, member = user(), roster(w)
    with pytest.raises(ValueError):
        staffing.assign_person(w.event, "seller", terms=TERMS, actor=w.boss)
    with pytest.raises(ValueError):
        staffing.assign_person(w.event, "seller", user=staff, roster_member=member, terms=TERMS, actor=w.boss)
    with pytest.raises(ValueError):
        assign(w, staff, role="juggler")


def test_assign_accepts_ids(staff_app):
    w = world()
    staff = user()
    a = staffing.assign_person(w.event.id, "editor", user=staff.id, terms=TERMS, actor=w.boss.id)
    assert a.user_id == staff.id
    with pytest.raises(staffing.StaffingError):
        staffing.assign_person(w.event.id, "editor", user=987654, terms=TERMS, actor=w.boss.id)


def test_event_without_atelier_or_in_closed_state_cannot_be_staffed(staff_app):
    w = world(status="open", atelier=None)
    with pytest.raises(staffing.EventNotStaffable):
        assign(w, user(), actor=w.admin)  # (only an admin may even try without an atelier)
    for status in ("done", "invoiced", "cancelled"):
        closed = world(status=status)
        with pytest.raises(staffing.EventNotStaffable):
            assign(closed, user())


def test_roster_member_must_belong_to_the_events_atelier_and_be_active(staff_app):
    w, other = world(), world()
    foreign = roster(other)
    with pytest.raises(staffing.StaffingError):
        assign(w, foreign)
    inactive = roster(w, is_active=False)
    with pytest.raises(staffing.StaffingError):
        assign(w, inactive)


def test_suspended_users_cannot_be_assigned(staff_app):
    w = world()
    with pytest.raises(staffing.StaffingError):
        assign(w, user(status="suspended"))


def test_duplicate_role_for_same_person_is_always_refused(staff_app):
    w = world()
    staff = user()
    assign(w, staff, role="photographer")
    with pytest.raises(staffing.DuplicateAssignment) as exc:
        assign(w, staff, role="photographer")
    assert len(exc.value.existing) == 1
    with pytest.raises(staffing.DuplicateAssignment):
        assign(w, staff, role="photographer", allow_conflict=True)  # never overridable


def test_duplicate_detection_sees_through_roster_link_and_mobile(staff_app):
    w = world()
    account = user()
    member = roster(w, user=account)
    assign(w, member, role="editor")
    with pytest.raises(staffing.DuplicateAssignment):
        assign(w, account, role="editor")  # same human via the account
    twin = roster(w, "تکراری", mobile=account.mobile)
    with pytest.raises(staffing.DuplicateAssignment):
        assign(w, twin, role="editor")  # same mobile number


def test_declined_assignment_does_not_block_a_new_one(staff_app):
    w = world()
    staff = user()
    first = assign(w, staff)
    staffing.transition(first, "declined", staff)
    again = assign(w, staff)
    assert again.id != first.id and again.status == "invited"


def test_same_person_may_hold_two_roles_on_one_event(staff_app):
    w = world()
    staff = user()
    assign(w, staff, role="photographer")
    assign(w, staff, role="seller")  # same event: not a double booking
    assert len(w.event.assignments) == 2


def test_double_booking_on_the_same_date_is_refused_unless_overridden(staff_app):
    w = world()
    staff = user()
    other_event = second_event(w)
    first = assign(w, staff)
    with pytest.raises(staffing.AssignmentConflict) as exc:
        assign(w, staff, role="seller", event=other_event)
    assert exc.value.conflicts == [first]
    forced = assign(w, staff, role="seller", event=other_event, allow_conflict=True)
    assert forced.event_id == other_event.id
    log = db.session.scalar(sa.select(m.AuditLog).where(m.AuditLog.entity_id == forced.id))
    assert log.data["override_conflict"] is True


def test_different_dates_and_freed_assignments_do_not_conflict(staff_app):
    w = world()
    staff = user()
    tomorrow = second_event(w, date=DAY + dt.timedelta(days=1))
    assign(w, staff)
    assign(w, staff, role="seller", event=tomorrow)  # other day: fine

    same_day = second_event(w)
    for status in ("declined", "cancelled"):
        busy = user()
        a = assign(w, busy)
        set_status(a, status)
        assign(w, busy, role="seller", event=same_day)  # freed person is bookable again


def test_cancelled_events_never_cause_conflicts(staff_app):
    w = world()
    staff = user()
    cancelled = second_event(w)
    a = assign(w, staff, event=cancelled)
    cancelled.status = "cancelled"  # (without cascade, to prove the event filter itself)
    db.session.flush()
    assert staffing.conflicts_for(DAY, user_id=staff.id) == []
    assert a.status == "invited"
    assign(w, staff)  # allowed


def test_conflicts_see_other_ateliers_too(staff_app):
    w1, w2 = world(), world()
    freelancer = user()
    assign(w1, freelancer)
    with pytest.raises(staffing.AssignmentConflict):
        assign(w2, freelancer)


def test_conflict_between_a_roster_entry_and_its_account(staff_app):
    w = world()
    account = user()
    member = roster(w, user=account)
    other_event = second_event(w)
    assign(w, member, role="editor")  # via roster
    with pytest.raises(staffing.AssignmentConflict):
        assign(w, account, role="seller", event=other_event)  # via the account
    with pytest.raises(staffing.AssignmentConflict):
        assign(w, roster(w, "همان آدم", mobile=account.mobile), role="seller", event=other_event)


def test_resolve_identity_expands_links_and_mobiles(staff_app):
    w = world()
    account = user()
    linked = roster(w, user=account)
    by_mobile = roster(w, mobile=account.mobile)
    stranger = roster(w, mobile="09190000000")
    ident = staffing.resolve_identity(user_id=account.id)
    assert ident.user_ids == {account.id}
    assert ident.roster_ids == {linked.id, by_mobile.id}
    assert account.mobile in ident.mobiles
    assert stranger.id not in ident.roster_ids
    # starting from a roster entry finds the same cluster
    again = staffing.resolve_identity(roster_member_id=linked.id)
    assert again.user_ids == {account.id} and again.roster_ids == ident.roster_ids


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------


def test_only_the_atelier_manager_admin_or_system_may_assign(staff_app):
    w = world()
    stranger_boss = user("atelier_manager")
    other_world = world()
    staff = user()
    for actor in (w.hall_manager, staff, stranger_boss, other_world.boss, user("hall_manager")):
        with pytest.raises(staffing.StaffingPermissionError):
            assign(w, staff, actor=actor)
    assert assign(w, staff, actor=w.admin).created_by_id == w.admin.id
    assert assign(w, user(), actor=w.boss.id) is not None  # id works
    system = assign(w, user(), actor=None)
    assert system.created_by_id is None
    assert "assignment.created" in audit_actions()


def test_anonymous_and_suspended_actors_are_refused(staff_app):
    w = world()
    anonymous = SimpleNamespace(is_authenticated=False, id=None)
    with pytest.raises(staffing.StaffingPermissionError):
        assign(w, user(), actor=anonymous)
    w.boss.status = "suspended"
    db.session.flush()
    with pytest.raises(staffing.StaffingPermissionError):
        assign(w, user(), actor=w.boss)


def test_flask_login_style_proxy_actor_is_accepted(staff_app):
    from werkzeug.local import LocalProxy

    w = world()
    staff = user()
    a = assign(w, staff)
    current_user = LocalProxy(lambda: staff)  # what flask_login.current_user looks like
    assert staffing.transition(a, "accepted", current_user).status == "accepted"
    log = db.session.scalar(sa.select(m.AuditLog).order_by(m.AuditLog.id.desc()))
    assert log.actor_id == staff.id


def test_check_permissions_can_be_disabled_for_trusted_callers(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff, actor=staff, check_permissions=False)
    assert a.created_by_id == staff.id  # still audited under the real actor


# --------------------------------------------------------------------------
# conflicts_for / check_assignable
# --------------------------------------------------------------------------


def test_conflicts_for_arguments_and_filters(staff_app):
    w = world()
    staff = user()
    morning = second_event(w, start_time=dt.time(10, 0))
    evening = second_event(w, start_time=dt.time(20, 0))
    a_evening = assign(w, staff, role="seller", event=evening, allow_conflict=True)
    a_morning = assign(w, staff, role="editor", event=morning, allow_conflict=True)
    with pytest.raises(ValueError):
        staffing.conflicts_for(DAY)
    assert staffing.conflicts_for(DAY, user_id=staff.id) == [a_morning, a_evening]  # by start time
    assert staffing.conflicts_for(DAY.isoformat(), user_id=staff.id) == [a_morning, a_evening]
    assert staffing.conflicts_for(dt.datetime(2025, 6, 20, 3, 0), user_id=staff.id) == [a_morning, a_evening]
    assert staffing.conflicts_for(DAY, user_id=staff.id, exclude_assignment_id=a_morning.id) == [a_evening]
    assert staffing.conflicts_for(DAY, user_id=staff.id, exclude_event_id=evening.id) == [a_morning]
    assert staffing.conflicts_for(DAY + dt.timedelta(days=1), user_id=staff.id) == []
    set_status(a_morning, "accepted")
    set_status(a_evening, "completed")
    assert staffing.conflicts_for(
        DAY, user_id=staff.id, statuses=c.COMMITTED_ASSIGNMENT_STATUSES
    ) == [a_morning, a_evening]
    set_status(a_morning, "invited")
    assert staffing.conflicts_for(
        DAY, user_id=staff.id, statuses=c.COMMITTED_ASSIGNMENT_STATUSES
    ) == [a_evening]
    with pytest.raises(TypeError):
        staffing.conflicts_for(12345, user_id=staff.id)


def test_conflicts_for_a_roster_member_without_account(staff_app):
    w = world()
    member = roster(w)
    a = assign(w, member, role="seller")
    other = second_event(w)
    assert staffing.conflicts_for(DAY, roster_member_id=member.id) == [a, ]
    with pytest.raises(staffing.AssignmentConflict):
        assign(w, member, role="editor", event=other)


def test_check_assignable_reports_everything_without_writing(staff_app):
    w = world()
    staff = user()
    other = second_event(w)
    existing = assign(w, staff, event=other)
    db.session.add(m.StaffAvailability(user=staff, date=DAY, note="مسافرت"))
    db.session.flush()
    before = db.session.scalar(sa.select(sa.func.count(m.Assignment.id)))

    check = staffing.check_assignable(w.event, "seller", user=staff)
    assert not check.ok and not check.blocking
    assert check.conflicts == [existing] and check.duplicates == []
    assert check.unavailable.note == "مسافرت"

    clash = staffing.check_assignable(other, "photographer", user=staff)
    assert clash.duplicates == [existing] and clash.blocking

    fresh = staffing.check_assignable(w.event, "seller", user=user())
    assert fresh.ok
    available_again = user()
    db.session.add(m.StaffAvailability(user=available_again, date=DAY, status="available"))
    assert staffing.check_assignable(w.event, "seller", user=available_again).ok
    assert db.session.scalar(sa.select(sa.func.count(m.Assignment.id))) == before
    with pytest.raises(ValueError):
        staffing.check_assignable(w.event, "seller")


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------

ALL_PAIRS = [(old, new) for old in S for new in S if old != new]


@pytest.mark.parametrize("old,new", ALL_PAIRS, ids=[f"{o.value}->{n.value}" for o, n in ALL_PAIRS])
def test_transition_table_is_enforced_for_every_pair(staff_app, old, new):
    w = world()
    a = assign(w, user())
    set_status(a, old)
    if new in c.ASSIGNMENT_TRANSITIONS[old]:
        assert staffing.transition(a, new, w.boss).status == new
    else:
        with pytest.raises(staffing.InvalidTransition) as exc:
            staffing.transition(a, new, w.boss)
        assert (exc.value.old, exc.value.new) == (old.value, new.value)
        assert a.status == old
    assert staffing.can_transition(old, new) == (new in c.ASSIGNMENT_TRANSITIONS[old])


def test_same_status_is_an_idempotent_noop(staff_app, bus):
    w = world()
    a = assign(w, user())
    bus.clear()
    audits = len(audit_actions())
    assert staffing.transition(a, "invited", w.boss) is a
    assert bus == [] and len(audit_actions()) == audits


def test_accept_and_decline_by_the_assignee(staff_app, bus):
    w = world()
    staff = user()
    a = assign(w, staff)
    bus.clear()
    staffing.transition(a, "accepted", staff, reason="با کمال میل")
    assert a.status == "accepted" and a.responded_at is not None
    assert audit_actions("assignments")[-1] == "assignment.accepted"
    log = db.session.scalar(sa.select(m.AuditLog).order_by(m.AuditLog.id.desc()))
    assert log.actor_id == staff.id
    assert log.data == {"from": "invited", "to": "accepted", "reason": "با کمال میل"}
    assert bus == [
        (
            "assignment.changed",
            {
                "assignment_id": a.id, "event_id": w.event.id, "change": "status",
                "old_status": "invited", "new_status": "accepted",
                "actor_id": staff.id, "user_id": staff.id,
            },
        )
    ]
    # the boss is told, the actor is not notified about their own action
    boss_notes = notes_for(w.boss)
    assert [n.kind for n in boss_notes] == ["assignment_response"]
    assert "پذیرفت" in boss_notes[0].title
    assert [n.kind for n in notes_for(staff)] == ["assignment_invited"]


def test_decline_notifies_the_manager(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    staffing.transition(a, "declined", staff)
    assert a.responded_at is not None
    assert "رد" in notes_for(w.boss)[-1].title


def test_manager_confirmation_notifies_the_assignee(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    staffing.transition(a, "accepted", staff)
    staffing.transition(a, "confirmed", w.boss)
    kinds = [n.kind for n in notes_for(staff)]
    assert kinds == ["assignment_invited", "assignment_changed"]
    assert "قطعی" in notes_for(staff)[-1].title


def test_assignee_may_only_accept_decline_or_withdraw(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.transition(a, "confirmed", staff)  # cannot self-confirm
    staffing.transition(a, "accepted", staff)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.transition(a, "completed", staff)
    staffing.transition(a, "cancelled", staff)  # withdrawing is allowed
    assert a.status == "cancelled"


def test_others_cannot_change_an_assignment(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    bystander = user()
    other_boss = world().boss
    for actor in (bystander, other_boss, w.hall_manager):
        with pytest.raises(staffing.StaffingPermissionError):
            staffing.transition(a, "accepted", actor)
    assert staffing.transition(a, "accepted", w.admin).status == "accepted"
    assert staffing.transition(a, "confirmed", None).status == "confirmed"  # system


def test_roster_account_can_answer_its_own_assignment(staff_app):
    w = world()
    account = user()
    member = roster(w, user=account)
    a = assign(w, member, role="editor", status="invited")
    assert staffing.transition(a, "accepted", account).status == "accepted"


def test_accepting_checks_committed_clashes_only(staff_app):
    w = world()
    staff = user()
    event_b = second_event(w)
    first = assign(w, staff)
    second = assign(w, staff, role="seller", event=event_b, allow_conflict=True)
    # both are only invited: accepting the first is fine...
    staffing.transition(first, "accepted", staff)
    # ...but accepting the second now clashes with a committed one
    with pytest.raises(staffing.AssignmentConflict) as exc:
        staffing.transition(second, "accepted", staff)
    assert exc.value.conflicts == [first]
    assert second.status == "invited"
    assert staffing.transition(second, "accepted", staff, allow_conflict=True).status == "accepted"


def test_reinviting_a_decliner_runs_the_assignment_checks(staff_app):
    w = world()
    staff = user()
    other_event = second_event(w)
    first = assign(w, staff)
    staffing.transition(first, "declined", staff)
    assert staffing.transition(first, "invited", w.boss).status == "invited"  # plain re-invite

    staffing.transition(first, "declined", staff)
    replacement = assign(w, staff)  # a fresh active assignment for the same role
    with pytest.raises(staffing.DuplicateAssignment):
        staffing.transition(first, "invited", w.boss)  # would create two active rows
    staffing.transition(replacement, "cancelled", w.boss)

    busy = assign(w, staff, role="seller", event=other_event, allow_conflict=True)  # same date, other event
    with pytest.raises(staffing.AssignmentConflict):
        staffing.transition(first, "invited", w.boss)
    assert staffing.transition(first, "invited", w.boss, allow_conflict=True).status == "invited"
    assert busy.status == "invited"


def test_confirming_an_already_committed_assignment_does_not_recheck(staff_app):
    w = world()
    staff = user()
    event_b = second_event(w)
    a = assign(w, staff)
    b = assign(w, staff, role="seller", event=event_b, allow_conflict=True)
    staffing.transition(a, "accepted", staff)
    staffing.transition(b, "accepted", staff, allow_conflict=True)
    assert staffing.transition(b, "confirmed", w.boss).status == "confirmed"  # accepted -> confirmed


def test_transitions_on_a_cancelled_event_are_refused(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    w.event.status = "cancelled"
    db.session.flush()
    with pytest.raises(staffing.EventNotStaffable):
        staffing.transition(a, "accepted", staff)
    assert staffing.transition(a, "cancelled", w.boss).status == "cancelled"


def test_locked_assignment_cannot_be_undone_until_unlocked(staff_app, bus):
    w = world()
    staff = user()
    a = assign(w, staff)
    staffing.transition(a, "accepted", staff)
    staffing.transition(a, "confirmed", w.boss)
    staffing.lock_terms(a, w.boss)
    with pytest.raises(staffing.TermsLocked):
        staffing.transition(a, "cancelled", w.boss)
    assert staffing.transition(a, "completed", w.boss).status == "completed"  # moving on is fine
    b = assign(w, user(), role="seller")
    set_status(b, "confirmed")
    staffing.lock_terms(b)
    with pytest.raises(staffing.TermsLocked):
        staffing.transition(b, "cancelled", w.boss)
    staffing.unlock_terms(b, w.boss)
    assert staffing.transition(b, "cancelled", w.boss).status == "cancelled"


def test_services_never_commit(staff_app):
    w = world()
    staff = user()
    db.session.commit()  # persist the fixtures
    a = assign(w, staff)
    staffing.transition(a, "accepted", staff)
    staffing.set_terms(a, {**TERMS, "base": 5}, w.boss)
    db.session.rollback()  # the caller's rollback must undo everything
    assert db.session.scalar(sa.select(sa.func.count(m.Assignment.id))) == 0
    assert db.session.scalar(sa.select(sa.func.count(m.AuditLog.id))) == 0
    assert db.session.scalar(sa.select(sa.func.count(m.Notification.id))) == 0


# --------------------------------------------------------------------------
# terms: set / lock / unlock
# --------------------------------------------------------------------------


def test_set_terms_replaces_snapshot_with_audit_event_and_notification(staff_app, bus):
    w = world()
    staff = user()
    a = assign(w, staff)
    other = assign(w, user(), role="seller")
    bus.clear()
    new_terms = {"base": 1_200_000, "commission_per_unit": 10_000, "bonus_tiers": [(60, 100_000)]}
    staffing.set_terms(a, new_terms, w.boss, reason="توافق جدید")
    assert a.terms_dict() == {
        "base": 1_200_000, "commission_per_unit": 10_000,
        "bonus_tiers": [{"threshold": 60, "amount": 100_000}],
    }
    assert other.terms_dict() == TERMS  # nobody else is touched
    log = db.session.scalar(sa.select(m.AuditLog).where(m.AuditLog.action == "assignment.terms_changed"))
    assert log.data["old"] == TERMS and log.data["reason"] == "توافق جدید"
    assert log.data["new"]["base"] == 1_200_000
    assert [(n, p["change"]) for n, p in bus] == [("assignment.changed", "terms")]
    assert notes_for(staff)[-1].title == "شرایط پرداخت شما تغییر کرد"


def test_set_terms_with_identical_terms_is_a_noop(staff_app, bus):
    w = world()
    a = assign(w, user())
    bus.clear()
    count = len(audit_actions())
    staffing.set_terms(a, dict(TERMS), w.boss)
    assert bus == [] and len(audit_actions()) == count


def test_set_terms_permissions_and_state_rules(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.set_terms(a, {"base": 1}, staff)  # the assignee cannot raise their own pay
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.set_terms(a, {"base": 1}, w.hall_manager)
    staffing.set_terms(a, {"base": 1}, w.admin)
    with pytest.raises(ValueError):
        staffing.set_terms(a, {"base": -1}, w.boss)
    assert a.base_pay == 1  # invalid input left the snapshot alone
    set_status(a, "cancelled")
    with pytest.raises(staffing.StaffingError):
        staffing.set_terms(a, {"base": 2}, w.boss)


def test_lock_terms_blocks_every_edit_path_and_is_idempotent(staff_app, bus):
    w = world()
    a = assign(w, user())
    bus.clear()
    staffing.lock_terms(a, w.boss)
    locked_at = a.terms_locked_at
    assert locked_at is not None and a.is_terms_locked
    staffing.lock_terms(a, w.boss)  # idempotent
    assert a.terms_locked_at == locked_at
    assert audit_actions().count("assignment.terms_locked") == 1
    assert [(n, p["change"]) for n, p in bus] == [("assignment.changed", "locked")]

    with pytest.raises(staffing.TermsLocked):
        staffing.set_terms(a, {"base": 9}, w.boss)
    assert issubclass(staffing.TermsLocked, m.TermsLockedError)
    assert issubclass(staffing.TermsLocked, staffing.StaffingError)
    db.session.commit()
    a.commission_per_unit = 1  # sneaking around the service hits the ORM guard
    with pytest.raises(m.TermsLockedError):
        db.session.flush()
    db.session.rollback()
    assert db.session.get(m.Assignment, a.id).commission_per_unit == 9_000


def test_stale_in_memory_copy_cannot_bypass_a_lock(staff_app):
    w = world()
    a = assign(w, user())
    db.session.commit()
    assert a.terms_locked_at is None  # loaded: looks unlocked
    db.session.execute(
        sa.text("UPDATE assignments SET terms_locked_at = '2025-01-01 00:00:00' WHERE id = :i"),
        {"i": a.id},
    )  # another request locks it behind our back
    a.base_pay = 1
    with pytest.raises(m.TermsLockedError):
        db.session.flush()
    db.session.rollback()


def test_only_managers_may_lock_terms(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.lock_terms(a, staff)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.lock_terms(a, w.hall_manager)
    assert not a.is_terms_locked
    staffing.lock_terms(a, w.admin)
    assert a.is_terms_locked


def test_lock_event_terms_locks_only_committed_assignments(staff_app):
    w = world()
    accepted, invited, declined = assign(w, user()), assign(w, user(), role="seller"), assign(w, user(), role="editor")
    set_status(accepted, "accepted")
    set_status(declined, "declined")
    locked = staffing.lock_event_terms(w.event, w.boss)
    assert locked == [accepted]
    assert accepted.is_terms_locked and not invited.is_terms_locked and not declined.is_terms_locked


def test_unlock_requires_a_manager_and_reenables_edits(staff_app):
    w = world()
    staff = user()
    a = assign(w, staff)
    staffing.lock_terms(a)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.unlock_terms(a, staff)
    staffing.unlock_terms(a, w.boss, reason="اصلاح فاکتور")
    assert not a.is_terms_locked
    staffing.set_terms(a, {"base": 5}, w.boss)
    assert a.base_pay == 5
    assert audit_actions().count("assignment.terms_unlocked") == 1


# --------------------------------------------------------------------------
# staffing_summary
# --------------------------------------------------------------------------


def test_summary_of_an_empty_event(staff_app):
    w = world()
    summary = staffing.staffing_summary(w.event)
    assert list(summary.roles) == ["presenter", "photographer", "seller", "editor"]
    assert (summary.needed_total, summary.filled_total, summary.missing_total) == (4, 0, 4)
    assert not summary.is_fully_staffed
    assert summary["photographer"].missing == 1 and summary["photographer"].label == "عکاس"


def test_summary_counts_filled_waiting_and_missing(staff_app):
    w = world(staffing_needs={"photographer": 2, "presenter": 1, "seller": 1, "editor": 1})
    p1, p2 = user(), user()
    a1, a2 = assign(w, p1), assign(w, p2)
    s = assign(w, user(), role="seller")
    gone = assign(w, user(), role="editor")
    set_status(a1, "confirmed")  # filled
    set_status(a2, "invited")  # waiting
    set_status(s, "completed")  # filled
    set_status(gone, "declined")  # ignored
    summary = staffing.staffing_summary(w.event)
    photo = summary["photographer"]
    assert (photo.needed, photo.filled, photo.invited, photo.missing, photo.shortfall) == (2, 1, 1, 0, 1)
    assert summary["seller"].is_filled and summary["seller"].missing == 0
    editor = summary["editor"]
    assert (editor.filled, editor.invited, editor.missing) == (0, 0, 1)  # declined does not count
    assert summary["presenter"].missing == 1
    assert (summary.needed_total, summary.filled_total, summary.invited_total) == (5, 2, 1)
    assert (summary.missing_total, summary.shortfall_total) == (2, 3)
    assert not summary.is_fully_staffed
    assert [a.id for a in photo.assignments] == [a1.id, a2.id]


def test_summary_fully_staffed_surplus_and_serialisation(staff_app):
    w = world(staffing_needs={"photographer": 1, "presenter": 0, "seller": 0, "editor": 0})
    a, b = assign(w, user()), assign(w, user())
    set_status(a, "accepted")
    set_status(b, "accepted")
    summary = staffing.staffing_summary(w.event)
    assert summary.is_fully_staffed and summary["photographer"].surplus == 1
    assert summary.filled_total == 1  # capped by need
    data = summary.to_dict()
    assert data["is_fully_staffed"] is True and data["event_id"] == w.event.id
    assert data["roles"][1]["role"] == "photographer"
    assert data["roles"][1]["assignment_ids"] == [a.id, b.id]


def test_summary_works_for_unsaved_events_and_unlisted_roles(staff_app):
    event = m.Event(staffing_needs={"photographer": 2})
    summary = staffing.staffing_summary(event)
    assert summary.event_id is None
    assert (summary["photographer"].needed, summary["seller"].needed) == (2, 0)
    assert summary["seller"].is_filled  # nothing needed, nothing missing


# --------------------------------------------------------------------------
# event life cycle
# --------------------------------------------------------------------------

EVENT_PAIRS = [(o, n) for o in E for n in E if o != n]


@pytest.mark.parametrize("old,new", EVENT_PAIRS, ids=[f"{o.value}->{n.value}" for o, n in EVENT_PAIRS])
def test_event_transition_table_is_enforced(staff_app, old, new):
    w = world(status=old.value)
    if new in c.EVENT_TRANSITIONS[old]:
        staffing.change_event_status(w.event, new, w.admin)
        assert w.event.status == new
    else:
        with pytest.raises(staffing.InvalidTransition):
            staffing.change_event_status(w.event, new, w.admin)
        assert w.event.status == old
    assert staffing.can_change_event_status(old, new) == (new in c.EVENT_TRANSITIONS[old])


def test_event_status_change_audits_publishes_and_notifies_managers(staff_app, bus):
    w = world(status="open")
    staffing.change_event_status(w.event, "confirmed", w.boss, reason="قرارداد امضا شد")
    assert audit_actions("events") == ["event.confirmed"]
    assert bus == [
        (
            "event.status_changed",
            {"event_id": w.event.id, "old_status": "open", "new_status": "confirmed", "actor_id": w.boss.id},
        )
    ]
    assert notes_for(w.boss) == []  # the actor is not told about their own action
    (note,) = notes_for(w.hall_manager)
    assert note.kind == "event_status" and "قطعی" in note.title


def test_event_status_same_value_is_a_noop(staff_app, bus):
    w = world()
    staffing.change_event_status(w.event, "confirmed", w.boss)
    assert bus == [] and audit_actions() == []


def test_event_status_permissions(staff_app):
    w = world(status="open")
    outsider = user()
    for actor in (outsider, world().boss, world().hall_manager):
        with pytest.raises(staffing.StaffingPermissionError):
            staffing.change_event_status(w.event, "cancelled", actor)
    staffing.change_event_status(w.event, "cancelled", w.hall_manager)  # hall manager may
    staffing.change_event_status(w.event, "draft", w.admin)
    staffing.change_event_status(w.event, "open", None)


def test_confirming_requires_an_atelier(staff_app):
    w = world(status="open", atelier=None)
    with pytest.raises(staffing.StaffingError):
        staffing.change_event_status(w.event, "confirmed", w.admin)
    assert w.event.status == "open"


def test_cancelling_an_event_cancels_active_assignments(staff_app, bus):
    w = world()
    invited, accepted, confirmed, declined, done = (assign(w, user(), role=r) for r in (
        "photographer", "seller", "editor", "presenter", "presenter"))
    set_status(accepted, "accepted")
    set_status(confirmed, "confirmed")
    set_status(declined, "declined")
    set_status(done, "completed")
    bus.clear()
    staffing.change_event_status(w.event, "cancelled", w.boss, reason="انصراف عروس")
    assert [a.status for a in (invited, accepted, confirmed)] == ["cancelled"] * 3
    assert declined.status == "declined" and done.status == "completed"
    names = [n for n, _ in bus]
    assert names.count("assignment.changed") == 3 and names[-1] == "event.status_changed"
    for person in (invited.user, accepted.user, confirmed.user):
        assert any("لغو" in n.title for n in notes_for(person))
    assert not any("لغو" in n.title for n in notes_for(declined.user))
    with pytest.raises(staffing.EventNotStaffable):
        assign(w, user(), role="seller")


def test_event_done_completes_working_assignments_and_drops_unanswered_invitations(staff_app):
    w = world()
    waiting = assign(w, user())
    accepted = assign(w, user(), role="seller")
    confirmed = assign(w, roster(w), role="editor")
    set_status(accepted, "accepted")
    staffing.change_event_status(w.event, "done", w.boss)
    assert (waiting.status, accepted.status, confirmed.status) == ("cancelled", "completed", "completed")


def test_handing_the_event_back_clears_the_atelier_and_staff(staff_app):
    w = world()
    a = assign(w, user())
    staffing.change_event_status(w.event, "open", w.boss)
    assert w.event.atelier_id is None and w.event.status == "open"
    assert a.status == "cancelled"
    assert any(n.kind == "event_status" for n in notes_for(w.boss)) is False  # the actor
    assert any(n.kind == "event_status" for n in notes_for(w.hall_manager))


def test_locked_assignments_veto_cancelling_the_event(staff_app, bus):
    w = world()
    a = assign(w, user())
    set_status(a, "confirmed")
    staffing.lock_terms(a)
    bus.clear()
    with pytest.raises(staffing.TermsLocked):
        staffing.change_event_status(w.event, "cancelled", w.boss)
    assert w.event.status == "confirmed" and a.status == "confirmed"
    assert bus == []
    staffing.change_event_status(w.event, "done", w.boss)  # finishing is fine
    assert a.status == "completed"


# --------------------------------------------------------------------------
# equipment on events
# --------------------------------------------------------------------------


def gear(w, name="دوربین کرایه‌ای", ownership="rented", rent=400_000, **kwargs):
    item = m.Equipment(
        atelier=w.atelier, name=name, kind="camera", ownership=ownership,
        rent_cost_per_event=rent, **kwargs,
    )
    db.session.add(item)
    db.session.flush()
    return item


def test_attach_equipment_snapshots_rent_audits_and_publishes(staff_app, bus):
    w = world()
    camera = gear(w)
    usage = staffing.attach_equipment(w.event, camera, w.boss)
    assert usage.rent_cost_snapshot == 400_000
    camera.rent_cost_per_event = 900_000
    db.session.flush()
    assert usage.rent_cost_snapshot == 400_000
    assert audit_actions("equipment_usages") == ["equipment.attached"]
    assert bus == [
        (
            "equipment.assigned",
            {
                "event_id": w.event.id, "equipment_id": camera.id, "usage_id": usage.id,
                "actor_id": w.boss.id, "removed": False,
            },
        )
    ]


def test_attach_equipment_is_idempotent_and_free_for_owned_gear(staff_app, bus):
    w = world()
    camera = gear(w, ownership="owned", rent=123)
    first = staffing.attach_equipment(w.event, camera, w.boss)
    bus.clear()
    assert staffing.attach_equipment(w.event, camera, w.boss) is first
    assert bus == []
    assert first.rent_cost_snapshot == 0


def test_attach_equipment_rules(staff_app):
    w, other = world(), world()
    camera = gear(w)
    with pytest.raises(staffing.StaffingPermissionError):
        staffing.attach_equipment(w.event, camera, user())
    with pytest.raises(staffing.StaffingError):
        staffing.attach_equipment(w.event, gear(other), w.boss)  # someone else's gear
    with pytest.raises(staffing.StaffingError):
        staffing.attach_equipment(w.event, gear(w, name="خراب", is_active=False), w.boss)
    no_atelier = world(status="open", atelier=None)
    with pytest.raises(staffing.EventNotStaffable):
        staffing.attach_equipment(no_atelier.event, camera, no_atelier.admin)
    w.event.status = "invoiced"
    with pytest.raises(staffing.EventNotStaffable):
        staffing.attach_equipment(w.event, camera, w.boss)
    w.event.status = "done"
    assert staffing.attach_equipment(w.event, camera, w.boss) is not None  # still allowed after the event


def test_detach_equipment(staff_app, bus):
    w = world()
    camera = gear(w)
    assert staffing.detach_equipment(w.event, camera, w.boss) is False  # never attached
    usage = staffing.attach_equipment(w.event, camera, w.boss)
    usage_id = usage.id
    bus.clear()
    assert staffing.detach_equipment(w.event, camera, w.boss) is True
    assert db.session.get(m.EquipmentUsage, usage_id) is None
    assert bus == [
        (
            "equipment.assigned",
            {
                "event_id": w.event.id, "equipment_id": camera.id, "usage_id": usage_id,
                "actor_id": w.boss.id, "removed": True,
            },
        )
    ]
    assert audit_actions("equipment_usages") == ["equipment.attached", "equipment.detached"]


def test_detach_is_refused_once_the_invoice_is_submitted(staff_app):
    w = world()
    camera = gear(w)
    staffing.attach_equipment(w.event, camera, w.boss)
    invoice = m.Invoice(event=w.event, atelier=w.atelier, status="submitted")
    db.session.add(invoice)
    db.session.flush()
    db.session.refresh(w.event)
    with pytest.raises(staffing.StaffingError):
        staffing.detach_equipment(w.event, camera, w.boss)
    invoice.status = "draft"
    db.session.flush()
    assert staffing.detach_equipment(w.event, camera, w.boss) is True


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------


def test_failing_event_subscriber_does_not_break_staffing(staff_app, bus):
    def broken(**_):
        raise RuntimeError("subscriber bug")

    eventbus.subscribe("assignment.changed", broken)
    w = world()
    staff = user()
    a = assign(w, staff)
    staffing.transition(a, "accepted", staff)
    assert a.status == "accepted"


def test_failing_notification_does_not_break_staffing(staff_app, monkeypatch):
    w = world()
    staff = user()

    def explode(*args, **kwargs):
        raise RuntimeError("sms gateway down")

    monkeypatch.setattr(notifications, "notify", explode)
    a = assign(w, staff)
    staffing.transition(a, "accepted", staff)
    staffing.change_event_status(w.event, "done", w.boss)
    assert a.status == "completed"


def test_bus_is_silent_for_failed_operations(staff_app, bus):
    w = world()
    staff = user()
    assign(w, staff)
    bus.clear()
    with pytest.raises(staffing.DuplicateAssignment):
        assign(w, staff)
    with pytest.raises(staffing.StaffingPermissionError):
        assign(w, user(), actor=user())
    assert bus == []


def test_exception_hierarchy():
    for exc in (
        staffing.InvalidTransition, staffing.AssignmentConflict, staffing.DuplicateAssignment,
        staffing.EventNotStaffable, staffing.TermsLocked, staffing.StaffingPermissionError,
    ):
        assert issubclass(exc, staffing.StaffingError)
    assert issubclass(staffing.StaffingPermissionError, PermissionError)
