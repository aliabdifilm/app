"""Shared fixtures and helpers of the ``test_halls_*`` files (this file holds no tests).

``world`` builds a small, realistic universe::

    manager (hall_manager)  -> hall "تالار گل‌ها" (Shiraz) and a second hall "باغ بهار"
        draft      = hall, in 10 days, 18:00, no atelier           <- the one most tests offer
        later      = hall, in 12 days, no time, draft
        confirmed  = hall, in 20 days, atelier "ستاره"             (confirmed)
        past       = hall, 5 days ago, atelier "ستاره"             (confirmed, can be rated)
    atelier  (atelier_manager "مدیر ستاره", verified) -> "اتلیه ستاره" (Shiraz)
    rival    (atelier_manager) -> "اتلیه رقیب" (Shiraz)
    outsider (hall_manager)    -> hall "تالار دیگر" with its own event  <- must stay invisible to ``manager``
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.services.constants import EventStatus, Role

TODAY = dt.date(2025, 7, 10)


def day(offset: int) -> dt.date:
    return TODAY + dt.timedelta(days=offset)


@pytest.fixture
def now(clock):
    """Freeze the app clock at 2025-07-10 12:00 Iran time (so ``security.today()`` is :data:`TODAY`)."""
    clock.set("2025-07-10 08:30:00")
    return clock


@pytest.fixture
def world(now, make_user, make_hall, make_atelier, make_event):
    manager = make_user(Role.HALL_MANAGER, full_name="مدیر تالار")
    hall = make_hall(manager, name="تالار گل‌ها", city="شیراز", address="شیراز، بلوار چمران")
    hall2 = make_hall(manager, name="باغ بهار", city="شیراز")

    atelier_manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر ستاره", is_verified=True)
    atelier = make_atelier(atelier_manager, name="اتلیه ستاره", city="شیراز", description="عکاسی عروسی")
    rival_manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر رقیب")
    rival = make_atelier(rival_manager, name="اتلیه رقیب", city="شیراز")

    import datetime as _dt

    draft = make_event(hall, date=day(10), start_time=_dt.time(18, 0), title="عروسی نمونه", guest_count_estimate=300)
    later = make_event(hall, date=day(12))
    confirmed = make_event(hall, atelier, date=day(20), start_time=_dt.time(19, 30))
    past = make_event(hall, atelier, date=day(-5))

    outsider = make_user(Role.HALL_MANAGER, full_name="مدیر دیگر")
    other_hall = make_hall(outsider, name="تالار دیگر", city="تهران")
    other_event = make_event(other_hall, date=day(10), start_time=_dt.time(18, 0))
    return SimpleNamespace(
        manager=manager,
        hall=hall,
        hall2=hall2,
        atelier_manager=atelier_manager,
        atelier=atelier,
        rival_manager=rival_manager,
        rival=rival,
        draft=draft,
        later=later,
        confirmed=confirmed,
        past=past,
        outsider=outsider,
        other_hall=other_hall,
        other_event=other_event,
    )


@pytest.fixture
def hc(world, client_for):
    """A client signed in as the hall manager of ``world``."""
    return client_for(world.manager)


def body(response) -> str:
    return response.get_data(as_text=True)


def reload(obj):
    """Fresh copy of *obj* from the database (requests ran in their own session)."""
    db.session.expire_all()
    return db.session.get(type(obj), obj.id)


def fill(rule: str, ids: dict[str, int]) -> str:
    """``/hall/events/<int:event_id>`` + ``{"event_id": 3}`` -> ``/hall/events/3``."""
    return re.sub(r"<(?:int:)?(\w+)>", lambda m: str(ids.get(m.group(1), 1)), rule)


def status_of(event) -> EventStatus:
    return reload(event).status
