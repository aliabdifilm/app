"""The talent directory, profiles (privacy by design) and direct invitations."""

from __future__ import annotations

import re

import sqlalchemy as sa
from test_marketplace_support import BIG_TERMS, application_of, body, day, mc, now, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AuditLog, Notification, PortfolioItem, Review, StaffAvailability
from app.services.constants import (
    AssignmentSource,
    AssignmentStatus,
    AvailabilityStatus,
    Role,
    Skill,
    UserStatus,
)
from app.services.money import to_persian_digits


def names(html: str) -> list[str]:
    return re.findall(r'<h2 class="mk-person__name"><a [^>]*>([^<]*)</a>', html)


def has_number(html: str, number: str) -> bool:
    return number in html or to_persian_digits(number) in html


def make_talent(make_user, make_staff_profile, name, number, skills=(Skill.PHOTOGRAPHER,), **fields):
    user = make_user(Role.STAFF, full_name=name, mobile=number)
    profile = make_staff_profile(user, skills=skills, is_listed=fields.pop("is_listed", True), **fields)
    return user, profile


# --------------------------------------------------------------------------
# who sees the directory, who is in it
# --------------------------------------------------------------------------


def test_managers_and_admins_may_browse_staff_may_not(app, world, client_for):
    for user in (world.manager, world.hall_manager, world.admin):
        assert client_for(user).get("/talent").status_code == 200
    assert client_for(world.staff).get("/talent").status_code == 403
    anonymous = app.test_client().get("/talent")
    assert anonymous.status_code == 302 and "/auth/login" in anonymous.headers["Location"]


def test_only_listed_active_staff_appear(world, mc, make_user, make_staff_profile):
    make_talent(make_user, make_staff_profile, "پنهان", "09121110010", is_listed=False)
    suspended, _ = make_talent(make_user, make_staff_profile, "معلق", "09121110011")
    suspended.status = UserStatus.SUSPENDED
    db.session.commit()
    pending = make_user(Role.STAFF, full_name="در انتظار", status="pending")
    make_staff_profile(pending, is_listed=True)
    listed = names(body(mc.get("/talent")))
    assert set(listed) == {"سارا نیرو", "رضا ادیتور"}


def test_a_hall_manager_or_atelier_manager_profile_is_never_listed(world, mc, make_staff_profile):
    make_staff_profile(world.hall_manager, is_listed=True)  # a profile on a non-staff account must not be listed
    assert "مدیر تالار" not in names(body(mc.get("/talent")))


def test_filters_by_skill_city_rating_and_name(world, mc, make_user, make_staff_profile):
    top, profile = make_talent(make_user, make_staff_profile, "ستاره", "09121110012", skills=(Skill.PHOTOGRAPHER, Skill.EDITOR), city="شیراز")
    profile.rating_avg, profile.rating_count = 4.5, 8
    low, low_profile = make_talent(make_user, make_staff_profile, "کم‌امتیاز", "09121110013", city="اصفهان")
    low_profile.rating_avg, low_profile.rating_count = 2.0, 3
    db.session.commit()
    assert set(names(body(mc.get("/talent?skill=photographer")))) == {"سارا نیرو", "ستاره", "کم‌امتیاز"}
    assert set(names(body(mc.get("/talent?skill=editor")))) == {"رضا ادیتور", "ستاره"}
    assert set(names(body(mc.get("/talent?city=اصفهان")))) == {"کم‌امتیاز"}
    assert set(names(body(mc.get("/talent?min_rating=4")))) == {"ستاره"}
    assert set(names(body(mc.get("/talent?min_rating=2")))) == {"ستاره", "کم‌امتیاز"}
    assert set(names(body(mc.get("/talent?name=ستا")))) == {"ستاره"}
    assert set(names(body(mc.get("/talent?skill=photographer&city=شیراز&min_rating=4")))) == {"ستاره"}
    assert names(body(mc.get("/talent")))[0] == "ستاره"  # best rating first


def test_name_search_treats_percent_and_underscore_literally(world, mc):
    assert names(body(mc.get("/talent?name=%25"))) == []
    assert names(body(mc.get("/talent?name=_"))) == []


def test_availability_on_a_jalali_date(world, mc, make_event, make_assignment):
    # busy: confirmed elsewhere that day.  unavailable: marked the day.  other: free.
    busy_event = make_event(world.hall, world.rival, date=day(20))
    make_assignment(busy_event, "photographer", user=world.staff, status="confirmed")
    db.session.add(StaffAvailability(user_id=world.other.id, date=day(20), status=AvailabilityStatus.UNAVAILABLE))
    db.session.commit()
    iso = day(20).isoformat()
    assert names(body(mc.get(f"/talent?on_date={iso}"))) == []
    from app.services import jalali

    jalali_text = to_persian_digits(jalali.to_jalali_str(day(20)))  # typed the way a person would: 1404/05/08
    assert names(body(mc.get(f"/talent?on_date={jalali_text}"))) == []
    assert set(names(body(mc.get(f"/talent?on_date={day(21).isoformat()}")))) == {"سارا نیرو", "رضا ادیتور"}


def test_availability_ignores_cancelled_and_declined_assignments(world, mc, make_event, make_assignment):
    event = make_event(world.hall, world.rival, date=day(20))
    make_assignment(event, "photographer", user=world.staff, status="declined")
    assert "سارا نیرو" in names(body(mc.get(f"/talent?on_date={day(20).isoformat()}")))


def test_availability_counts_roster_linked_assignments(world, mc, make_event, make_assignment, make_roster_member):
    member = make_roster_member(world.rival, user=world.staff)
    event = make_event(world.hall, world.rival, date=day(22))
    make_assignment(event, "photographer", roster_member=member)
    assert "سارا نیرو" not in names(body(mc.get(f"/talent?on_date={day(22).isoformat()}")))


def test_sorting_and_pagination(world, mc, make_user, make_staff_profile):
    for n in range(14):
        make_talent(make_user, make_staff_profile, f"نیرو {n:02d}", f"0913000{n:04d}", experience_years=n)
    page1 = names(body(mc.get("/talent?sort=experience")))
    page2 = names(body(mc.get("/talent?sort=experience&page=2")))
    assert len(page1) == 12 and len(page2) == 4  # 16 listed in total (2 from the fixture)
    assert page1[0] == "نیرو 13"
    assert len(set(page1 + page2)) == 16
    links = re.findall(r'href="(/talent\?[^"]*page=2[^"]*)"', body(mc.get("/talent?sort=experience&skill=photographer")))
    assert links and "sort=experience" in links[0] and "skill=photographer" in links[0]


def test_empty_states(world, mc, client_for, make_user):
    assert "نیرویی با این فیلترها پیدا نشد" in body(mc.get("/talent?city=مشهد"))
    db.session.execute(sa.update(type(world.profile)).values(is_listed=False))
    db.session.commit()
    assert "هنوز نیرویی در جستجو نمایش داده نمی‌شود" in body(mc.get("/talent"))


# --------------------------------------------------------------------------
# privacy: no mobile numbers unless a relationship exists
# --------------------------------------------------------------------------


def test_the_directory_and_profile_never_show_the_mobile_of_a_stranger(world, mc):
    for url in ("/talent", f"/talent/{world.staff.id}", f"/talent/{world.other.id}"):
        html = body(mc.get(url))
        for number in (world.staff.mobile, world.other.mobile):
            assert not has_number(html, number), (url, number)
    assert "tel:" not in body(mc.get(f"/talent/{world.staff.id}"))


def test_a_nameless_talent_does_not_show_the_number_as_a_name(world, mc, make_user, make_staff_profile):
    user = make_user(Role.STAFF, full_name="", mobile="09125556677")
    make_staff_profile(user, is_listed=True)
    html = body(mc.get("/talent"))
    assert not has_number(html, "09125556677")
    assert "کاربر بی‌نام" in html


def test_a_name_that_is_a_phone_number_is_hidden_too(world, mc, make_user, make_staff_profile):
    user = make_user(Role.STAFF, full_name="۰۹۱۲۵۵۵۶۶۷۷", mobile="09125556677")
    make_staff_profile(user, is_listed=True)
    assert not has_number(body(mc.get("/talent")), "09125556677")


def test_pending_application_or_invitation_do_not_reveal_the_number(world, mc, client_for):
    client_for(world.staff).post(f"/jobs/{world.post.id}/apply", data={})
    assert not has_number(body(mc.get(f"/talent/{world.staff.id}")), world.staff.mobile)
    assert mc.post(f"/talent/{world.other.id}/invite", data={}).status_code in (404, 422)


def test_an_accepted_application_reveals_the_number(world, mc, client_for):
    client_for(world.staff).post(f"/jobs/{world.post.id}/apply", data={})
    application = application_of(world.staff, world.post)
    mc.post(f"/jobs/manage/applications/{application.id}/accept")
    html = body(mc.get(f"/talent/{world.staff.id}"))
    assert f"tel:{world.staff.mobile}" in html and has_number(html, world.staff.mobile)
    # the rival atelier has no relationship with this person
    rival_html = body(client_for(world.rival_manager).get(f"/talent/{world.staff.id}"))
    assert not has_number(rival_html, world.staff.mobile)


def test_a_committed_assignment_reveals_the_number_but_an_invitation_does_not(world, mc, make_assignment):
    invited = make_assignment(world.event, "seller", user=world.staff, status="invited")
    assert not has_number(body(mc.get(f"/talent/{world.staff.id}")), world.staff.mobile)
    invited.status = AssignmentStatus.ACCEPTED
    db.session.commit()
    assert has_number(body(mc.get(f"/talent/{world.staff.id}")), world.staff.mobile)


def test_a_cancelled_assignment_does_not_reveal_the_number(world, mc, make_assignment):
    make_assignment(world.event, "seller", user=world.staff, status="cancelled")
    assert not has_number(body(mc.get(f"/talent/{world.staff.id}")), world.staff.mobile)


def test_the_hall_manager_sees_the_number_only_through_an_assignment_at_the_hall(world, make_assignment, client_for):
    hall = client_for(world.hall_manager)
    assert not has_number(body(hall.get(f"/talent/{world.staff.id}")), world.staff.mobile)
    make_assignment(world.event, "seller", user=world.staff, status="confirmed")
    assert has_number(body(hall.get(f"/talent/{world.staff.id}")), world.staff.mobile)
    assert "دعوت به همکاری" not in body(hall.get(f"/talent/{world.staff.id}"))  # halls browse only


def test_the_admin_sees_the_contact(world, client_for):
    assert has_number(body(client_for(world.admin).get(f"/talent/{world.staff.id}")), world.staff.mobile)


# --------------------------------------------------------------------------
# the profile page
# --------------------------------------------------------------------------


def test_profile_shows_bio_skills_experience_rating_portfolio_and_reviews(world, mc, make_assignment):
    world.profile.owns_camera = True
    world.profile.expected_base = 800_000
    world.profile.instagram = "sara.photo"
    done = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    db.session.add_all([
        PortfolioItem(user_id=world.staff.id, title="نمونهٔ عروسی", image_path="portfolio/aa/" + "a" * 32 + ".jpg", is_public=True),
        PortfolioItem(user_id=world.staff.id, title="خصوصی", image_path="portfolio/bb/" + "b" * 32 + ".jpg", is_public=False),
        Review(assignment=done, author=world.manager, subject=world.staff, rating=4, comment="کارش عالی بود"),
    ])
    db.session.commit()
    world.profile.refresh_rating()
    db.session.commit()
    html = body(mc.get(f"/talent/{world.staff.id}"))
    assert "عکاس عروسی با چهار سال سابقه" in html
    assert "۴ سال سابقه" in html and "عکاس" in html and "فروشنده" in html
    assert "دوربین" in html and "پایه ۸۰۰٬۰۰۰ تومان" in html
    assert 'href="https://instagram.com/sara.photo"' in html and 'rel="noopener noreferrer nofollow"' in html
    assert "نمونهٔ عروسی" in html and "خصوصی" not in html
    assert "کارش عالی بود" in html and "اتلیه ستاره" in html
    assert 'aria-label="امتیاز ۴٫۰ از ۵"' in html


def test_hidden_reviews_are_not_shown_or_counted(world, mc, make_assignment):
    done = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    review = Review(assignment=done, author=world.manager, subject=world.staff, rating=1, comment="نظر مخفی‌شده", is_hidden=True)
    db.session.add(review)
    db.session.commit()
    world.profile.refresh_rating()
    db.session.commit()
    html = body(mc.get(f"/talent/{world.staff.id}"))
    assert "نظر مخفی‌شده" not in html and "هنوز نظری ثبت نشده است" in html
    assert reload(world.profile).rating_count == 0


def test_a_malicious_instagram_value_is_not_turned_into_a_link(world, mc):
    world.profile.instagram = 'javascript:alert(1)"><script>'
    db.session.commit()
    html = body(mc.get(f"/talent/{world.staff.id}"))
    assert "<script>" not in html and 'href="javascript' not in html and "instagram.com" not in html


def test_user_text_on_the_profile_is_escaped(world, mc, make_assignment):
    payload = "<script>alert(1)</script>"
    world.profile.bio = payload
    world.staff.full_name = payload
    done = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    db.session.add(Review(assignment=done, author=world.manager, subject=world.staff, rating=3, comment=payload))
    db.session.commit()
    for url in (f"/talent/{world.staff.id}", "/talent"):
        html = body(mc.get(url))
        assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html, url


def test_an_unlisted_profile_is_404_unless_there_is_a_relationship(world, mc, client_for, make_user, make_staff_profile):
    hidden, _ = make_talent(make_user, make_staff_profile, "پنهان", "09121110010", is_listed=False)
    assert mc.get(f"/talent/{hidden.id}").status_code == 404
    assert client_for(world.admin).get(f"/talent/{hidden.id}").status_code == 200
    # an application to one of my posts opens the profile (not the number)
    client_for(hidden).post(f"/jobs/{world.post.id}/apply", data={})
    page = mc.get(f"/talent/{hidden.id}")
    assert page.status_code == 200
    assert "نمایش داده نمی‌شود" in body(page) and not has_number(body(page), hidden.mobile)
    # ... but not for the rival atelier
    assert client_for(world.rival_manager).get(f"/talent/{hidden.id}").status_code == 404


def test_missing_non_staff_and_suspended_profiles_are_404(world, mc, make_user, make_staff_profile):
    assert mc.get("/talent/99999").status_code == 404
    assert mc.get(f"/talent/{world.hall_manager.id}").status_code == 404
    suspended, _ = make_talent(make_user, make_staff_profile, "معلق", "09121110011")
    suspended.status = UserStatus.SUSPENDED
    db.session.commit()
    assert mc.get(f"/talent/{suspended.id}").status_code == 404


# --------------------------------------------------------------------------
# inviting
# --------------------------------------------------------------------------

INVITE = {
    "role": "photographer",
    "base": "1,000,000",
    "commission": "9000",
    "tier_threshold[]": ["70"],
    "tier_amount[]": ["150000"],
    "notes": "ساعت ۱۸ حاضر باشید",
}


def invite_data(event, **overrides):
    data = dict(INVITE, event=str(event.id))
    data.update(overrides)
    return data


def test_invite_creates_an_invited_assignment_with_the_snapshot(world, mc):
    response = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/talent/{world.staff.id}")
    db.session.expire_all()
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.user_id == world.staff.id))
    assert assignment.status == AssignmentStatus.INVITED
    assert assignment.source == AssignmentSource.MARKETPLACE and assignment.job_application_id is None
    assert assignment.event_id == world.event.id and assignment.role == "photographer"
    assert assignment.terms_dict() == BIG_TERMS
    assert assignment.notes == "ساعت ۱۸ حاضر باشید" and assignment.created_by_id == world.manager.id
    note = db.session.scalar(sa.select(Notification).where(Notification.user_id == world.staff.id, Notification.kind == "assignment_invited"))
    assert "دعوت" in note.title
    actions = [row.action for row in db.session.scalars(sa.select(AuditLog))]
    assert "talent.invited" in actions and "assignment.created" in actions


def test_the_invite_form_is_prefilled_from_the_atelier_preset(world, mc):
    world.atelier.default_pay_presets = {"photographer": BIG_TERMS}
    db.session.commit()
    html = body(mc.get(f"/talent/{world.staff.id}/invite?event={world.event.id}&role=photographer"))
    assert 'value="1000000"' in html and 'value="9000"' in html
    assert f'<option value="{world.event.id}" selected>' in html


def test_the_invite_form_falls_back_to_the_talents_expected_terms(world, mc):
    world.profile.expected_base = 700_000
    db.session.commit()
    assert 'value="700000"' in body(mc.get(f"/talent/{world.staff.id}/invite"))


def test_the_role_must_be_one_of_the_talents_skills(world, mc):
    response = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event, role="editor"))
    assert response.status_code == 422 and "نقش نامعتبر است" in body(response)
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 0


def test_a_foreign_or_unknown_event_cannot_be_used(world, mc):
    for event_id in (world.rival_event.id, 99999):
        response = mc.post(f"/talent/{world.staff.id}/invite", data=dict(INVITE, event=str(event_id)))
        assert response.status_code == 422 and "مراسم انتخاب‌شده معتبر نیست" in body(response)
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 0


def test_a_busy_talent_needs_an_explicit_override(world, mc, make_event, make_assignment):
    elsewhere = make_event(world.hall, world.rival, date=world.event.date)
    make_assignment(elsewhere, "seller", user=world.staff, status="confirmed")
    refused = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assert refused.status_code == 422
    html = body(refused)
    assert "همان روز مشغول است" in html and "دعوت با علم به تداخل" in html
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 1
    accepted = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event, override="1"))
    assert accepted.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 2


def test_the_ateliers_double_booking_block_cannot_be_overridden(world, mc, make_event, make_assignment):
    world.atelier.block_double_booking = True
    db.session.commit()
    elsewhere = make_event(world.hall, world.rival, date=world.event.date)
    make_assignment(elsewhere, "seller", user=world.staff, status="confirmed")
    response = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event, override="1"))
    assert response.status_code == 422 and "مجاز نیست" in body(response)
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 1


def test_an_unavailable_day_also_needs_the_override(world, mc):
    db.session.add(StaffAvailability(user_id=world.staff.id, date=world.event.date, status=AvailabilityStatus.UNAVAILABLE))
    db.session.commit()
    assert mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event)).status_code == 422
    assert mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event, override="1")).status_code == 302


def test_inviting_the_same_role_twice_is_refused(world, mc):
    assert mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event)).status_code == 302
    again = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assert again.status_code == 422 and "همین نقش را از قبل" in body(again)
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 1


def test_inviting_an_unlisted_stranger_is_404_and_only_managers_may_invite(world, mc, client_for, make_user, make_staff_profile):
    hidden, _ = make_talent(make_user, make_staff_profile, "پنهان", "09121110010", is_listed=False)
    assert mc.get(f"/talent/{hidden.id}/invite").status_code == 404
    assert mc.post(f"/talent/{hidden.id}/invite", data=invite_data(world.event)).status_code == 404
    for user in (world.hall_manager, world.admin, world.staff):
        assert client_for(user).post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event)).status_code == 403


def test_the_daily_invitation_limit(app, world, mc, make_event, clock):
    app.config["MARKETPLACE_MAX_INVITES_PER_DAY"] = 1
    other_event = make_event(world.hall, world.atelier, date=day(20))
    assert mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event)).status_code == 302
    blocked = mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(other_event))
    assert blocked.status_code == 429 and "بیش از حد مجاز" in body(blocked)
    first = db.session.scalar(sa.select(Assignment))
    first.created_at = clock.now()  # (rows get the real clock by default; the test clock is frozen in 2025)
    db.session.commit()
    clock.advance(hours=25)
    assert mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(other_event)).status_code == 302


def test_no_event_to_invite_to_explains_what_to_do(world, mc):
    world.event.status = "cancelled"
    db.session.commit()
    assert "مراسم قابل افیشی ندارید" in body(mc.get(f"/talent/{world.staff.id}/invite"))


# --------------------------------------------------------------------------
# answering an invitation
# --------------------------------------------------------------------------


def test_the_invited_person_accepts_and_the_manager_is_told(world, mc, sc):
    mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    html = body(sc.get("/jobs/invitations"))
    assert "دعوت به‌عنوان عکاس" in html and "اتلیه ستاره" in html and "اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان" in html
    assert "ساعت ۱۸ حاضر باشید" in html
    assignment = db.session.scalar(sa.select(Assignment))
    assert sc.post(f"/jobs/invitations/{assignment.id}/accept").status_code == 302
    assert reload(assignment).status == AssignmentStatus.ACCEPTED
    assert "دعوت بی‌پاسخی ندارید" in body(sc.get("/jobs/invitations"))
    notes = db.session.scalars(sa.select(Notification).where(Notification.user_id == world.manager.id, Notification.kind == "assignment_response")).all()
    assert notes and "پذیرفت" in notes[-1].title


def test_declining_an_invitation(world, mc, sc):
    mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assignment = db.session.scalar(sa.select(Assignment))
    assert sc.post(f"/jobs/invitations/{assignment.id}/decline").status_code == 302
    assert reload(assignment).status == AssignmentStatus.DECLINED
    again = sc.post(f"/jobs/invitations/{assignment.id}/accept", follow_redirects=True)
    assert "قبلاً پاسخ داده‌اید" in body(again)


def test_someone_elses_invitation_is_404(world, mc, client_for):
    mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assignment = db.session.scalar(sa.select(Assignment))
    for user in (world.other, ):
        client = client_for(user)
        assert client.post(f"/jobs/invitations/{assignment.id}/accept").status_code == 404
        assert client.post(f"/jobs/invitations/{assignment.id}/decline").status_code == 404
        assert "دعوت به‌عنوان" not in body(client.get("/jobs/invitations"))
    assert reload(assignment).status == AssignmentStatus.INVITED


def test_accepting_an_invitation_is_refused_when_a_committed_clash_exists(world, mc, sc, make_event, make_assignment):
    mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    elsewhere = make_event(world.hall, world.rival, date=world.event.date)
    make_assignment(elsewhere, "seller", user=world.staff, status="confirmed")
    assignment = db.session.scalar(sa.select(Assignment).where(Assignment.event_id == world.event.id))
    html = body(sc.get("/jobs/invitations"))
    assert "تداخل" in html
    refused = sc.post(f"/jobs/invitations/{assignment.id}/accept", follow_redirects=True)
    assert "مراسم قطعی دیگری دارید" in body(refused)
    assert reload(assignment).status == AssignmentStatus.INVITED


def test_the_invitation_badge_in_the_menu(world, mc, sc):
    mc.post(f"/talent/{world.staff.id}/invite", data=invite_data(world.event))
    assert 'class="nav__badge">۱<' in body(sc.get("/jobs/invitations"))
