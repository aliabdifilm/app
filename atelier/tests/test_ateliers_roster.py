"""Fixed staff (نیروهای ثابت): CRUD, linking to platform accounts, default terms, isolation, validation."""

from __future__ import annotations

import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, body, mc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import AuditLog, RosterMember
from app.services import staffing
from app.services.constants import Role, Skill


def create(client, **extra):
    data = {"display_name": "کریم ادیتور", "skills": ["editor"], **extra}
    return client.post("/atelier/roster/new", data=data)


def test_create_member_without_an_account(world, mc):
    response = create(mc, mobile="۰۹۱۳۱۲۳۴۵۶۷", notes="شیفت شب")
    assert response.status_code == 302
    member = db.session.scalar(sa.select(RosterMember).where(RosterMember.display_name == "کریم ادیتور"))
    assert member.atelier_id == world.atelier.id and member.user_id is None
    assert member.mobile == "09131234567" and member.skills == ["editor"] and member.is_active
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "roster.created")) is not None
    page = body(mc.get("/atelier/roster"))
    assert "کریم ادیتور" in page and "بدون حساب کاربری" in page and "۰۹۱۳۱۲۳۴۵۶۷" in page


def test_member_with_a_registered_number_is_linked_to_the_account(world, mc):
    response = create(mc, display_name="سارا", mobile=world.staff.mobile, skills=["photographer", "seller"])
    assert response.status_code == 302
    member = db.session.scalar(sa.select(RosterMember).where(RosterMember.display_name == "سارا"))
    assert member.user_id == world.staff.id
    assert "حساب کاربری متصل" in body(mc.get("/atelier/roster"))
    # the afish of a linked member reaches the account (notification + same-person detection)
    assignment = staffing.assign_person(world.event, "photographer", roster_member=member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    assert assignment.person_user_id == world.staff.id


def test_only_staff_accounts_are_linked(world, mc):
    response = create(mc, display_name="مدیر", mobile=world.rival_manager.mobile)
    assert response.status_code == 302
    assert db.session.scalar(sa.select(RosterMember).where(RosterMember.display_name == "مدیر")).user_id is None


def test_the_same_account_cannot_be_on_the_roster_twice(world, mc):
    assert create(mc, display_name="سارا", mobile=world.staff.mobile).status_code == 302
    again = create(mc, display_name="سارا دوم", mobile=world.staff.mobile)
    assert again.status_code == 422 and "قبلاً" in body(again)


def test_validation_keeps_the_typed_values(world, mc):
    response = mc.post("/atelier/roster/new", data={"display_name": "", "mobile": "123", "skills": ["editor", "pilot"], "notes": "x" * 600})
    assert response.status_code == 422
    page = body(response)
    assert "نام را وارد کنید" in page and "شمارهٔ موبایل نامعتبر" in page and "مهارت نامعتبر" in page and "بیش از" in page
    assert 'value="123"' in page
    no_skill = mc.post("/atelier/roster/new", data={"display_name": "الف"})
    assert no_skill.status_code == 422 and "دست‌کم یک مهارت" in body(no_skill)
    assert db.session.scalar(sa.select(sa.func.count(RosterMember.id))) == 2  # only the fixtures' members


def test_default_terms_are_stored_only_for_the_chosen_skills(world, mc):
    response = create(
        mc,
        skills=["editor"],
        **{
            "base__editor": "900000",
            "commission__editor": "4000",
            "tier_threshold__editor[]": ["60"],
            "tier_amount__editor[]": ["80000"],
            "base__seller": "500000",  # not a skill of this member: ignored
        },
    )
    assert response.status_code == 302
    member = db.session.scalar(sa.select(RosterMember).where(RosterMember.display_name == "کریم ادیتور"))
    assert member.default_terms == {
        "editor": {"base": 900_000, "commission_per_unit": 4_000, "bonus_tiers": [{"threshold": 60, "amount": 80_000}]}
    }
    assert member.terms_for("editor")["base"] == 900_000
    assert member.terms_for("seller") is None


def test_edit_member(world, mc):
    page = body(mc.get(f"/atelier/roster/{world.member.id}/edit"))
    assert "رضا عکاس" in page and "عکاس" in page
    response = mc.post(
        f"/atelier/roster/{world.member.id}/edit",
        data={"display_name": "رضا محمدی", "skills": ["photographer"], "is_active": "1", "mobile": ""},
    )
    assert response.status_code == 302
    member = reload(world.member)
    assert member.display_name == "رضا محمدی" and member.skills == ["photographer"] and member.mobile is None
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "roster.updated")) is not None


def test_edit_validation_error_keeps_the_page(world, mc):
    response = mc.post(f"/atelier/roster/{world.member.id}/edit", data={"display_name": "", "skills": ["editor"]})
    assert response.status_code == 422 and "ویرایش «رضا عکاس»" in body(response)
    assert reload(world.member).display_name == "رضا عکاس"


def test_deactivate_hides_the_member_from_the_picker_but_keeps_history(world, mc):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    assert mc.post(f"/atelier/roster/{world.member.id}/toggle").status_code == 302
    assert reload(world.member).is_active is False
    pick = body(mc.get(f"/atelier/events/{world.event.id}/afish/new?role=photographer"))
    assert 'at-member__name">رضا عکاس' not in pick  # (the flash message names him, the picker must not list him)
    assert "رضا عکاس" in body(mc.get("/atelier/roster?state=inactive"))
    assert 'at-member__name">رضا عکاس' not in body(mc.get("/atelier/roster"))
    assert 'at-member__name">رضا عکاس' in body(mc.get("/atelier/roster?state=all"))
    assert mc.post(f"/atelier/roster/{world.member.id}/toggle").status_code == 302
    assert reload(world.member).is_active is True


def test_delete_only_when_never_staffed(world, mc, make_roster_member):
    fresh = make_roster_member(world.atelier, display_name="تازه")
    fresh_id = fresh.id
    assert mc.post(f"/atelier/roster/{fresh_id}/delete").status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(RosterMember.id)).where(RosterMember.id == fresh_id)) == 0
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    assert mc.post(f"/atelier/roster/{world.member.id}/delete").status_code == 302
    assert reload(world.member) is not None  # history keeps the row
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "roster.deleted")) is not None


def test_list_filters_search_and_pagination(world, mc, make_roster_member):
    for n in range(24):
        make_roster_member(world.atelier, display_name=f"نیروی شماره {n}", skills=(Skill.PRESENTER,))
    page = body(mc.get("/atelier/roster"))
    assert 'aria-label="صفحه‌بندی"' in page
    assert "نیروی شماره 0" in body(mc.get("/atelier/roster?q=شماره 0"))
    presenters = body(mc.get("/atelier/roster?skill=presenter"))
    assert "رضا عکاس" not in presenters and "نیروی شماره" in presenters
    assert "رضا عکاس" in body(mc.get("/atelier/roster?skill=editor"))
    found = body(mc.get("/atelier/roster?q=رضا"))
    assert "رضا عکاس" in found and "نیروی شماره" not in found
    assert mc.get("/atelier/roster?q=%25&skill=zzz&state=zzz&page=-4").status_code == 200  # junk is harmless


def test_empty_roster_explains_the_next_step(app, make_user, make_atelier, client_for):
    manager = make_user(Role.ATELIER_MANAGER)
    make_atelier(manager)
    page = body(client_for(manager).get("/atelier/roster"))
    assert "هنوز نیروی ثابتی ثبت نکرده‌اید" in page and "حساب کاربری لازم نیست" in page


def test_add_user_from_the_directory(world, mc, make_user, make_staff_profile):
    hidden = make_user(Role.STAFF, full_name="پنهان")
    make_staff_profile(hidden, is_listed=False)
    assert mc.post("/atelier/roster/add-user", data={"user_id": hidden.id}).status_code == 302
    assert db.session.scalar(sa.select(RosterMember).where(RosterMember.user_id == hidden.id)) is None  # not listed, no token

    response = mc.post("/atelier/roster/add-user", data={"user_id": world.staff.id})
    assert response.status_code == 302 and "/edit" in response.headers["Location"]
    member = db.session.scalar(sa.select(RosterMember).where(RosterMember.user_id == world.staff.id))
    assert member.atelier_id == world.atelier.id and member.display_name == "سارا نیرو"
    assert set(member.skills) == {"photographer", "seller"}
    again = mc.post("/atelier/roster/add-user", data={"user_id": world.staff.id})
    assert again.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(RosterMember.id)).where(RosterMember.user_id == world.staff.id)) == 1
