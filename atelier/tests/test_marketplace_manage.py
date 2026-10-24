"""The atelier manager's side: posts, the applicants inbox, accept (-> afish) / reject, close, tenant isolation."""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from test_marketplace_support import BIG_TERMS, application_of, body, day, mc, now, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AuditLog, JobPost, Notification
from app.services import staffing
from app.services.constants import (
    ApplicationStatus,
    AssignmentSource,
    AssignmentStatus,
    EventStatus,
    JobPostStatus,
    Role,
    Skill,
)


def count_rows(model) -> int:
    db.session.expire_all()
    return db.session.scalar(sa.select(sa.func.count(model.id)))


def notes_of(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt.order_by(Notification.id)))


def audit_actions():
    db.session.expire_all()
    return [row.action for row in db.session.scalars(sa.select(AuditLog).order_by(AuditLog.id))]


def applied(world, client_for, user=None, post=None, message="سلام"):
    user = user or world.staff
    post = post or world.post
    assert client_for(user).post(f"/jobs/{post.id}/apply", data={"message": message}).status_code == 302
    return application_of(user, post)


FORM = {
    "title": "عکاس برای مراسم جمعه",
    "role": "photographer",
    "date": "",
    "city": "شیراز",
    "slots": "2",
    "description": "ساعت ۱۸ در تالار",
    "base": "1,000,000",
    "commission": "9000",
    "tier_threshold[]": ["70", ""],
    "tier_amount[]": ["150000", ""],
}


def form_data(**overrides):
    data = dict(FORM)
    data["date"] = day(14).isoformat()
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# creating a post
# --------------------------------------------------------------------------


def test_create_a_post_with_the_terms_snapshot_ready(world, mc):
    response = mc.post("/jobs/new", data=form_data())
    assert response.status_code == 302
    db.session.expire_all()
    post = db.session.scalar(sa.select(JobPost).where(JobPost.title == "عکاس برای مراسم جمعه"))
    assert post.atelier_id == world.atelier.id
    assert (post.role, post.date, post.city, post.slots, post.status) == ("photographer", day(14), "شیراز", 2, "open")
    assert post.terms_dict() == BIG_TERMS
    assert "job_post.created" in audit_actions()
    assert response.headers["Location"].endswith(f"/jobs/{post.id}/applicants")


def test_persian_digits_and_jalali_dates_are_accepted(world, mc):
    response = mc.post("/jobs/new", data=form_data(date="۱۴۰۴/۰۴/۳۱", base="۱٬۰۰۰٬۰۰۰", commission="۹۰۰۰", slots="۲",
                                                  **{"tier_threshold[]": ["۷۰"], "tier_amount[]": ["۱۵۰٬۰۰۰"]}))
    assert response.status_code == 302
    post = db.session.scalar(sa.select(JobPost).where(JobPost.title == "عکاس برای مراسم جمعه"))
    assert post.date == dt.date(2025, 7, 22) and post.terms_dict() == BIG_TERMS


def test_the_new_post_is_on_the_public_board(app, world, mc):
    mc.post("/jobs/new", data=form_data())
    assert "عکاس برای مراسم جمعه" in body(app.test_client().get("/jobs"))


def test_validation_errors_give_422_and_keep_what_was_typed(world, mc):
    response = mc.post("/jobs/new", data=form_data(title="", slots="99", base="abc", date=day(-1).isoformat()))
    assert response.status_code == 422
    html = body(response)
    assert "عنوان آگهی را وارد کنید" in html
    assert "تعداد نیروی مورد نیاز نباید بیشتر از ۵۰ باشد" in html
    assert "تاریخ همکاری نباید گذشته باشد" in html
    assert "حقوق پایه نامعتبر است" in html
    assert 'value="99"' in html and 'value="شیراز"' in html
    assert count_rows(JobPost) == 2


def test_bonus_tier_errors(world, mc):
    bad = mc.post("/jobs/new", data=form_data(**{"tier_threshold[]": ["70", "70"], "tier_amount[]": ["1", "2"]}))
    assert bad.status_code == 422 and "نباید تعداد عکس یکسان داشته باشند" in body(bad)
    worse = mc.post("/jobs/new", data=form_data(**{"tier_threshold[]": ["x"], "tier_amount[]": ["5"]}))
    assert worse.status_code == 422 and "تعداد عکس را به‌صورت عدد صحیح" in body(worse)
    assert count_rows(JobPost) == 2


def test_a_blank_city_falls_back_to_the_hall_or_atelier_city(world, mc):
    mc.post("/jobs/new", data=form_data(city="", role="editor", event=str(world.event.id), date=world.event.date.isoformat(), title="با مراسم"))
    post = db.session.scalar(sa.select(JobPost).where(JobPost.title == "با مراسم"))
    assert post.city == "شیراز" and post.event_id == world.event.id


def test_a_foreign_event_in_the_form_is_refused(world, mc):
    response = mc.post("/jobs/new", data=form_data(event=str(world.rival_event.id), date=world.rival_event.date.isoformat()))
    assert response.status_code == 422 and "مراسم انتخاب‌شده معتبر نیست" in body(response)
    assert count_rows(JobPost) == 2


def test_the_event_date_and_the_post_date_must_agree(world, mc):
    response = mc.post("/jobs/new", data=form_data(event=str(world.event.id)))  # event is day(7), form says day(14)
    assert response.status_code == 422 and "باید با تاریخ مراسمِ انتخاب‌شده یکی باشد" in body(response)


def test_a_second_open_post_for_the_same_event_and_role_is_refused(world, mc):
    response = mc.post("/jobs/new", data=form_data(event=str(world.event.id), date=world.event.date.isoformat()))
    assert response.status_code == 409 and "آگهیِ باز «عکاس» دارید" in body(response)
    assert count_rows(JobPost) == 2


def test_the_form_is_prefilled_from_the_afish_page_contract(world, mc):
    url = (f"/jobs/new?event={world.event.id}&role=photographer&date={world.event.date.isoformat()}&city=شیراز"
           "&title=عکاس برای مراسم&slots=2&base=1000000&commission=9000&tiers=70:150000,100:300000")
    html = body(mc.get(url))
    assert 'value="عکاس برای مراسم"' in html
    assert f'<option value="{world.event.id}" selected>' in html
    assert '<option value="photographer" selected>' in html
    assert 'value="1000000"' in html and 'value="9000"' in html
    assert 'value="۷۰"' in html and 'value="150000"' in html and 'value="300000"' in html
    assert 'name="slots"' in html and 'value="2"' in html


def test_the_form_is_prefilled_from_the_atelier_preset_for_the_role(world, mc):
    world.atelier.default_pay_presets = {"photographer": BIG_TERMS}
    db.session.commit()
    html = body(mc.get("/jobs/new?role=photographer"))
    assert 'value="1000000"' in html and 'value="9000"' in html and 'value="150000"' in html
    assert 'data-presets=' in html


def test_junk_in_the_prefill_query_is_ignored(world, mc):
    html = body(mc.get("/jobs/new?event=abc&role=astronaut&date=zzz&slots=-4&base=x&tiers=a:b,,7"))
    assert mc.get("/jobs/new?event=99999999999999999999").status_code == 200
    assert "ثبت آگهی همکاری" in html


def test_a_foreign_event_in_the_prefill_is_ignored(world, mc):
    html = body(mc.get(f"/jobs/new?event={world.rival_event.id}"))
    assert f'value="{world.rival_event.id}" selected' not in html


def test_the_daily_post_limit(app, world, mc, clock):
    app.config["MARKETPLACE_MAX_POSTS_PER_DAY"] = 2
    world.post.created_at = clock.now() - dt.timedelta(days=3)  # the fixture post is old; it does not count
    db.session.commit()
    assert mc.post("/jobs/new", data=form_data(title="یک")).status_code == 302
    assert mc.post("/jobs/new", data=form_data(title="دو")).status_code == 302
    blocked = mc.post("/jobs/new", data=form_data(title="سه"))
    assert blocked.status_code == 429 and "بیش از حد مجاز" in body(blocked)
    assert 'value="سه"' in body(blocked)  # what was typed is kept
    clock.advance(hours=25)  # the 24 h window has passed
    assert mc.post("/jobs/new", data=form_data(title="سه", date=day(14).isoformat())).status_code == 302


def test_the_manager_without_an_atelier_is_sent_to_onboarding(client_for, make_user):
    newcomer = make_user(Role.ATELIER_MANAGER)
    for url in ("/jobs/manage", "/jobs/new", "/jobs/manage/inbox"):
        response = client_for(newcomer).get(url)
        assert response.status_code == 302 and "/atelier/setup" in response.headers["Location"]


# --------------------------------------------------------------------------
# editing
# --------------------------------------------------------------------------


def test_edit_changes_the_post_and_is_audited(world, mc):
    response = mc.post(f"/jobs/{world.post.id}/edit", data=form_data(title="عنوان تازه", event=str(world.event.id),
                                                                   date=world.event.date.isoformat(), base="2000000", slots="3"))
    assert response.status_code == 302
    post = reload(world.post)
    assert post.title == "عنوان تازه" and post.slots == 3 and post.terms_dict()["base"] == 2_000_000
    assert "job_post.updated" in audit_actions()


def test_edit_form_shows_the_current_values(world, mc):
    html = body(mc.get(f"/jobs/{world.post.id}/edit"))
    assert 'value="عکاس برای عروسی"' in html and 'value="1000000"' in html and 'value="9000"' in html


def test_terms_role_and_date_are_frozen_once_applications_exist(world, mc, client_for):
    applied(world, client_for)
    html = body(mc.get(f"/jobs/{world.post.id}/edit"))
    assert "قفل است" in html and 'name="role"' not in html and 'name="base"' not in html
    # a forged POST cannot change them either
    response = mc.post(f"/jobs/{world.post.id}/edit", data=form_data(title="فقط عنوان", base="5", role="seller",
                                                                   date=day(30).isoformat(), event=str(world.event.id)))
    assert response.status_code == 302
    post = reload(world.post)
    assert post.title == "فقط عنوان"
    assert post.terms_dict() == BIG_TERMS and post.role == "photographer" and post.date == day(7)


def test_slots_cannot_drop_below_the_accepted_count(world, mc, client_for, make_user, make_staff_profile):
    other = make_user(Role.STAFF, full_name="نیروی دوم", mobile="09121110009")
    make_staff_profile(other, skills=(Skill.PHOTOGRAPHER,))
    world.post.slots = 2
    db.session.commit()
    for user in (world.staff, other):
        mc.post(f"/jobs/manage/applications/{applied(world, client_for, user=user).id}/accept")
    assert reload(world.post).status == JobPostStatus.FILLED
    response = mc.post(f"/jobs/{world.post.id}/edit", data=form_data(slots="1"))
    assert response.status_code == 422 and "کمتر باشد" in body(response)
    assert reload(world.post).slots == 2


def test_raising_the_slots_of_a_filled_post_opens_it_again(world, mc, client_for):
    first = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{first.id}/accept")
    assert reload(world.post).status == "filled"
    mc.post(f"/jobs/{world.post.id}/edit", data=form_data(slots="2"))
    assert reload(world.post).status == "open"


# --------------------------------------------------------------------------
# accepting: the afish row with the exact terms snapshot
# --------------------------------------------------------------------------


def test_accept_creates_the_assignment_with_the_exact_snapshot(world, mc, client_for):
    application = applied(world, client_for)
    response = mc.post(f"/jobs/manage/applications/{application.id}/accept")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/jobs/{world.post.id}/applicants")

    db.session.expire_all()
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.job_application_id == application.id))
    assert assignment.event_id == world.event.id and assignment.user_id == world.staff.id
    assert assignment.role == "photographer"
    assert assignment.source == AssignmentSource.MARKETPLACE
    assert assignment.status == AssignmentStatus.CONFIRMED
    assert assignment.created_by_id == world.manager.id
    assert (assignment.base_pay, assignment.commission_per_unit, assignment.bonus_tiers) == (
        1_000_000, 9_000, [{"threshold": 70, "amount": 150_000}]
    )
    assert assignment.terms_dict() == world.post.terms_dict()
    # the AGENTS.md vector: 80 photos -> 1,870,000 for this person
    assert assignment.pay_terms().base + 80 * assignment.commission_per_unit + 150_000 == 1_870_000

    application = reload(application)
    assert application.status == ApplicationStatus.ACCEPTED and application.decided_at is not None
    assert reload(world.post).status == JobPostStatus.FILLED  # one slot, one accepted
    actions = audit_actions()
    assert "job_application.accepted" in actions and "assignment.created" in actions


def test_the_snapshot_survives_later_changes_to_the_post(world, mc, client_for):
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    post = reload(world.post)
    post.apply_terms({"base": 9, "commission_per_unit": 9, "bonus_tiers": []})
    db.session.commit()
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.job_application_id == application.id))
    assert assignment.terms_dict() == BIG_TERMS


def test_accept_tells_the_applicant_and_reveals_the_contact_both_ways(world, mc, sc, client_for):
    application = applied(world, client_for)
    review_page = body(mc.get(f"/jobs/manage/applications/{application.id}"))
    assert world.staff.mobile not in review_page  # not before
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    assert any("پذیرفته شد" in n.title for n in notes_of(world.staff, "application_decided"))
    # the manager now sees the number of the person ...
    assert f"tel:{world.staff.mobile}" in body(mc.get(f"/jobs/manage/applications/{application.id}"))
    # ... and the person the manager's and the atelier's
    mine = body(sc.get("/jobs/applications"))
    assert f"tel:{world.manager.mobile}" in mine and "tel:07112345678" in mine
    assert f"tel:{world.manager.mobile}" in body(sc.get(f"/jobs/{world.post.id}"))


def test_no_contact_before_the_acceptance_for_the_applicant(world, sc, client_for):
    applied(world, client_for)
    pending = body(sc.get("/jobs/applications")) + body(sc.get(f"/jobs/{world.post.id}"))
    assert world.manager.mobile not in pending and "07112345678" not in pending


def test_a_filled_post_leaves_the_board_and_refuses_more_accepts(app, world, mc, client_for, make_user, make_staff_profile):
    other = make_user(Role.STAFF, full_name="نیروی دوم", mobile="09121110009")
    make_staff_profile(other, skills=(Skill.PHOTOGRAPHER,))
    first = applied(world, client_for)
    second = applied(world, client_for, user=other)
    mc.post(f"/jobs/manage/applications/{first.id}/accept")
    assert "عکاس برای عروسی" not in body(app.test_client().get("/jobs"))
    refused = mc.post(f"/jobs/manage/applications/{second.id}/accept", follow_redirects=True)
    assert "ظرفیت این آگهی تکمیل شده است" in body(refused)
    assert reload(second).status == ApplicationStatus.PENDING
    assert count_rows(Assignment) == 1


def test_accept_is_idempotent_on_a_double_submit(world, mc, client_for):
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    again = mc.post(f"/jobs/manage/applications/{application.id}/accept", follow_redirects=True)
    assert "قبلاً پذیرفته شده است" in body(again)
    assert count_rows(Assignment) == 1


def test_accept_is_blocked_by_a_conflict_that_appeared_after_applying(world, mc, client_for, make_event, make_assignment):
    application = applied(world, client_for)
    elsewhere = make_event(world.hall, world.rival, date=world.post.date, title="جای دیگر")
    make_assignment(elsewhere, "seller", user=world.staff, status="confirmed")
    response = mc.post(f"/jobs/manage/applications/{application.id}/accept", follow_redirects=True)
    assert "در همان روز در مراسم دیگری افیش شده است" in body(response)
    assert reload(application).status == ApplicationStatus.PENDING
    assert reload(world.post).status == JobPostStatus.OPEN
    assert count_rows(Assignment) == 1  # only the other atelier's row


def test_the_review_page_warns_about_a_busy_applicant_without_details(world, mc, client_for, make_event, make_assignment):
    application = applied(world, client_for)
    elsewhere = make_event(world.hall, world.rival, date=world.post.date, title="مراسم محرمانه")
    make_assignment(elsewhere, "seller", user=world.staff, status="confirmed")
    html = body(mc.get(f"/jobs/manage/applications/{application.id}"))
    assert "این نفر همان روز مشغول است" in html and "مراسم محرمانه" not in html


def test_a_post_without_an_event_needs_a_candidate_event_on_its_date(world, mc, client_for, make_job_post, make_event):
    loose = make_job_post(world.atelier, Skill.SELLER, date=day(15), title="بدون مراسم", base_pay=400_000, commission_per_unit=0)
    application = applied(world, client_for, post=loose)
    # no event of the atelier on that day yet
    html = body(mc.get(f"/jobs/manage/applications/{application.id}"))
    assert "در این تاریخ مراسمی ندارید" in html
    refused = mc.post(f"/jobs/manage/applications/{application.id}/accept", follow_redirects=True)
    assert "مراسمی را که این نیرو در آن کار می‌کند انتخاب کنید" in body(refused)
    # one event on that day: chosen automatically
    only = make_event(world.hall, world.atelier, date=day(15), title="تنها مراسم")
    assert mc.post(f"/jobs/manage/applications/{application.id}/accept").status_code == 302
    db.session.expire_all()
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.job_application_id == application.id))
    assert assignment.event_id == only.id and assignment.role == "seller" and assignment.base_pay == 400_000


def test_with_two_events_on_the_day_the_manager_must_choose_and_only_a_valid_one_works(world, mc, client_for, make_job_post, make_event):
    loose = make_job_post(world.atelier, Skill.SELLER, date=day(16), title="دو مراسم", base_pay=1, commission_per_unit=0)
    lunch = make_event(world.hall, world.atelier, date=day(16), title="ناهار")
    dinner = make_event(world.hall, world.atelier, date=day(16), title="شام")
    foreign = make_event(world.hall, world.rival, date=day(16), title="رقیب")
    other_day = make_event(world.hall, world.atelier, date=day(17), title="روز دیگر")
    application = applied(world, client_for, post=loose)
    html = body(mc.get(f"/jobs/manage/applications/{application.id}"))
    assert 'name="event"' in html and f'value="{lunch.id}"' in html and f'value="{dinner.id}"' in html
    assert f'value="{foreign.id}"' not in html
    for bad in (foreign.id, other_day.id):  # ids from the client are never trusted
        refused = mc.post(f"/jobs/manage/applications/{application.id}/accept", data={"event": str(bad)}, follow_redirects=True)
        assert "مراسم انتخاب‌شده معتبر نیست" in body(refused), bad
    unknown = mc.post(f"/jobs/manage/applications/{application.id}/accept", data={"event": "99999"}, follow_redirects=True)
    assert "انتخاب کنید" in body(unknown)
    assert count_rows(Assignment) == 0
    assert mc.post(f"/jobs/manage/applications/{application.id}/accept", data={"event": str(dinner.id)}).status_code == 302
    db.session.expire_all()
    assert db.session.scalar(sa.select(Assignment.event_id).where(Assignment.job_application_id == application.id)) == dinner.id


def test_a_post_linked_to_an_event_ignores_the_event_in_the_form(world, mc, client_for, make_event):
    sibling = make_event(world.hall, world.atelier, date=world.post.date, title="مراسم دوم همان روز")
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/accept", data={"event": str(sibling.id)})
    db.session.expire_all()
    assert db.session.scalar(sa.select(Assignment.event_id).where(Assignment.job_application_id == application.id)) == world.event.id


def test_accept_refuses_when_the_event_is_no_longer_staffable(world, mc, client_for):
    application = applied(world, client_for)
    world.event.status = EventStatus.CANCELLED
    db.session.commit()
    mc.post(f"/jobs/manage/applications/{application.id}/accept", follow_redirects=True)
    assert count_rows(Assignment) == 0
    assert reload(application).status == ApplicationStatus.PENDING


# --------------------------------------------------------------------------
# rejecting, closing, reopening
# --------------------------------------------------------------------------


def test_reject_with_a_message(world, mc, client_for):
    application = applied(world, client_for)
    response = mc.post(f"/jobs/manage/applications/{application.id}/reject", data={"message": "ظرفیت پر شد"})
    assert response.status_code == 302
    application = reload(application)
    assert application.status == ApplicationStatus.REJECTED and application.decided_at is not None
    [note] = notes_of(world.staff, "application_decided")
    assert "ظرفیت پر شد" in note.body and note.url == "/jobs/applications"
    assert "job_application.rejected" in audit_actions()
    assert count_rows(Assignment) == 0


def test_rejecting_or_accepting_a_decided_application_is_refused(world, mc, client_for):
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/reject", data={})
    again = mc.post(f"/jobs/manage/applications/{application.id}/reject", data={}, follow_redirects=True)
    assert "فقط درخواست‌های در انتظار بررسی را می‌توان رد کرد" in body(again)
    accept = mc.post(f"/jobs/manage/applications/{application.id}/accept", follow_redirects=True)
    assert "فقط درخواست‌های در انتظار بررسی را می‌توان پذیرفت" in body(accept)
    assert count_rows(Assignment) == 0


def test_closing_a_post_rejects_pending_applicants_and_tells_them(world, mc, client_for, make_user, make_staff_profile):
    other = make_user(Role.STAFF, full_name="نیروی دوم", mobile="09121110009")
    make_staff_profile(other, skills=(Skill.PHOTOGRAPHER,))
    first = applied(world, client_for)
    second = applied(world, client_for, user=other)
    response = mc.post(f"/jobs/{world.post.id}/close", data={"reject_pending": "1"})
    assert response.status_code == 302
    assert reload(world.post).status == JobPostStatus.CLOSED
    assert reload(first).status == ApplicationStatus.REJECTED and reload(second).status == ApplicationStatus.REJECTED
    assert len(notes_of(other, "application_decided")) == 1
    assert "job_post.closed" in audit_actions()


def test_closing_without_the_bulk_flag_keeps_pending_applications(world, mc, client_for):
    application = applied(world, client_for)
    mc.post(f"/jobs/{world.post.id}/close", data={})
    assert reload(application).status == ApplicationStatus.PENDING
    assert reload(world.post).status == JobPostStatus.CLOSED


def test_bulk_close_after_the_post_is_filled(world, mc, client_for, make_user, make_staff_profile):
    other = make_user(Role.STAFF, full_name="نیروی دوم", mobile="09121110009")
    make_staff_profile(other, skills=(Skill.PHOTOGRAPHER,))
    first = applied(world, client_for)
    second = applied(world, client_for, user=other)
    mc.post(f"/jobs/manage/applications/{first.id}/accept")
    html = body(mc.get(f"/jobs/{world.post.id}/applicants"))
    assert "ظرفیت آگهی تکمیل شد" in html and "۱ درخواست در انتظار" in html
    mc.post(f"/jobs/{world.post.id}/close", data={"reject_pending": "1"})
    assert reload(second).status == ApplicationStatus.REJECTED
    assert reload(first).status == ApplicationStatus.ACCEPTED  # the accepted one is untouched


def test_reopen_a_closed_post(world, mc, client_for):
    mc.post(f"/jobs/{world.post.id}/close", data={})
    assert mc.post(f"/jobs/{world.post.id}/reopen").status_code == 302
    assert reload(world.post).status == JobPostStatus.OPEN
    again = mc.post(f"/jobs/{world.post.id}/reopen", follow_redirects=True)
    assert "این آگهی بسته نیست" in body(again)


def test_a_post_cannot_be_reopened_after_its_date_or_when_hidden(world, mc, clock):
    mc.post(f"/jobs/{world.post.id}/close", data={})
    clock.advance(days=10)
    assert "تاریخ این آگهی گذشته است" in body(mc.post(f"/jobs/{world.post.id}/reopen", follow_redirects=True))
    assert reload(world.post).status == JobPostStatus.CLOSED
    hidden = reload(world.post)
    hidden.status = JobPostStatus.HIDDEN
    db.session.commit()
    assert "مخفی شده" in body(mc.post(f"/jobs/{world.post.id}/reopen", follow_redirects=True))
    assert reload(world.post).status == JobPostStatus.HIDDEN


def test_cancelling_the_afish_row_elsewhere_reopens_the_post(world, mc, client_for):
    """The atelier cancels from its own afish board: the application and the fill count stay true (bus subscriber)."""
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    assert reload(world.post).status == JobPostStatus.FILLED
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.job_application_id == application.id))
    staffing.transition(assignment, AssignmentStatus.CANCELLED, world.manager)
    db.session.commit()
    assert reload(application).status == ApplicationStatus.WITHDRAWN
    assert reload(world.post).status == JobPostStatus.OPEN


def test_cancelling_the_event_withdraws_accepted_applications(world, mc, client_for):
    application = applied(world, client_for)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    staffing.change_event_status(world.event, "cancelled", world.manager)
    db.session.commit()
    assert reload(application).status == ApplicationStatus.WITHDRAWN


# --------------------------------------------------------------------------
# tenant isolation
# --------------------------------------------------------------------------


def test_manage_lists_show_only_my_own_posts_and_applicants(world, mc, client_for, make_user, make_staff_profile):
    outsider = make_user(Role.STAFF, full_name="درخواست‌دهندهٔ رقیب", mobile="09121110008")
    make_staff_profile(outsider, skills=(Skill.PHOTOGRAPHER,))
    applied(world, client_for)
    applied(world, client_for, user=outsider, post=world.rival_post)
    manage = body(mc.get("/jobs/manage?tab=all"))
    assert "عکاس برای عروسی" in manage and "عکاس رقیب" not in manage
    inbox = body(mc.get("/jobs/manage/inbox?tab=all"))
    assert "سارا نیرو" in inbox and "درخواست‌دهندهٔ رقیب" not in inbox
    rival_inbox = body(client_for(world.rival_manager).get("/jobs/manage/inbox?tab=all"))
    assert "درخواست‌دهندهٔ رقیب" in rival_inbox and "سارا نیرو" not in rival_inbox


def test_foreign_posts_and_applications_are_404_and_untouched(world, mc, client_for, make_user, make_staff_profile):
    outsider = make_user(Role.STAFF, full_name="درخواست‌دهندهٔ رقیب", mobile="09121110008")
    make_staff_profile(outsider, skills=(Skill.PHOTOGRAPHER,))
    foreign = applied(world, client_for, user=outsider, post=world.rival_post)
    for method, url in (
        ("get", f"/jobs/{world.rival_post.id}/edit"),
        ("post", f"/jobs/{world.rival_post.id}/edit"),
        ("get", f"/jobs/{world.rival_post.id}/applicants"),
        ("post", f"/jobs/{world.rival_post.id}/close"),
        ("post", f"/jobs/{world.rival_post.id}/reopen"),
        ("get", f"/jobs/manage/applications/{foreign.id}"),
        ("post", f"/jobs/manage/applications/{foreign.id}/accept"),
        ("post", f"/jobs/manage/applications/{foreign.id}/reject"),
    ):
        assert getattr(mc, method)(url, data=form_data()).status_code == 404, (method, url)
    assert reload(foreign).status == ApplicationStatus.PENDING
    assert reload(world.rival_post).status == JobPostStatus.OPEN
    assert count_rows(Assignment) == 0


def test_a_manager_cannot_create_a_post_for_a_foreign_atelier(world, mc):
    response = mc.post("/jobs/new", data=form_data(atelier_id=str(world.rival.id)))
    assert response.status_code == 302  # one atelier: the form field is ignored and the post is mine
    db.session.expire_all()
    post = db.session.scalar(sa.select(JobPost).where(JobPost.title == "عکاس برای مراسم جمعه"))
    assert post.atelier_id == world.atelier.id


def test_a_manager_with_two_ateliers_must_pick_one_of_his_own(world, mc, make_atelier):
    second = make_atelier(world.manager, name="شعبهٔ دوم", city="اصفهان")
    bad = mc.post("/jobs/new", data=form_data(atelier_id=str(world.rival.id)))
    assert bad.status_code == 422
    good = mc.post("/jobs/new", data=form_data(atelier_id=str(second.id)))
    assert good.status_code == 302
    db.session.expire_all()
    assert db.session.scalar(sa.select(JobPost.atelier_id).where(JobPost.title == "عکاس برای مراسم جمعه")) == second.id
    assert "عکاس رقیب" not in body(mc.get("/jobs/manage?tab=all"))


# --------------------------------------------------------------------------
# lists, pagination, escaping
# --------------------------------------------------------------------------


def test_manage_tabs_and_pagination(world, mc, make_job_post):
    for n in range(12):
        make_job_post(world.atelier, date=day(30 + n), title=f"آگهی {n}")
    make_job_post(world.atelier, date=day(50), title="بسته", status=JobPostStatus.CLOSED)
    assert body(mc.get("/jobs/manage?tab=open")).count("mk-job__body") == 10
    assert body(mc.get("/jobs/manage?tab=open&page=2")).count("mk-job__body") == 3
    assert body(mc.get("/jobs/manage?tab=closed")).count("mk-job__body") == 1
    assert body(mc.get("/jobs/manage?tab=all")).count("mk-job__body") == 10
    assert body(mc.get("/jobs/manage?tab=nonsense")).count("mk-job__body") == 10  # falls back to the open tab


def test_inbox_tabs_and_empty_state(world, mc, client_for):
    assert "درخواست جدیدی در انتظار نیست" in body(mc.get("/jobs/manage/inbox"))
    application = applied(world, client_for)
    assert "سارا نیرو" in body(mc.get("/jobs/manage/inbox"))
    mc.post(f"/jobs/manage/applications/{application.id}/reject", data={})
    assert "سارا نیرو" not in body(mc.get("/jobs/manage/inbox"))
    assert "سارا نیرو" in body(mc.get("/jobs/manage/inbox?tab=rejected"))


def test_applicant_text_is_escaped_in_the_inbox_and_the_review_page(world, mc, client_for):
    payload = '<script>alert(1)</script><b onmouseover=x>'
    application = applied(world, client_for, message=payload)
    world.staff.full_name = payload
    world.profile.bio = payload
    db.session.commit()
    for url in ("/jobs/manage/inbox", f"/jobs/{world.post.id}/applicants", f"/jobs/manage/applications/{application.id}"):
        html = body(mc.get(url))
        assert "<script>alert(1)" not in html and "<b onmouseover" not in html, url
        assert "&lt;script&gt;alert(1)" in html, url


def test_the_nav_badge_counts_pending_applications(world, mc, client_for):
    applied(world, client_for)
    html = body(mc.get("/jobs/manage"))
    assert 'class="nav__badge">۱<' in html


def test_pay_preview_endpoint_uses_the_payroll_formula(world, mc):
    response = mc.post("/jobs/api/pay-preview", json={**{"base": 1_000_000, "commission_per_unit": 9_000, "bonus_tiers": BIG_TERMS["bonus_tiers"]}, "units": 80})
    data = response.get_json()["data"]
    assert (data["base"], data["commission"], data["bonus"], data["total"]) == (1_000_000, 720_000, 150_000, 1_870_000)
    assert data["sentence"] == "اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان"
    bad = mc.post("/jobs/api/pay-preview", json={"base": "x"})
    assert bad.status_code == 422 and bad.get_json()["ok"] is False


def _count_queries(client, urls):
    from sqlalchemy import event

    counter = {"n": 0}

    def count(conn, cursor, statement, *rest):  # noqa: ANN001
        counter["n"] += 1

    for url in urls:
        client.get(url)  # warm up
    event.listen(db.engine, "before_cursor_execute", count)
    try:
        results = []
        for url in urls:
            counter["n"] = 0
            client.get(url)
            results.append(counter["n"])
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    return results


def test_list_pages_do_not_query_per_row(world, mc, sc, make_job_post, make_event):
    posts = []
    for n in range(10):
        event = make_event(world.hall, world.atelier, date=day(60 + n))
        posts.append(make_job_post(world.atelier, date=event.date, title=f"آگهی {n}", event_id=event.id))
    for post in posts:
        sc.post(f"/jobs/{post.id}/apply", data={"message": "سلام"})
    for post in posts[:4]:
        db.session.expire_all()
    manage, inbox, mine = _count_queries(mc, ["/jobs/manage?tab=all", "/jobs/manage/inbox"]) + _count_queries(sc, ["/jobs/applications"])
    assert manage <= 16 and inbox <= 16 and mine <= 16  # 9-14 each, however many rows (per-row loads would add 20+)
