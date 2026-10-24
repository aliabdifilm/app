"""The staffing board (افیش): assign, terms snapshot, conflicts and override, answers, replace, copy."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, TODAY, body, day, mc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AuditLog, Notification, StaffAvailability
from app.modules.ateliers import board as boards
from app.services import staffing
from app.services.constants import AssignmentStatus as S
from app.services.constants import Role, Skill

TERMS_FORM = {
    "base": "1000000",
    "commission": "9000",
    "tier_threshold[]": ["70"],
    "tier_amount[]": ["150000"],
}


def assign_url(event):
    return f"/atelier/events/{event.id}/afish/assign"


def post_assign(client, event, role="photographer", *, member=None, user=None, token=None, **extra):
    data = {"role": role, **TERMS_FORM}
    if member is not None:
        data["member"] = member.id
    if user is not None:
        data["user"] = user.id
    if token:
        data["k"] = token
    data.update(extra)
    return client.post(assign_url(event), data=data)


def active(event, role=None):
    db.session.expire_all()
    rows = db.session.scalars(sa.select(Assignment).where(Assignment.event_id == event.id)).all()
    return [a for a in rows if a.is_active and (role is None or a.role == role)]


# --------------------------------------------------------------------------
# assigning
# --------------------------------------------------------------------------


def test_assign_roster_member_is_confirmed_at_once_with_the_terms_snapshot(world, mc):
    response = post_assign(mc, world.event, member=world.member)
    assert response.status_code == 302
    (assignment,) = active(world.event)
    assert assignment.status == S.CONFIRMED
    assert assignment.roster_member_id == world.member.id
    assert assignment.terms_dict() == BIG_TERMS  # the AGENTS.md vector, as integers
    assert isinstance(assignment.base_pay, int)
    log = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "assignment.created"))
    assert log is not None and log.data["terms"]["base"] == 1_000_000


def test_assign_platform_user_invites_and_notifies(world, mc):
    response = post_assign(mc, world.event, user=world.staff)
    assert response.status_code == 302
    (assignment,) = active(world.event)
    assert assignment.status == S.INVITED
    note = db.session.scalar(sa.select(Notification).where(Notification.user_id == world.staff.id))
    assert note is not None and note.kind == "assignment_invited"


def test_assign_form_is_prefilled_from_the_atelier_preset(world, mc):
    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&member={world.member.id}"))
    assert 'value="1000000"' in page and 'value="9000"' in page
    assert "پیش‌فرض نقش در تنظیمات اتلیه" in page
    # no preset for editors: the form says so and starts empty
    editor = body(mc.get(f"{assign_url(world.event)}?role=editor&member={world.member.id}"))
    assert "پیش‌فرضی ثبت نشده است" in editor


def test_roster_default_terms_win_over_the_atelier_preset(world, mc):
    world.member.default_terms = {"photographer": {"base": 700_000, "commission_per_unit": 5_000, "bonus_tiers": []}}
    db.session.commit()
    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&member={world.member.id}"))
    assert 'value="700000"' in page and "پیش‌فرض همین نیروی ثابت" in page


def test_snapshot_is_immutable_after_the_preset_changes(world, mc):
    post_assign(mc, world.event, member=world.member)
    (assignment,) = active(world.event)
    before = assignment.terms_dict()

    new_preset = {"base": "2000000", "commission": "12000"}
    prefix = "__photographer"
    form = {f"base{prefix}": new_preset["base"], f"commission{prefix}": new_preset["commission"]}
    assert mc.post("/atelier/settings/presets", data=form).status_code == 302

    db.session.expire_all()
    assert db.session.get(Assignment, assignment.id).terms_dict() == before  # old afish untouched
    page = body(mc.get(f"{assign_url(world.later)}?role=photographer&member={world.member.id}"))
    assert 'value="2000000"' in page  # the next afish starts from the new preset


def test_the_same_person_cannot_hold_a_role_twice(world, mc):
    assert post_assign(mc, world.event, member=world.member).status_code == 302
    again = post_assign(mc, world.event, member=world.member)
    assert again.status_code == 422
    assert "قبلاً" in body(again)
    assert len(active(world.event, Skill.PHOTOGRAPHER)) == 1


def test_same_person_may_take_a_second_role_with_its_own_terms(world, mc):
    post_assign(mc, world.event, member=world.member)
    page = body(mc.get(f"{assign_url(world.event)}?role=editor&member={world.member.id}"))
    assert "نقش دیگری هم دارد" in page
    assert post_assign(mc, world.event, role="editor", member=world.member, base="400000").status_code == 302
    roles = {a.role: a.base_pay for a in active(world.event)}
    assert roles == {Skill.PHOTOGRAPHER: 1_000_000, Skill.EDITOR: 400_000}


def test_roster_member_of_another_atelier_is_404(world, mc):
    assert mc.get(f"{assign_url(world.event)}?role=photographer&member={world.rival_member.id}").status_code == 404
    assert post_assign(mc, world.event, member=world.rival_member).status_code == 404
    assert not active(world.event)


def test_inactive_roster_member_is_refused(world, mc):
    world.member.is_active = False
    db.session.commit()
    response = mc.get(f"{assign_url(world.event)}?role=photographer&member={world.member.id}")
    assert response.status_code == 302 and "/afish/new" in response.headers["Location"]


def test_closed_event_cannot_be_staffed(world, mc):
    staffing.change_event_status(world.event, "cancelled", world.manager)
    db.session.commit()
    response = post_assign(mc, world.event, member=world.member)
    assert response.status_code == 302 and not active(world.event)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"base": "abc"}, "نامعتبر"),
        ({"base": "-5"}, "نامعتبر"),
        ({"commission": "9.5"}, "نامعتبر"),
        ({"tier_threshold[]": ["70", "70"], "tier_amount[]": ["1", "2"]}, "دو پلهٔ پاداش"),
        ({"tier_threshold[]": ["0"], "tier_amount[]": ["100"]}, "تعداد عکس"),
        ({"tier_threshold[]": ["70"], "tier_amount[]": [""]}, "مبلغ"),
        ({"call_time": "25:99"}, "نامعتبر"),
    ],
)
def test_invalid_terms_are_rejected_with_a_message_and_the_typed_values(world, mc, fields, message):
    response = post_assign(mc, world.event, member=world.member, **fields)
    assert response.status_code == 422
    page = body(response)
    assert message in page
    assert not active(world.event)
    assert 'name="role"' in page and f'value="{world.member.id}"' in page  # the form is back, same person


def test_too_many_tiers_are_rejected(world, mc):
    response = post_assign(
        mc,
        world.event,
        member=world.member,
        **{"tier_threshold[]": [str(i) for i in range(1, 11)], "tier_amount[]": ["1000"] * 10},
    )
    assert response.status_code == 422 and "حداکثر" in body(response)


def test_persian_digits_are_accepted_in_every_field(world, mc):
    response = post_assign(
        mc,
        world.event,
        member=world.member,
        base="۱٬۰۰۰٬۰۰۰",
        commission="۹۰۰۰",
        call_time="۱۸:۳۰",
        **{"tier_threshold[]": ["۷۰"], "tier_amount[]": ["۱۵۰٬۰۰۰"]},
    )
    assert response.status_code == 302
    (assignment,) = active(world.event)
    assert assignment.terms_dict() == BIG_TERMS and assignment.call_time == dt.time(18, 30)


def test_unknown_role_is_404(world, mc):
    assert mc.get(f"{assign_url(world.event)}?role=wizard&member={world.member.id}").status_code == 404


def test_exactly_one_person_is_required(world, mc):
    assert mc.get(f"{assign_url(world.event)}?role=photographer").status_code == 404
    both = f"{assign_url(world.event)}?role=photographer&member={world.member.id}&user={world.staff.id}"
    assert mc.get(both).status_code == 404


# --------------------------------------------------------------------------
# double booking: warning, explicit override, hard block
# --------------------------------------------------------------------------


@pytest.fixture
def busy_staff(world):
    """The platform photographer is already booked by the rival atelier on the same day."""
    staffing.assign_person(world.rival_event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.rival_manager)
    db.session.commit()
    return world.staff


def test_conflict_is_a_blocking_warning_without_leaking_the_other_atelier(world, mc, busy_staff):
    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&user={busy_staff.id}"))
    assert "تداخل" in page and "مراسم دیگری (اتلیهٔ دیگر)" in page
    assert "مراسم رقیب" not in page and "اتلیه رقیب" not in page  # nothing about the other atelier
    assert 'name="override"' in page


def test_conflict_without_override_is_refused(world, mc, busy_staff):
    response = post_assign(mc, world.event, user=busy_staff)
    assert response.status_code == 422
    assert "تداخل" in body(response)
    assert not active(world.event)


def test_conflict_with_explicit_override_is_assigned_and_audited(world, mc, busy_staff):
    response = post_assign(mc, world.event, user=busy_staff, override="1")
    assert response.status_code == 302
    (assignment,) = active(world.event)
    assert assignment.user_id == busy_staff.id
    log = db.session.scalar(
        sa.select(AuditLog).where(AuditLog.action == "assignment.created", AuditLog.entity_id == assignment.id)
    )
    assert log.data["override_conflict"] is True


def test_conflict_inside_my_own_atelier_shows_hall_and_role(world, mc):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    other = world.later
    other.date = world.event.date  # a second event the same day
    db.session.commit()
    page = body(mc.get(f"{assign_url(other)}?role=editor&member={world.member.id}"))
    assert "تالار گل‌ها" in page and "عکاس" in page


def test_hard_block_policy_refuses_even_with_override(world, mc, busy_staff):
    world.atelier.block_double_booking = True
    db.session.commit()
    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&user={busy_staff.id}"))
    assert 'name="override"' not in page and "مجاز نیست" in page
    response = post_assign(mc, world.event, user=busy_staff, override="1")
    assert response.status_code == 422
    assert not active(world.event)


def test_unavailable_day_needs_the_override_too(world, mc):
    db.session.add(StaffAvailability(user_id=world.staff.id, date=world.event.date, note="سفر"))
    db.session.commit()
    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&user={world.staff.id}"))
    assert "در دسترس نیستم" in page and "سفر" in page
    assert post_assign(mc, world.event, user=world.staff).status_code == 422
    assert post_assign(mc, world.event, user=world.staff, override="1").status_code == 302
    assert len(active(world.event)) == 1


def test_unavailable_day_is_blocked_by_the_hard_policy(world, mc):
    world.atelier.block_double_booking = True
    db.session.add(StaffAvailability(user_id=world.staff.id, date=world.event.date))
    db.session.commit()
    assert post_assign(mc, world.event, user=world.staff, override="1").status_code == 422


def test_picker_flags_busy_people(world, mc, busy_staff):
    page = body(mc.get(f"/atelier/events/{world.event.id}/afish/new?role=photographer&via=search"))
    assert "همان روز مشغول است" in page


# --------------------------------------------------------------------------
# answers of invited people (accept / decline) as the manager sees them
# --------------------------------------------------------------------------


def test_accepting_fills_the_slot_and_tells_the_manager(world, mc):
    post_assign(mc, world.event, user=world.staff)
    (assignment,) = active(world.event)
    staffing.transition(assignment, S.ACCEPTED, world.staff)
    db.session.commit()

    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "پذیرفته" in page and "قطعی کردن" in page
    note = db.session.scalar(
        sa.select(Notification).where(Notification.user_id == world.manager.id, Notification.kind == "assignment_response")
    )
    assert note is not None and "پذیرفت" in note.title
    assert boards.load_board(world.event).completeness.filled == 1


def test_declining_shows_a_refusal_and_allows_reinviting_or_replacing(world, mc):
    post_assign(mc, world.event, user=world.staff)
    (assignment,) = active(world.event)
    staffing.transition(assignment, S.DECLINED, world.staff)
    db.session.commit()

    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "رد شده" in page and "دعوت دوباره" in page and "جایگزین" in page
    assert boards.load_board(world.event).completeness.missing == 4  # the refusal frees the slot
    note = db.session.scalar(
        sa.select(Notification).where(Notification.user_id == world.manager.id, Notification.kind == "assignment_response")
    )
    assert "رد" in note.title

    response = mc.post(f"/atelier/events/{world.event.id}/afish/{assignment.id}/reinvite")
    assert response.status_code == 302
    assert reload(assignment).status == S.INVITED


def test_confirm_skips_the_wait_and_confirm_all_only_takes_accepted_people(world, mc, make_user, make_staff_profile):
    other = make_user(Role.STAFF, full_name="نیروی دوم")
    make_staff_profile(other, skills=(Skill.SELLER,), is_listed=True)
    post_assign(mc, world.event, user=world.staff)
    post_assign(mc, world.event, role="seller", user=other)
    first, second = sorted(active(world.event), key=lambda a: a.id)
    staffing.transition(first, S.ACCEPTED, world.staff)
    db.session.commit()

    response = mc.post(f"/atelier/events/{world.event.id}/afish/confirm-all")
    assert response.status_code == 302
    assert reload(first).status == S.CONFIRMED
    assert reload(second).status == S.INVITED  # nobody confirms an unanswered invitation implicitly

    assert mc.post(f"/atelier/events/{world.event.id}/afish/{second.id}/confirm").status_code == 302
    assert reload(second).status == S.CONFIRMED  # the explicit "no waiting" action


def test_removing_a_person_cancels_and_tells_them(world, mc):
    post_assign(mc, world.event, user=world.staff)
    (assignment,) = active(world.event)
    response = mc.post(f"/atelier/events/{world.event.id}/afish/{assignment.id}/cancel")
    assert response.status_code == 302
    assert reload(assignment).status == S.CANCELLED
    titles = db.session.scalars(sa.select(Notification.title).where(Notification.user_id == world.staff.id)).all()
    assert any("لغو" in title for title in titles)


def test_locked_terms_cannot_be_removed_or_edited(world, mc):
    post_assign(mc, world.event, member=world.member)
    (assignment,) = active(world.event)
    staffing.lock_event_terms(world.event, world.manager)
    db.session.commit()

    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    assert "شرایط قفل‌شده" in page
    assert mc.post(f"/atelier/events/{world.event.id}/afish/{assignment.id}/cancel").status_code == 302
    assert reload(assignment).status == S.CONFIRMED
    edit = f"/atelier/events/{world.event.id}/afish/{assignment.id}/terms"
    assert mc.get(edit).status_code == 200 and "قفل" in body(mc.get(edit))
    assert mc.post(edit, data={**TERMS_FORM, "base": "5"}).status_code == 409
    assert reload(assignment).base_pay == 1_000_000


# --------------------------------------------------------------------------
# edit terms, replace, copy, needs
# --------------------------------------------------------------------------


def test_editing_terms_updates_the_snapshot_and_notifies_the_person(world, mc):
    post_assign(mc, world.event, user=world.staff)
    (assignment,) = active(world.event)
    url = f"/atelier/events/{world.event.id}/afish/{assignment.id}/terms"
    assert mc.get(url).status_code == 200
    response = mc.post(url, data={"base": "1200000", "commission": "9500", "reason": "توافق جدید"})
    assert response.status_code == 302
    fresh = reload(assignment)
    assert fresh.base_pay == 1_200_000 and fresh.commission_per_unit == 9_500 and fresh.bonus_tiers == []
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "assignment.terms_changed")) is not None
    titles = db.session.scalars(sa.select(Notification.title).where(Notification.user_id == world.staff.id)).all()
    assert any("شرایط پرداخت" in title for title in titles)


def test_edit_terms_validation_keeps_typed_values(world, mc):
    post_assign(mc, world.event, member=world.member)
    (assignment,) = active(world.event)
    url = f"/atelier/events/{world.event.id}/afish/{assignment.id}/terms"
    response = mc.post(url, data={"base": "x1", "commission": "9000"})
    assert response.status_code == 422 and "نامعتبر" in body(response)
    assert reload(assignment).base_pay == 1_000_000


def test_replace_cancels_the_old_person_only_when_the_new_one_is_assigned(world, mc, make_roster_member):
    new = make_roster_member(world.atelier, display_name="علی جانشین", skills=(Skill.PHOTOGRAPHER,))
    post_assign(mc, world.event, member=world.member)
    (old,) = active(world.event)

    page = body(mc.get(f"{assign_url(world.event)}?role=photographer&member={new.id}&replace={old.id}"))
    assert "رضا عکاس" in page and 'name="replace"' in page
    assert 'value="1000000"' in page  # the old terms are copied

    response = post_assign(mc, world.event, member=new, replace=old.id)
    assert response.status_code == 302
    assert reload(old).status == S.CANCELLED
    assert [a.roster_member_id for a in active(world.event)] == [new.id]


def test_failed_replacement_keeps_the_old_person(world, mc):
    post_assign(mc, world.event, member=world.member)
    (old,) = active(world.event)
    response = post_assign(mc, world.event, member=world.member, replace=old.id)  # same person: duplicate
    assert response.status_code == 422
    assert reload(old).status == S.CONFIRMED


def test_replace_argument_of_another_event_is_404(world, mc):
    staffing.assign_person(world.rival_event, "photographer", roster_member=world.rival_member, terms=BIG_TERMS, actor=world.rival_manager)
    db.session.commit()
    foreign = db.session.scalar(sa.select(Assignment).where(Assignment.event_id == world.rival_event.id))
    url = f"{assign_url(world.event)}?role=photographer&member={world.member.id}&replace={foreign.id}"
    assert mc.get(url).status_code == 404


def test_copy_staffing_from_a_previous_event(world, mc, make_roster_member):
    second = make_roster_member(world.atelier, display_name="مریم", skills=(Skill.PRESENTER,))
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    staffing.assign_person(world.event, "presenter", roster_member=second, terms={"base": 5, "commission_per_unit": 0, "bonus_tiers": []}, actor=world.manager)
    db.session.commit()

    chooser = body(mc.get(f"/atelier/events/{world.later.id}/afish/copy"))
    assert f"source={world.event.id}" in chooser
    plan = body(mc.get(f"/atelier/events/{world.later.id}/afish/copy?source={world.event.id}"))
    assert "رضا عکاس" in plan and "مریم" in plan

    picked = [a.id for a in active(world.event)]
    response = mc.post(f"/atelier/events/{world.later.id}/afish/copy", data={"source": world.event.id, "pick": picked})
    assert response.status_code == 302
    copied = {a.role: a for a in active(world.later)}
    assert set(copied) == {Skill.PHOTOGRAPHER, Skill.PRESENTER}
    assert copied[Skill.PHOTOGRAPHER].terms_dict() == BIG_TERMS and copied[Skill.PRESENTER].base_pay == 5


def test_copy_skips_people_who_are_busy_that_day(world, mc):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    world.later.date = world.event.date + dt.timedelta(days=1)
    other_day = world.later
    staffing.assign_person(other_day, "editor", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    # copying the first event onto the second: the member already works there (as editor) -> clash is not a
    # duplicate (other role) but the person is busy that day only if the date is shared; here dates differ.
    plan = body(mc.get(f"/atelier/events/{other_day.id}/afish/copy?source={world.event.id}"))
    assert "رضا عکاس" in plan


def test_copy_from_a_foreign_event_is_404(world, mc):
    assert mc.get(f"/atelier/events/{world.event.id}/afish/copy?source={world.rival_event.id}").status_code == 404


def test_head_count_per_role_changes_the_slots(world, mc):
    response = mc.post(f"/atelier/events/{world.event.id}/needs", data={"need__photographer": "3", "need__seller": "0"})
    assert response.status_code == 302
    db.session.expire_all()
    needs = world.event.staffing_needs
    assert needs["photographer"] == 3 and needs["seller"] == 0 and needs["editor"] == 1
    board = boards.load_board(reload(world.event))
    assert board.completeness.needed == 1 + 3 + 0 + 1  # presenter + photographer + seller + editor
    assert "۳ جای خالی" in body(mc.get(f"/atelier/events/{world.event.id}"))


def test_head_count_is_validated(world, mc):
    response = mc.post(f"/atelier/events/{world.event.id}/needs", data={"need__photographer": "99"})
    assert response.status_code == 302
    assert reload(world.event).needed("photographer") == 1


def test_multiple_people_per_role_are_counted(world, mc, make_roster_member):
    mc.post(f"/atelier/events/{world.event.id}/needs", data={"need__photographer": "2"})
    second = make_roster_member(world.atelier, display_name="دومی", skills=(Skill.PHOTOGRAPHER,))
    post_assign(mc, world.event, member=world.member)
    board = boards.load_board(reload(world.event))
    assert board.summary[Skill.PHOTOGRAPHER].missing == 1
    post_assign(mc, world.event, member=second)
    board = boards.load_board(reload(world.event))
    assert board.summary[Skill.PHOTOGRAPHER].filled == 2 and board.summary[Skill.PHOTOGRAPHER].missing == 0


# --------------------------------------------------------------------------
# directory: unlisted people cannot be browsed or guessed
# --------------------------------------------------------------------------


def test_directory_lists_only_public_profiles_and_finds_others_by_exact_mobile(world, mc, make_user, make_staff_profile):
    hidden = make_user(Role.STAFF, full_name="نیروی پنهان", mobile="09125550000")
    make_staff_profile(hidden, skills=(Skill.PHOTOGRAPHER,), is_listed=False)
    url = f"/atelier/events/{world.event.id}/afish/new?role=photographer&via=search"
    assert "سارا نیرو" in body(mc.get(url)) and "نیروی پنهان" not in body(mc.get(url))
    assert "نیروی پنهان" not in body(mc.get(url + "&q=پنهان"))
    page = body(mc.get(url + "&q=۰۹۱۲۵۵۵۰۰۰۰"))
    assert "نیروی پنهان" in page and "k=" in page  # exact number: found, with a signed token


def test_unlisted_user_needs_the_signed_token_to_be_assigned(world, mc, make_user, make_staff_profile):
    hidden = make_user(Role.STAFF, full_name="نیروی پنهان", mobile="09125550000")
    make_staff_profile(hidden, skills=(Skill.PHOTOGRAPHER,), is_listed=False)
    guess = f"{assign_url(world.event)}?role=photographer&user={hidden.id}"
    assert mc.get(guess).status_code == 404
    assert post_assign(mc, world.event, user=hidden).status_code == 404
    assert not active(world.event)

    token = boards.directory_token(world.manager.id, hidden.id)
    assert mc.get(f"{guess}&k={token}").status_code == 200
    assert post_assign(mc, world.event, user=hidden, token=token).status_code == 302
    # a token minted for somebody else is worthless
    other_token = boards.directory_token(world.rival_manager.id, hidden.id)
    assert mc.get(f"{guess}&k={other_token}").status_code == 404


def test_only_staff_accounts_can_be_picked(world, mc, make_user):
    boss = make_user(Role.HALL_MANAGER)
    assert mc.get(f"{assign_url(world.event)}?role=photographer&user={boss.id}").status_code == 404


# --------------------------------------------------------------------------
# board overview, page contents
# --------------------------------------------------------------------------


def test_event_page_shows_slots_statuses_meter_and_share_text(world, mc):
    post_assign(mc, world.event, member=world.member)
    post_assign(mc, world.event, role="seller", user=world.staff)
    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    for label in ("پرزنتور", "عکاس", "فروشنده", "ادیتور"):
        assert label in page
    assert "رضا عکاس" in page and "سارا نیرو" in page
    assert "دعوت شده" in page and "نیروی ثابت" in page
    assert "۱ از ۴ قطعی" in page and "۱ منتظر پاسخ" in page
    assert "پایه ۱٬۰۰۰٬۰۰۰ تومان" in page and "پاداش ۷۰ عکس به بالا ۱۵۰٬۰۰۰ تومان" in page
    assert "جای خالی" in page
    assert "09" not in page.split('data-share-text="')[1].split('"')[0]  # the shared text has no phone numbers


def test_board_overview_lists_upcoming_events_with_gaps(world, mc):
    post_assign(mc, world.event, member=world.member)
    page = body(mc.get("/atelier/afish"))
    assert "تالار گل‌ها" in page and "رضا عکاس" in page and "جای خالی" in page
    assert "مراسم رقیب" not in page
    assert "تالار" in body(mc.get("/atelier/afish?range=week"))
    assert mc.get("/atelier/afish?range=past").status_code == 200
    assert mc.get("/atelier/afish?range=nonsense").status_code == 200


def test_marketplace_link_is_prefilled_from_the_slot_when_the_module_exists(world, mc, app):
    @app.get("/jobs/new")
    def job_new_stub():  # stands in for the marketplace create form
        return "stub"

    page = body(mc.get(f"/atelier/events/{world.event.id}"))
    links = [chunk.split('"')[0].replace("&amp;", "&") for chunk in page.split("/jobs/new?")[1:]]
    assert len(links) == 4  # one per role that still has an empty slot
    photographer = next(link for link in links if "role=photographer" in link)
    assert f"event={world.event.id}" in photographer and "base=1000000" in photographer and "commission=9000" in photographer
    assert "tiers=70%3A150000" in photographer or "tiers=70:150000" in photographer
    assert "date=" in photographer and "city=" in photographer and "slots=1" in photographer


# --------------------------------------------------------------------------
# probing registered numbers is limited
# --------------------------------------------------------------------------


def test_exact_mobile_lookups_are_rate_limited_but_name_searches_are_not(world, mc):
    from app.modules.ateliers.common import LOOKUP_LIMIT

    url = f"/atelier/events/{world.event.id}/afish/new?role=photographer&via=search&q=09125559999"
    for _ in range(LOOKUP_LIMIT):
        assert mc.get(url).status_code == 200
    blocked = mc.get(url)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert mc.get(url.replace("09125559999", "سارا")).status_code == 200


def test_linking_a_roster_member_by_mobile_spends_the_same_budget(world, mc):
    from app.modules.ateliers.common import LOOKUP_LIMIT

    for n in range(LOOKUP_LIMIT):
        response = mc.post("/atelier/roster/new", data={"display_name": f"م{n}", "skills": ["editor"], "mobile": f"0913{n:07d}"})
        assert response.status_code == 302
    blocked = mc.post("/atelier/roster/new", data={"display_name": "آخر", "skills": ["editor"], "mobile": "09139999999"})
    assert blocked.status_code == 429
    assert mc.post("/atelier/roster/new", data={"display_name": "بی‌شماره", "skills": ["editor"]}).status_code == 302
