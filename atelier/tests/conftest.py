"""Shared pytest fixtures for the whole suite (documented in docs/ARCHITECTURE.md, section "Tests").

Quick reference
---------------
``app``            a fresh application (in-memory SQLite, CSRF off, console SMS, FrozenClock-ready)
                   with an **app context pushed for the whole test**, so ``db.session`` just works
``app_config``     dict of config overrides for ``app``; override the fixture in your module
``client``         a test client; every request runs in its own app context / database session
                   (like production) and the test's session is expired afterwards
``login_as``       ``login_as(client, user)`` signs the client in without the OTP dance
``client_for``     ``client_for(user)`` -> a new client already signed in as *user*
``csrf_app``       like ``app`` but with CSRF protection ON; ``csrf_client``, ``get_csrf(client, url)``
``clock``          a :class:`FrozenClock` installed as the app's time source (``clock.advance(minutes=3)``)
``outbox``         the console SMS provider: ``outbox.last_code(mobile)``, ``outbox.outbox``
``bus``            isolated event bus recording ``(event_name, payload)`` tuples
``empty_nav``      an empty navigation registry for the duration of the test
factories          ``make_user, make_hall, make_atelier, make_event, make_roster_member,
                   make_assignment, make_staff_profile, make_job_post, make_notification``
                   - each creates the parents it needs, commits, and returns the object

Run the suite on PostgreSQL with ``ATELIER_TEST_DB_URL=postgresql+psycopg2://...``.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import re

import pytest

from app import create_app
from app.extensions import db, get_state
from app.models import (
    Assignment,
    Atelier,
    Event,
    Hall,
    JobPost,
    Notification,
    RosterMember,
    StaffProfile,
    User,
)
from app.services import eventbus, nav
from app.services.constants import (
    AssignmentSource,
    AssignmentStatus,
    EventStatus,
    Role,
    Skill,
    UserStatus,
)
from app.services.testing import FrozenClock, IsolatedClient, install_clock, login_client

TEST_DB_URL = os.environ.get("ATELIER_TEST_DB_URL", "sqlite://")
_IS_SQLITE = TEST_DB_URL.startswith("sqlite")


# --------------------------------------------------------------------------
# application and clients
# --------------------------------------------------------------------------


AtelierTestClient = IsolatedClient  # every request in its own app context + session (see its docstring)


def _build_app(tmp_path, **overrides):
    config = {"SQLALCHEMY_DATABASE_URI": TEST_DB_URL, "UPLOAD_DIR": str(tmp_path / "uploads")}
    config.update(overrides)
    flask_app = create_app("testing", **config)
    flask_app.test_client_class = AtelierTestClient
    return flask_app


@pytest.fixture
def app_config():
    """Overrides for the ``app`` fixture.  Redefine in a test module to change the config."""
    return {}


@pytest.fixture
def app(tmp_path, app_config):
    flask_app = _build_app(tmp_path, **app_config)
    context = flask_app.app_context()
    context.push()
    if not _IS_SQLITE:  # shared database: start from an empty schema
        db.drop_all()
        db.create_all()
    try:
        yield flask_app
    finally:
        db.session.remove()
        if not _IS_SQLITE:
            db.drop_all()
        db.engine.dispose()
        context.pop()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf_app(app):
    """The same application with CSRF protection switched ON (for enforcement tests).

    It is the ``app`` fixture itself (so the factories write to the same database); Flask-WTF
    reads ``WTF_CSRF_ENABLED`` on every request, so flipping it per test is enough.
    """
    app.config["WTF_CSRF_ENABLED"] = True
    return app


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


_CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


@pytest.fixture
def get_csrf():
    """``get_csrf(client, url="/auth/login")`` -> the CSRF token the page embeds for that client's session."""

    def fetch(client, url: str = "/auth/login") -> str:
        response = client.get(url)
        match = _CSRF_RE.search(response.get_data(as_text=True))
        assert match, f"no csrf-token meta tag on {url} (status {response.status_code})"
        return match.group(1)

    return fetch


@pytest.fixture
def login_as():
    """``login_as(client, user)`` - sign a test client in as *user*, bypassing the OTP step."""

    def sign_in(client, user, *, remember: bool = False):
        login_client(client, user, remember=remember)
        return client

    return sign_in


@pytest.fixture
def client_for(app, login_as):
    """``client_for(user)`` -> a new test client that is signed in as *user*."""

    def make(user):
        return login_as(app.test_client(), user)

    return make


@pytest.fixture
def clock(app):
    """A controllable clock used by ``security.now()`` / ``security.today()`` (and so by OTP expiry)."""
    frozen = FrozenClock("2025-07-17 12:00:00")
    install_clock(app, frozen)
    return frozen


@pytest.fixture
def outbox(app):
    """The console SMS provider of ``app``; ``outbox.last_code("0912...")`` returns the last OTP sent."""
    return get_state(app).sms


@pytest.fixture
def bus():
    """Isolated event bus that records every published event as ``(name, payload)``."""
    with eventbus.isolated():
        records: list[tuple[str, dict]] = []
        for name in eventbus.KNOWN_EVENTS:
            eventbus.subscribe(name, lambda _n=name, **payload: records.append((_n, payload)))
        yield records


@pytest.fixture
def empty_nav():
    """An empty navigation registry (modules register theirs at import time)."""
    with nav.isolated():
        yield nav


# --------------------------------------------------------------------------
# factories
# --------------------------------------------------------------------------


def _commit(*objects):
    db.session.add_all(objects)
    db.session.commit()
    return objects[0] if len(objects) == 1 else objects


@pytest.fixture
def make_user(app):
    """``make_user(role="staff", *, mobile=None, full_name=None, status="active", password=None, **fields)``."""
    counter = itertools.count(1)

    def make(role=Role.STAFF, *, mobile=None, full_name=None, status=UserStatus.ACTIVE, password=None, **fields):
        number = next(counter)
        role = Role.coerce(role)
        user = User(
            mobile=mobile or f"0912{number:07d}",
            full_name=full_name if full_name is not None else f"{role.label} {number}",
            role=role,
            status=UserStatus.coerce(status),
            **fields,
        )
        if password:
            user.set_password(password)
        return _commit(user)

    return make


@pytest.fixture
def make_hall(app, make_user):
    """``make_hall(manager=None, **fields)`` - creates a hall manager when none is given."""
    counter = itertools.count(1)

    def make(manager=None, **fields):
        manager = manager or make_user(Role.HALL_MANAGER)
        fields.setdefault("name", f"تالار نمونه {next(counter)}")
        fields.setdefault("city", "شیراز")
        return _commit(Hall(manager=manager, **fields))

    return make


@pytest.fixture
def make_atelier(app, make_user):
    """``make_atelier(manager=None, **fields)`` - creates an atelier manager when none is given (``atelier.manager``)."""
    counter = itertools.count(1)

    def make(manager=None, **fields):
        manager = manager or make_user(Role.ATELIER_MANAGER)
        fields.setdefault("name", f"اتلیه نمونه {next(counter)}")
        fields.setdefault("city", "شیراز")
        return _commit(Atelier(manager=manager, **fields))

    return make


@pytest.fixture
def make_event(app, make_hall):
    """``make_event(hall=None, atelier=None, *, date=None, status=None, **fields)``.

    Creates the hall when omitted.  ``status`` defaults to ``confirmed`` when an atelier is
    given (it is "their" event) and ``draft`` otherwise.  Dates default to consecutive days
    from 2025-07-17 so two events never clash by accident.
    """
    counter = itertools.count(0)

    def make(hall=None, atelier=None, *, date=None, status=None, **fields):
        hall = hall or make_hall()
        offset = next(counter)
        if status is None:
            status = EventStatus.CONFIRMED if atelier is not None else EventStatus.DRAFT
        event = Event(
            hall=hall,
            atelier=atelier,
            date=date or dt.date(2025, 7, 17) + dt.timedelta(days=offset),
            status=EventStatus.coerce(status),
            title=fields.pop("title", f"مراسم نمونه {offset + 1}"),
            **fields,
        )
        return _commit(event)

    return make


@pytest.fixture
def make_roster_member(app, make_atelier):
    """``make_roster_member(atelier=None, *, user=None, display_name=None, skills=("photographer",), **fields)``."""
    counter = itertools.count(1)

    def make(atelier=None, *, user=None, display_name=None, skills=(Skill.PHOTOGRAPHER,), **fields):
        atelier = atelier or make_atelier()
        number = next(counter)
        member = RosterMember(
            atelier=atelier,
            user=user,
            display_name=display_name or (user.full_name if user else f"نیروی ثابت {number}"),
            skills=[Skill.coerce(s).value for s in skills],
            **fields,
        )
        return _commit(member)

    return make


@pytest.fixture
def make_assignment(app, make_event, make_atelier, make_user):
    """``make_assignment(event=None, role="photographer", *, user=None, roster_member=None, status="confirmed", **terms)``.

    Without a person a staff user is created.  Pay terms default to the AGENTS.md vector
    (base 1,000,000 + 9,000 per photo + 150,000 bonus from 70 photos).  Built directly
    (no notifications / audit); use ``app.services.staffing.assign_person`` to test the service.
    """

    def make(
        event=None,
        role=Skill.PHOTOGRAPHER,
        *,
        user=None,
        roster_member=None,
        status=AssignmentStatus.CONFIRMED,
        base_pay=1_000_000,
        commission_per_unit=9_000,
        bonus_tiers=None,
        **fields,
    ):
        if event is None:
            event = make_event(atelier=make_atelier())
        if user is None and roster_member is None:
            user = make_user(Role.STAFF)
        assignment = Assignment(
            event=event,
            role=Skill.coerce(role),
            user=user,
            roster_member=roster_member,
            status=AssignmentStatus.coerce(status),
            source=AssignmentSource.ROSTER if roster_member is not None else AssignmentSource.DIRECT,
            base_pay=base_pay,
            commission_per_unit=commission_per_unit,
            bonus_tiers=[{"threshold": 70, "amount": 150_000}] if bonus_tiers is None else bonus_tiers,
            **fields,
        )
        return _commit(assignment)

    return make


@pytest.fixture
def make_staff_profile(app, make_user):
    """``make_staff_profile(user=None, *, skills=("photographer",), **fields)`` - a marketplace profile."""

    def make(user=None, *, skills=(Skill.PHOTOGRAPHER,), **fields):
        user = user or make_user(Role.STAFF)
        profile = StaffProfile(user=user, **fields)
        profile.skills = [Skill.coerce(s).value for s in skills]
        return _commit(profile)

    return make


@pytest.fixture
def make_job_post(app, make_atelier):
    """``make_job_post(atelier=None, role="photographer", *, date=None, **fields)``."""
    counter = itertools.count(1)

    def make(atelier=None, role=Skill.PHOTOGRAPHER, *, date=None, **fields):
        atelier = atelier or make_atelier()
        number = next(counter)
        fields.setdefault("title", f"آگهی همکاری {number}")
        fields.setdefault("city", atelier.city or "شیراز")
        post = JobPost(
            atelier=atelier,
            role=Skill.coerce(role),
            date=date or dt.date(2025, 8, 1) + dt.timedelta(days=number),
            base_pay=fields.pop("base_pay", 1_000_000),
            commission_per_unit=fields.pop("commission_per_unit", 9_000),
            **fields,
        )
        return _commit(post)

    return make


@pytest.fixture
def make_notification(app, make_user):
    """``make_notification(user=None, title="...", *, read=False, **fields)``."""

    def make(user=None, title="اعلان آزمایشی", *, read=False, **fields):
        user = user or make_user()
        note = Notification(user_id=user.id, kind=fields.pop("kind", "system"), title=title, **fields)
        if read:
            from app.models import utcnow

            note.read_at = utcnow()
        return _commit(note)

    return make
