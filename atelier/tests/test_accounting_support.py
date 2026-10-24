"""Shared fixtures and helpers of the accounting tests (no tests in here).

``studio`` is one atelier with its manager signed in, a confirmed event in a hall and a crew of four
(presenter, photographer, seller, editor) on the AGENTS.md terms: base 1,000,000 + 9,000 per unit +
150,000 from 70 units.  ``form(...)`` builds the dict the invoice popup posts.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import Invoice, Notification, PayrollLine

CREW_ROLES = ("presenter", "photographer", "seller", "editor")
JALALI_VIEW = {"y": 1404, "m": 4}  # the Jalali month that contains the default test events (2025-07-17 ...)


def form(qty: dict | None = None, pay: dict | None = None, venue: Any = None, revision: int = 0, token: str | None = None, **extra: Any) -> dict:
    """The posted invoice form: ``qty={"print__16x21": 50}``, ``pay={"cash": 1_000_000}``."""
    data: dict[str, str] = {"base_revision": str(revision), "save_token": token or secrets.token_urlsafe(12)}
    for key, value in (qty or {}).items():
        data[f"qty__{key}"] = str(value)
    for method, value in (pay or {}).items():
        data[f"pay__{method}"] = str(value)
    if venue is not None:
        data["venue_rent"] = str(venue)
    data.update({k: str(v) for k, v in extra.items()})
    return data


def vector_form(**kw: Any) -> dict:
    """80 sold units (50 + 20 prints of two sizes + 10 raw files) and 20,000,000 in cash."""
    return form(
        qty={"print__16x21": 50, "print__20x30": 20, "raw_file": 10},
        pay={"cash": 20_000_000},
        **kw,
    )


@dataclass
class Studio:
    atelier: Any
    manager: Any
    hall: Any
    event: Any
    crew: list = field(default_factory=list)
    client: Any = None

    @property
    def url(self) -> str:
        return f"/accounting/events/{self.event.id}/invoice"

    def post(self, data: dict | None = None, **kw: Any):
        return self.client.post(self.url, data=data if data is not None else vector_form(), **kw)

    def invoice(self) -> Invoice | None:
        db.session.expire_all()
        return db.session.scalar(sa.select(Invoice).where(Invoice.event_id == self.event.id))

    def lines(self) -> list[PayrollLine]:
        db.session.expire_all()
        return list(db.session.scalars(sa.select(PayrollLine).order_by(PayrollLine.id)))


@pytest.fixture
def studio(client, make_atelier, make_event, make_assignment, login_as):
    atelier = make_atelier(default_venue_rent=3_000_000)
    event = make_event(atelier=atelier, status="confirmed")
    crew = [make_assignment(event, role) for role in CREW_ROLES]
    login_as(client, atelier.manager)
    return Studio(atelier, atelier.manager, event.hall, event, crew, client)


@pytest.fixture
def rival(make_atelier, make_event, make_assignment):
    """Another atelier with an invoiceable event of its own (tenant-isolation tests)."""
    atelier = make_atelier(default_venue_rent=1_000_000)
    event = make_event(atelier=atelier, status="confirmed")
    crew = [make_assignment(event, "photographer")]
    return Studio(atelier, atelier.manager, event.hall, event, crew)


def titles(user) -> list[str]:
    db.session.expire_all()
    return [n.title for n in db.session.scalars(sa.select(Notification).where(Notification.user_id == user.id))]


def flashes(html: str) -> list[str]:
    """Texts of the flash messages a page carries (``<li data-tone=..>``)."""
    return re.findall(r'<li data-tone="[a-z]+">(.*?)</li>', html, flags=re.S)


def csv_rows(response) -> list[list[str]]:
    import csv
    import io

    text = response.get_data(as_text=True)
    assert text.startswith("﻿"), "CSV must start with a UTF-8 BOM so Excel reads Persian"
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))
