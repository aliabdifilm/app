"""Staff side: applying (and why it is blocked), withdrawing, daily limits."""

from __future__ import annotations

import sqlalchemy as sa
from test_marketplace_support import application_of, body, day, mc, now, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AuditLog, JobApplication, Notification, StaffAvailability
from app.services import staffing
from app.services.constants import ApplicationStatus, AssignmentStatus, AvailabilityStatus, Role

APPLY_URL = "/jobs/{}/apply"


def count_rows(model) -> int:
    db.session.expire_all()
    return db.session.scalar(sa.select(sa.func.count(model.id)))


def notifications_of(user, kind=None):
    db.session.expire_all()
    stmt = sa.select(Notification).where(Notification.user_id == user.id)
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    return list(db.session.scalars(stmt.order_by(Notification.id)))


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_apply_creates_a_pending_application_and_tells_the_manager(world, sc):
    response = sc.post(APPLY_URL.format(world.post.id), data={"message": "سلام، چهار سال سابقه دارم"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/jobs/applications")
    application = application_of(world.staff, world.post)
    assert application.status == ApplicationStatus.PENDING
    assert application.message == "سلام، چهار سال سابقه دارم"
    assert application.decided_at is None
    [note] = notifications_of(world.manager, "application_received")
    assert "سارا نیرو" in note.title + note.body and "عکاس برای عروسی" in note.body
    assert note.url == f"/jobs/manage/applications/{application.id}"
    audit = db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "job_application.created"))
    assert audit.actor_id == world.staff.id and audit.entity_id == application.id


def test_the_message_is_optional(world, sc):
    assert sc.post(APPLY_URL.format(world.post.id), data={}).status_code == 302
    assert application_of(world.staff, world.post).message is None


def test_a_too_long_message_is_rejected_and_the_typed_text_is_kept(world, sc):
    text = "الف" * 501
    response = sc.post(APPLY_URL.format(world.post.id), data={"message": text})
    assert response.status_code == 422
    html = body(response)
    assert "نباید بیش از ۵۰۰ نویسه باشد" in html
    assert text in html
    assert application_of(world.staff, world.post) is None


def test_applying_twice_keeps_one_row_and_says_why(world, sc):
    sc.post(APPLY_URL.format(world.post.id), data={"message": "اول"})
    response = sc.post(APPLY_URL.format(world.post.id), data={"message": "دوم"})
    assert response.status_code == 409
    assert "قبلاً ثبت شده" in body(response)
    assert count_rows(JobApplication) == 1
    assert application_of(world.staff, world.post).message == "اول"


def test_the_detail_page_shows_the_status_after_applying(world, sc):
    sc.post(APPLY_URL.format(world.post.id), data={"message": "سلام"})
    html = body(sc.get(f"/jobs/{world.post.id}"))
    assert "درخواست شما" in html and "در انتظار بررسی" in html
    assert 'name="message"' not in html  # no second form
    assert "انصراف از درخواست" in html


# --------------------------------------------------------------------------
# blocked: skill, date, state
# --------------------------------------------------------------------------


def test_applying_without_the_role_skill_is_blocked_with_a_clear_message(world, client_for):
    response = client_for(world.other).post(APPLY_URL.format(world.post.id), data={"message": "من هم هستم"})  # other = editor only
    assert response.status_code == 422
    assert "مهارت «عکاس» را در پروفایل خود ثبت کرده باشید" in body(response)
    assert application_of(world.other, world.post) is None


def test_applying_without_any_profile_is_blocked(world, make_user, client_for):
    bare = make_user(Role.STAFF, full_name="بدون پروفایل")
    response = client_for(bare).post(APPLY_URL.format(world.post.id), data={})
    assert response.status_code == 422 and "مهارت" in body(response)
    assert count_rows(JobApplication) == 0


def test_the_detail_page_shows_the_reason_instead_of_a_form_when_the_skill_is_missing(world, client_for):
    html = body(client_for(world.other).get(f"/jobs/{world.post.id}"))
    assert "ارسال درخواست ممکن نیست" in html
    assert "مهارت «عکاس»" in html
    assert 'name="message"' not in html  # no apply form, only the reason


def test_a_date_conflict_blocks_the_application(world, make_event, make_assignment, sc):
    elsewhere = make_event(world.hall, world.rival, date=world.post.date, title="جای دیگر")
    make_assignment(elsewhere, "photographer", user=world.staff, status="confirmed")
    response = sc.post(APPLY_URL.format(world.post.id), data={"message": "سلام"})
    assert response.status_code == 409
    html = body(response)
    assert "در مراسم دیگری افیش شده‌اید" in html
    assert application_of(world.staff, world.post) is None


def test_a_conflict_message_does_not_leak_the_other_event(world, make_event, make_assignment, sc):
    elsewhere = make_event(world.hall, world.rival, date=world.post.date, title="مراسم محرمانه رقیب")
    make_assignment(elsewhere, "photographer", user=world.staff, status="invited")
    html = body(sc.post(APPLY_URL.format(world.post.id), data={}))
    assert "مراسم محرمانه رقیب" not in html


def test_an_unavailable_day_blocks_the_application(world, sc):
    db.session.add(StaffAvailability(user_id=world.staff.id, date=world.post.date, status=AvailabilityStatus.UNAVAILABLE))
    db.session.commit()
    response = sc.post(APPLY_URL.format(world.post.id), data={})
    assert response.status_code == 409
    assert "در دسترس نیستم" in body(response)
    assert application_of(world.staff, world.post) is None


def test_a_declined_assignment_does_not_count_as_a_conflict(world, make_event, make_assignment, sc):
    elsewhere = make_event(world.hall, world.rival, date=world.post.date)
    make_assignment(elsewhere, "photographer", user=world.staff, status="declined")
    assert sc.post(APPLY_URL.format(world.post.id), data={}).status_code == 302


def test_closed_filled_expired_and_event_closed_posts_refuse_applications(world, make_job_post, sc):
    for status, text in (("closed", "بسته شده"), ("filled", "تکمیل شده")):
        post = make_job_post(world.atelier, date=day(20), title=status, status=status)
        response = sc.post(APPLY_URL.format(post.id), data={})
        assert response.status_code == 409 and text in body(response)
    past = make_job_post(world.atelier, date=day(-1), title="گذشته")
    assert "گذشته است" in body(sc.post(APPLY_URL.format(past.id), data={}))
    world.event.status = "cancelled"
    db.session.commit()
    assert "دیگر برگزار نمی‌شود" in body(sc.post(APPLY_URL.format(world.post.id), data={}))
    assert count_rows(JobApplication) == 0


def test_a_hidden_or_missing_post_is_404(world, make_job_post, sc):
    hidden = make_job_post(world.atelier, date=day(20), status="hidden")
    assert sc.post(APPLY_URL.format(hidden.id), data={}).status_code == 404
    assert sc.post(APPLY_URL.format(99999), data={}).status_code == 404


def test_only_staff_can_apply(world, client_for):
    for user in (world.manager, world.hall_manager, world.admin):
        assert client_for(user).post(APPLY_URL.format(world.post.id), data={}).status_code == 403
    assert count_rows(JobApplication) == 0


def test_an_account_waiting_for_approval_is_sent_to_the_pending_page(world, make_user, client_for):
    pending = make_user(Role.STAFF, status="pending")
    response = client_for(pending).post(APPLY_URL.format(world.post.id), data={})
    assert response.status_code == 302 and "/auth/pending" in response.headers["Location"]


# --------------------------------------------------------------------------
# withdraw and re-apply
# --------------------------------------------------------------------------


def test_withdraw_a_pending_application_then_apply_again(world, sc):
    sc.post(APPLY_URL.format(world.post.id), data={"message": "اول"})
    application = application_of(world.staff, world.post)
    response = sc.post(f"/jobs/applications/{application.id}/withdraw")
    assert response.status_code == 302
    assert application_of(world.staff, world.post).status == ApplicationStatus.WITHDRAWN
    assert any("پس گرفت" in n.body for n in notifications_of(world.manager))

    again = sc.post(APPLY_URL.format(world.post.id), data={"message": "دوباره"})
    assert again.status_code == 302
    revived = application_of(world.staff, world.post)
    assert revived.id == application.id and revived.status == ApplicationStatus.PENDING and revived.message == "دوباره"
    assert count_rows(JobApplication) == 1


def test_a_rejected_applicant_cannot_apply_again(world, sc, mc):
    sc.post(APPLY_URL.format(world.post.id), data={})
    application = application_of(world.staff, world.post)
    mc.post(f"/jobs/manage/applications/{application.id}/reject", data={"message": ""})
    response = sc.post(APPLY_URL.format(world.post.id), data={})
    assert response.status_code == 409 and "رد شده است" in body(response)


def test_only_the_applicant_can_withdraw(world, sc, client_for, make_user):
    sc.post(APPLY_URL.format(world.post.id), data={})
    application = application_of(world.staff, world.post)
    stranger = make_user(Role.STAFF)
    assert client_for(stranger).post(f"/jobs/applications/{application.id}/withdraw").status_code == 404
    assert client_for(world.manager).post(f"/jobs/applications/{application.id}/withdraw").status_code == 403
    assert application_of(world.staff, world.post).status == ApplicationStatus.PENDING


def test_withdrawing_an_accepted_application_cancels_the_afish_and_reopens_the_post(world, sc, mc):
    sc.post(APPLY_URL.format(world.post.id), data={})
    application = application_of(world.staff, world.post)
    assert mc.post(f"/jobs/manage/applications/{application.id}/accept").status_code == 302
    assert reload(world.post).status == "filled"

    assert sc.post(f"/jobs/applications/{application.id}/withdraw").status_code == 302
    db.session.expire_all()
    assert application_of(world.staff, world.post).status == ApplicationStatus.WITHDRAWN
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.job_application_id == application.id))
    assert assignment.status == AssignmentStatus.CANCELLED
    assert reload(world.post).status == "open"  # the slot is free again


def test_my_applications_lists_only_my_own(world, sc, client_for):
    sc.post(APPLY_URL.format(world.post.id), data={"message": "پیام من"})
    mine = body(sc.get("/jobs/applications"))
    assert "عکاس برای عروسی" in mine and "پیام من" in mine
    assert "عکاس رقیب" not in mine
    others = body(client_for(world.other).get("/jobs/applications"))
    assert "عکاس برای عروسی" not in others and "هنوز درخواستی نفرستاده‌اید" in others


def test_my_applications_filter_tabs_and_pagination(world, make_job_post, sc):
    for n in range(12):
        post = make_job_post(world.atelier, date=day(40 + n), title=f"آگهی {n}", city="شیراز")
        sc.post(APPLY_URL.format(post.id), data={})
    assert body(sc.get("/jobs/applications")).count("mk-app__body") == 10
    assert body(sc.get("/jobs/applications?page=2")).count("mk-app__body") == 2
    assert "درخواستی در این بخش نیست" in body(sc.get("/jobs/applications?tab=accepted"))


# --------------------------------------------------------------------------
# spam protection
# --------------------------------------------------------------------------


def test_the_daily_application_limit(app, world, make_job_post, sc, clock):
    app.config["MARKETPLACE_MAX_APPLICATIONS_PER_DAY"] = 2
    posts = [make_job_post(world.atelier, date=day(30 + n), title=f"p{n}") for n in range(3)]
    assert sc.post(APPLY_URL.format(posts[0].id), data={}).status_code == 302
    assert sc.post(APPLY_URL.format(posts[1].id), data={}).status_code == 302
    blocked = sc.post(APPLY_URL.format(posts[2].id), data={})
    assert blocked.status_code == 429
    assert "بیش از حد مجاز" in body(blocked)
    assert application_of(world.staff, posts[2]) is None
    clock.advance(hours=25)  # the 24 h window has passed
    assert sc.post(APPLY_URL.format(posts[2].id), data={}).status_code == 302


# --------------------------------------------------------------------------
# JSON callers
# --------------------------------------------------------------------------


def test_apply_answers_json_for_fetch_callers(world, sc):
    ok = sc.post(APPLY_URL.format(world.post.id), json={"message": "سلام"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    again = sc.post(APPLY_URL.format(world.post.id), json={"message": "سلام"})
    assert again.status_code == 409
    payload = again.get_json()
    assert payload["ok"] is False and payload["code"] == "duplicate" and "قبلاً" in payload["error"]


def test_a_completed_collaboration_cannot_be_withdrawn(world, sc, mc):
    sc.post(APPLY_URL.format(world.post.id), data={})
    application = application_of(world.staff, world.post)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    staffing.change_event_status(world.event, "done", world.manager)
    db.session.commit()
    response = sc.post(f"/jobs/applications/{application.id}/withdraw", follow_redirects=True)
    assert "انجام شده است و قابل انصراف نیست" in body(response)
    assert application_of(world.staff, world.post).status == ApplicationStatus.ACCEPTED
