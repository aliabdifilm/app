"""Shared fixtures and helpers of the ``test_marketplace_*`` files (this file holds no tests).

``world`` builds a small, realistic universe::

    manager (atelier_manager, verified) -> atelier "اتلیه ستاره" (Shiraz)
        hall "تالار گل‌ها" (another user manages it)
        event = confirmed, in 7 days            <- the afish the accepted people land on
        post  = open "عکاس برای عروسی", photographer, AGENTS.md terms (1,000,000 + 9,000/photo + 150,000 from 70)
    staff    = active staff "سارا نیرو": photographer + seller, listed, Shiraz
    other    = a second staff member without the photographer skill
    rival_manager -> a second atelier with its own event and post
    hall_manager, admin
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.services.constants import JobPostStatus, Role, Skill

TODAY = dt.date(2025, 7, 10)
BIG_TERMS = {
    "base": 1_000_000,
    "commission_per_unit": 9_000,
    "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
}


def day(offset: int) -> dt.date:
    return TODAY + dt.timedelta(days=offset)


def body(response) -> str:
    return response.get_data(as_text=True)


def reload(obj):
    """Fresh copy of *obj* from the database (requests ran in their own session)."""
    db.session.expire_all()
    return db.session.get(type(obj), obj.id)


def csrf_free(text: str) -> str:
    """Page text without script/style blocks, for 'does this string appear' assertions."""
    return re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.S)


@pytest.fixture
def now(clock):
    """Freeze the app clock at 2025-07-10 12:00 Iran time (so ``security.today()`` is :data:`TODAY`)."""
    clock.set("2025-07-10 08:30:00")
    return clock


@pytest.fixture
def world(now, make_user, make_atelier, make_hall, make_event, make_staff_profile, make_job_post):
    manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر اتلیه", is_verified=True)
    atelier = make_atelier(manager, name="اتلیه ستاره", city="شیراز", phone="07112345678")
    hall_manager = make_user(Role.HALL_MANAGER, full_name="مدیر تالار")
    hall = make_hall(hall_manager, name="تالار گل‌ها", city="شیراز")
    event = make_event(hall, atelier, date=day(7), title="عروسی نمونه")
    staff = make_user(Role.STAFF, full_name="سارا نیرو", city="شیراز", mobile="09121110001")
    profile = make_staff_profile(
        staff, skills=(Skill.PHOTOGRAPHER, Skill.SELLER), is_listed=True, city="شیراز", experience_years=4,
        bio="عکاس عروسی با چهار سال سابقه",
    )
    other = make_user(Role.STAFF, full_name="رضا ادیتور", mobile="09121110002")
    other_profile = make_staff_profile(other, skills=(Skill.EDITOR,), is_listed=True, city="تهران")
    post = make_job_post(
        atelier, Skill.PHOTOGRAPHER, date=day(7), title="عکاس برای عروسی", city="شیراز",
        base_pay=BIG_TERMS["base"], commission_per_unit=BIG_TERMS["commission_per_unit"],
        bonus_tiers=BIG_TERMS["bonus_tiers"], slots=1, event_id=event.id,
    )
    rival_manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر رقیب")
    rival = make_atelier(rival_manager, name="اتلیه رقیب", city="تهران")
    rival_event = make_event(hall, rival, date=day(9), title="مراسم رقیب")
    rival_post = make_job_post(rival, Skill.PHOTOGRAPHER, date=day(9), title="عکاس رقیب", city="تهران", event_id=rival_event.id)
    admin = make_user(Role.ADMIN, full_name="ادمین")
    return SimpleNamespace(
        manager=manager,
        atelier=atelier,
        hall_manager=hall_manager,
        hall=hall,
        event=event,
        staff=staff,
        profile=profile,
        other=other,
        other_profile=other_profile,
        post=post,
        rival_manager=rival_manager,
        rival=rival,
        rival_event=rival_event,
        rival_post=rival_post,
        admin=admin,
    )


@pytest.fixture
def mc(world, client_for):
    """A client signed in as the atelier manager of ``world``."""
    return client_for(world.manager)


@pytest.fixture
def sc(world, client_for):
    """A client signed in as the staff member of ``world``."""
    return client_for(world.staff)


@pytest.fixture
def apply_for(client_for):
    """``apply_for(user, post, message="...")`` -> the response of the apply POST."""

    def apply(user, post, message="سلام، آماده‌ام"):
        return client_for(user).post(f"/jobs/{post.id}/apply", data={"message": message})

    return apply


def application_of(user, post):
    from app.models import JobApplication
    import sqlalchemy as sa

    db.session.expire_all()
    return db.session.scalar(sa.select(JobApplication).where(JobApplication.user_id == user.id, JobApplication.post_id == post.id))


def set_status(post, status):
    post.status = JobPostStatus.coerce(status)
    db.session.commit()
