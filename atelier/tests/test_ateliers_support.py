"""Shared fixtures and helpers of the ``test_ateliers_*`` files (this file holds no tests).

``world`` builds a small, realistic universe::

    manager (atelier_manager) -> atelier "اتلیه ستاره" (Shiraz)
        hall "تالار گل‌ها" (another user manages it)
        event  = confirmed, in 7 days     <- the one most tests staff
        later  = confirmed, in 20 days
        member = roster photographer (no account)
        staff  = listed platform photographer (account + profile)
    rival (atelier_manager) -> a second atelier with its own confirmed event
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.services.constants import Role, Skill

TODAY = dt.date(2025, 7, 10)
BIG_TERMS = {
    "base": 1_000_000,
    "commission_per_unit": 9_000,
    "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
}


def day(offset: int) -> dt.date:
    return TODAY + dt.timedelta(days=offset)


@pytest.fixture
def now(clock):
    """Freeze the app clock at 2025-07-10 12:00 Iran time (so ``security.today()`` is :data:`TODAY`)."""
    clock.set("2025-07-10 08:30:00")
    return clock


@pytest.fixture
def world(
    now,
    make_user,
    make_atelier,
    make_hall,
    make_event,
    make_roster_member,
    make_staff_profile,
):
    manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر اتلیه")
    atelier = make_atelier(manager, name="اتلیه ستاره", city="شیراز")
    atelier.default_pay_presets = {"photographer": BIG_TERMS}
    db.session.commit()
    hall_manager = make_user(Role.HALL_MANAGER, full_name="مدیر تالار")
    hall = make_hall(hall_manager, name="تالار گل‌ها", city="شیراز", address="شیراز، بلوار چمران")
    event = make_event(hall, atelier, date=day(7), title="عروسی نمونه", guest_count_estimate=300)
    later = make_event(hall, atelier, date=day(20), title="عروسی دوم")
    member = make_roster_member(atelier, display_name="رضا عکاس", skills=(Skill.PHOTOGRAPHER, Skill.EDITOR))
    staff_user = make_user(Role.STAFF, full_name="سارا نیرو", city="شیراز")
    make_staff_profile(staff_user, skills=(Skill.PHOTOGRAPHER, Skill.SELLER), is_listed=True, city="شیراز")

    rival_manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر رقیب")
    rival = make_atelier(rival_manager, name="اتلیه رقیب", city="شیراز")
    rival_event = make_event(hall, rival, date=day(7), title="مراسم رقیب")
    rival_member = make_roster_member(rival, display_name="نیروی رقیب")
    return SimpleNamespace(
        manager=manager,
        atelier=atelier,
        hall_manager=hall_manager,
        hall=hall,
        event=event,
        later=later,
        member=member,
        staff=staff_user,
        rival_manager=rival_manager,
        rival=rival,
        rival_event=rival_event,
        rival_member=rival_member,
    )


@pytest.fixture
def mc(world, client_for):
    """A test client signed in as the atelier manager of ``world``."""
    return client_for(world.manager)


def body(response) -> str:
    return response.get_data(as_text=True)


def reload(obj):
    """Fresh copy of *obj* from the database (requests ran in their own session)."""
    db.session.expire_all()
    return db.session.get(type(obj), obj.id)
