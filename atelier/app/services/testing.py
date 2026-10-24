"""Helpers for tests and QA tooling (never used while serving real requests).

* :func:`login_client` signs a Flask test client in as a user *without* the OTP
  dance, producing exactly the session cookie a real login would, so Flask-Login's
  ``strong`` session protection stays enabled in tests.
* :class:`FrozenClock` is a controllable clock for ``get_state().clock``.

The pytest fixtures in ``tests/conftest.py`` and the ``flask routes-matrix``
command are thin wrappers around these.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from flask import has_app_context
from flask.testing import FlaskClient
from flask_login import login_user

from app.extensions import get_state
from app.models.base import to_naive_utc


class IsolatedClient(FlaskClient):
    """Flask test client whose requests behave like production requests.

    A plain ``FlaskClient`` reuses an app context that is already pushed (a test, a CLI
    command), which makes every request share ``flask.g`` and the SQLAlchemy session:
    Flask-Login's cached ``g._login_user`` then leaks from one client to the next, and a
    view that forgets ``db.session.commit()`` still "works".  This client gives each request
    its **own app context** (own ``g``, own session, uncommitted work rolled back at the end)
    and afterwards expires the caller's session so it re-reads what the request wrote.
    """

    def open(self, *args: Any, **kwargs: Any):
        with self.application.app_context():
            response = super().open(*args, **kwargs)
        if has_app_context():
            from app.extensions import db

            try:
                db.session.expire_all()
            except Exception:  # pragma: no cover - session already closed
                pass
        return response


def login_client(client: Any, user: Any, *, remember: bool = False) -> None:
    """Make *client* (a Flask test client) authenticated as *user*."""
    user_id = user if isinstance(user, int) else user.id
    from app.extensions import db
    from app.models import User

    account = db.session.get(User, user_id)
    if account is None:
        raise ValueError(f"no user with id {user_id}")
    # `login_user` writes the same keys a real sign-in does (_user_id, _fresh, _id ...).
    with client.application.test_request_context(
        environ_base={
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_USER_AGENT": client.environ_base.get("HTTP_USER_AGENT", ""),
        }
    ):
        from flask import session as request_session

        login_user(account, remember=remember, fresh=True)
        request_session.permanent = True
        data = dict(request_session)
    with client.session_transaction() as session:
        session.clear()
        session.update(data)


class FrozenClock:
    """A clock you advance by hand::

        clock = FrozenClock("2025-07-17 12:00:00")
        get_state(app).clock = clock
        clock.advance(seconds=121)      # the 2-minute OTP is now expired
    """

    def __init__(self, start: Any = None) -> None:
        if start is None:
            start = dt.datetime(2025, 7, 17, 12, 0, 0)
        elif isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        self._now = to_naive_utc(start)

    def __call__(self) -> dt.datetime:
        return self._now

    def now(self) -> dt.datetime:
        return self._now

    def today(self) -> dt.date:
        return self._now.date()

    def set(self, value: Any) -> None:
        self._now = to_naive_utc(dt.datetime.fromisoformat(value) if isinstance(value, str) else value)

    def advance(self, seconds: float = 0, *, minutes: float = 0, hours: float = 0, days: float = 0) -> dt.datetime:
        self._now = self._now + dt.timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self._now


def install_clock(app: Any, clock: Any) -> Any:
    """Use *clock* (callable returning a datetime) as the time source of *app*."""
    get_state(app).clock = clock
    return clock
