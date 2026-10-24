"""Shared fixtures and helpers of the ``test_staff_*`` files (this file holds no tests).

``world`` builds a small, realistic universe (the clock is frozen at 2025-07-10 12:00 Iran time)::

    staff (staff, active)            -> "علی عکاس", the person whose panel is tested
    manager -> atelier "اتلیه ستاره"   hall "تالار گل‌ها" (address, phone, map, logistics; another user manages it)
    event   = confirmed, in 7 days, 18:00
    mine    = staff's confirmed photographer afish on ``event`` (call time 16:30, the AGENTS.md pay terms)
    other   = a second, unrelated staff user (for isolation tests)
"""

from __future__ import annotations

import datetime as dt
import struct
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import Invoice, PayrollLine
from app.services import payroll
from app.services.constants import (
    AssignmentStatus,
    EventStatus,
    InvoiceStatus,
    PayoutMethod,
    Role,
    Skill,
)

TODAY = dt.date(2025, 7, 10)
BIG_TERMS = {
    "base": 1_000_000,
    "commission_per_unit": 9_000,
    "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
}
VECTOR_TOTAL = 1_870_000  # AGENTS.md: 1,000,000 + 80 x 9,000 + 150,000


def day(offset: int) -> dt.date:
    return TODAY + dt.timedelta(days=offset)


def body(response) -> str:
    return response.get_data(as_text=True)


def reload(obj):
    """Fresh copy of *obj* from the database (requests ran in their own session)."""
    db.session.expire_all()
    return db.session.get(type(obj), obj.id)


def png_bytes(width: int = 400, height: int = 400) -> bytes:
    """A header-valid PNG (enough for ``uploads.sniff_image``)."""
    ihdr = struct.pack(">I4sIIBBBBB", 13, b"IHDR", width, height, 8, 2, 0, 0, 0) + b"\x00\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\x00" * 16


def jpeg_bytes(width: int = 400, height: int = 400) -> bytes:
    frame = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + frame + b"\xff\xd9"


@pytest.fixture
def now(clock):
    """Freeze the app clock at 2025-07-10 12:00 Iran time (so ``security.today()`` is :data:`TODAY`)."""
    clock.set("2025-07-10 08:30:00")
    return clock


@pytest.fixture
def world(now, make_user, make_atelier, make_hall, make_event, make_assignment):
    manager = make_user(Role.ATELIER_MANAGER, full_name="مدیر اتلیه", mobile="09125550001")
    atelier = make_atelier(manager, name="اتلیه ستاره", city="شیراز", phone="07136000000")
    hall_manager = make_user(Role.HALL_MANAGER, full_name="مدیر تالار")
    hall = make_hall(
        hall_manager,
        name="تالار گل‌ها",
        city="شیراز",
        address="شیراز، بلوار چمران، تالار گل‌ها",
        phone="07112345678",
        map_url="https://maps.example/golha",
        logistics={"loading": "درب شرقی", "power": "پریز کنار سن"},
    )
    staff = make_user(Role.STAFF, full_name="علی عکاس", mobile="09121110001", city="شیراز")
    other = make_user(Role.STAFF, full_name="نیروی دیگر", mobile="09121110002")
    event = make_event(hall, atelier, date=day(7), title="عروسی نمونه", start_time=dt.time(18, 0))
    mine = make_assignment(event, Skill.PHOTOGRAPHER, user=staff, status=AssignmentStatus.CONFIRMED, call_time=dt.time(16, 30), notes="لباس مشکی")
    return SimpleNamespace(
        manager=manager,
        atelier=atelier,
        hall_manager=hall_manager,
        hall=hall,
        staff=staff,
        other=other,
        event=event,
        mine=mine,
    )


@pytest.fixture
def sc(world, client_for):
    """A test client signed in as the staff member of ``world``."""
    return client_for(world.staff)


@pytest.fixture
def make_line():
    """``make_line(assignment, units=80, paid=False, invoice_status=SUBMITTED, ...)`` -> its ``PayrollLine``.

    The numbers come from ``payroll.compute_pay`` (what the accounting module stores), the event is marked
    ``invoiced`` like after a real invoice.
    """

    def make(assignment, units=80, *, paid=False, invoice_status=InvoiceStatus.SUBMITTED, method=PayoutMethod.CARD_TO_CARD):
        event = assignment.event
        invoice = db.session.query(Invoice).filter_by(event_id=event.id).first()
        if invoice is None:
            invoice = Invoice(event=event, atelier=event.atelier, status=InvoiceStatus.coerce(invoice_status))
            db.session.add(invoice)
            db.session.flush()
        pay = payroll.compute_pay(assignment.pay_terms(), units)
        line = PayrollLine(
            invoice=invoice,
            assignment=assignment,
            role=assignment.role,
            person_name=assignment.person_name,
            units=pay.units,
            base=pay.base,
            commission=pay.commission,
            bonus=pay.bonus,
            total=pay.total,
            applied_tier=(
                {"threshold": pay.applied_tier.threshold, "amount": pay.applied_tier.amount} if pay.applied_tier else None
            ),
        )
        if paid:
            line.paid_at = dt.datetime(2025, 7, 1, 8, 30)
            line.paid_method = method
        db.session.add(line)
        db.session.commit()
        return line

    return make


@pytest.fixture
def past_job(world, make_event, make_assignment, make_line):
    """A finished, invoiced event 20 days ago with a completed afish of the staff member and its payroll line (80 photos)."""

    def make(offset=-20, units=80, **line_options):
        event = make_event(world.hall, world.atelier, date=day(offset), status=EventStatus.INVOICED, title="مراسم گذشته")
        assignment = make_assignment(event, Skill.PHOTOGRAPHER, user=world.staff, status=AssignmentStatus.COMPLETED)
        return assignment, make_line(assignment, units, **line_options)

    return make
