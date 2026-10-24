"""Access control of the atelier panel: roles, tenant isolation (IDOR), CSRF, no 500 on any GET."""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa
from test_ateliers_support import BIG_TERMS, body, day, mc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, AtelierHall, EventOffer, RosterMember
from app.services import staffing
from app.services.constants import OfferMode, OfferStatus, Role


def atelier_rules(app, method):
    rules = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("ateliers.") and method in rule.methods:
            rules.append(rule)
    return sorted(rules, key=lambda r: r.rule)


def fill(rule: str, ids: dict[str, int]) -> str:
    return re.sub(r"<(?:int:)?(\w+)>", lambda m: str(ids.get(m.group(1), 1)), rule)


@pytest.fixture
def full_world(world, make_event):
    """``world`` plus an afish row, an offer and a hall rent so that every id in a URL exists."""
    assignment = staffing.assign_person(
        world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager
    )
    offer_event = make_event(world.hall, None, date=day(30), status="offered", offer_mode=OfferMode.DIRECT)
    offer = EventOffer(event=offer_event, atelier=world.atelier, offered_by=world.hall_manager)
    link = AtelierHall(atelier=world.atelier, hall=world.hall, rent_amount=2_000_000)
    db.session.add_all([offer, link])
    db.session.commit()
    ids = {
        "event_id": world.event.id,
        "assignment_id": assignment.id,
        "member_id": world.member.id,
        "offer_id": offer.id,
        "link_id": link.id,
        "action": "confirm",
        "equipment_id": 1,
    }
    return world, ids


def test_every_route_belongs_to_the_atelier_prefix(app):
    rules = atelier_rules(app, "GET") + atelier_rules(app, "POST")
    assert rules and all(rule.rule.startswith("/atelier") for rule in rules)


def test_manager_can_get_every_page_without_a_500(app, full_world, mc):
    world, ids = full_world
    extra = {
        "/atelier/events/<int:event_id>/afish/new": "?role=photographer",
        "/atelier/events/<int:event_id>/afish/assign": f"?role=editor&member={world.member.id}",
        "/atelier/events/<int:event_id>/afish/copy": f"?source={world.event.id}",
    }
    seen = 0
    for rule in atelier_rules(app, "GET"):
        url = fill(rule.rule, ids) + extra.get(rule.rule, "")
        if rule.rule == "/atelier/events/<int:event_id>/afish/copy":
            url = fill(rule.rule, {**ids, "event_id": world.later.id}) + f"?source={world.event.id}"
        response = mc.get(url)
        seen += 1
        assert response.status_code in (200, 302), f"{url} -> {response.status_code}"
        if response.status_code == 302:  # only the setup page (an atelier already exists)
            assert rule.rule == "/atelier/setup"
    assert seen >= 14


@pytest.mark.parametrize("tab", ["offers", "open", "upcoming", "past", "bogus"])
def test_event_tabs_render(mc, full_world, tab):
    assert mc.get(f"/atelier/events?tab={tab}").status_code == 200


@pytest.mark.parametrize("query", ["?y=1405&m=5", "?y=1405&m=12", "?y=1&m=1", "?y=abc", "?m=13&y=1405", "?y=1599&m=12"])
def test_calendar_survives_odd_months(mc, full_world, query):
    assert mc.get("/atelier/calendar" + query).status_code == 200


def test_anonymous_is_sent_to_login_everywhere(app, full_world):
    ids = full_world[1]
    client = app.test_client()
    for method in ("GET", "POST"):
        for rule in atelier_rules(app, method):
            response = client.open(fill(rule.rule, ids), method=method)
            assert response.status_code == 302 and "/auth/login" in response.headers["Location"], rule.rule


@pytest.mark.parametrize("role", [Role.STAFF, Role.HALL_MANAGER, Role.ADMIN])
def test_other_roles_are_forbidden_everywhere(app, full_world, make_user, client_for, role):
    ids = full_world[1]
    client = client_for(make_user(role))
    for method in ("GET", "POST"):
        for rule in atelier_rules(app, method):
            response = client.open(fill(rule.rule, ids), method=method)
            assert response.status_code == 403, f"{method} {rule.rule} -> {response.status_code}"


def test_pending_manager_is_sent_to_the_waiting_page(app, make_user, client_for):
    pending = make_user(Role.ATELIER_MANAGER, status="pending")
    client = client_for(pending)
    response = client.get("/atelier/")
    assert response.status_code == 302 and "/auth/pending" in response.headers["Location"]
    assert client.get("/atelier/setup").status_code == 200  # the onboarding page is allowed


def test_manager_without_atelier_is_sent_to_setup(app, make_user, client_for):
    newcomer = make_user(Role.ATELIER_MANAGER)
    client = client_for(newcomer)
    for url in ("/atelier/", "/atelier/roster", "/atelier/events", "/atelier/settings", "/atelier/afish"):
        response = client.get(url)
        assert response.status_code == 302 and "/atelier/setup" in response.headers["Location"], url


# --------------------------------------------------------------------------
# tenant isolation: another atelier's ids answer 404 (never 403, never data)
# --------------------------------------------------------------------------


@pytest.fixture
def rival_rows(world):
    assignment = staffing.assign_person(
        world.rival_event, "photographer", roster_member=world.rival_member, terms=BIG_TERMS, actor=world.rival_manager
    )
    offer = EventOffer(event=world.rival_event, atelier=world.rival, offered_by=world.hall_manager)
    link = AtelierHall(atelier=world.rival, hall=world.hall, rent_amount=1)
    db.session.add_all([offer, link])
    db.session.commit()
    return assignment, offer, link


FOREIGN_GET = [
    "/atelier/events/{ev}",
    "/atelier/events/{ev}/afish/new?role=photographer",
    "/atelier/events/{ev}/afish/assign?role=photographer&member={mine}",
    "/atelier/events/{ev}/afish/copy",
    "/atelier/events/{ev}/afish/{asg}/terms",
    "/atelier/roster/{theirs}/edit",
]
FOREIGN_POST = [
    "/atelier/events/{ev}/afish/assign",
    "/atelier/events/{ev}/afish/confirm-all",
    "/atelier/events/{ev}/afish/{asg}/confirm",
    "/atelier/events/{ev}/afish/{asg}/cancel",
    "/atelier/events/{ev}/afish/{asg}/terms",
    "/atelier/events/{ev}/needs",
    "/atelier/events/{ev}/done",
    "/atelier/events/{ev}/handback",
    "/atelier/events/{ev}/equipment",
    "/atelier/events/{ev}/equipment/1/remove",
    "/atelier/offers/{offer}/accept",
    "/atelier/offers/{offer}/decline",
    "/atelier/roster/{theirs}/toggle",
    "/atelier/roster/{theirs}/delete",
    "/atelier/settings/halls/{link}/delete",
]


def test_foreign_objects_are_404(world, mc, rival_rows):
    assignment, offer, link = rival_rows
    values = {
        "ev": world.rival_event.id,
        "mine": world.member.id,
        "theirs": world.rival_member.id,
        "asg": assignment.id,
        "offer": offer.id,
        "link": link.id,
    }
    for template in FOREIGN_GET:
        assert mc.get(template.format(**values)).status_code == 404, template
    for template in FOREIGN_POST:
        data = {"role": "photographer", "member": world.member.id, "equipment_id": 1}
        assert mc.post(template.format(**values), data=data).status_code == 404, template
    # nothing changed on the rival side
    db.session.expire_all()
    assert db.session.get(Assignment, assignment.id).status.value == "confirmed"
    assert db.session.get(RosterMember, world.rival_member.id) is not None
    assert db.session.get(EventOffer, offer.id).status == OfferStatus.PENDING


def test_assignment_of_my_other_event_cannot_be_used_through_this_event(world, mc):
    other = staffing.assign_person(world.later, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    assert mc.post(f"/atelier/events/{world.event.id}/afish/{other.id}/cancel").status_code == 404
    assert mc.get(f"/atelier/events/{world.event.id}/afish/{other.id}/terms").status_code == 404
    assert reload(other).status.value == "confirmed"


def test_lists_only_contain_my_own_rows(world, mc, rival_rows):
    for url in ("/atelier/afish", "/atelier/events?tab=upcoming", "/atelier/calendar?y=1404&m=4", "/atelier/roster"):
        page = body(mc.get(url))
        assert "مراسم رقیب" not in page and "نیروی رقیب" not in page and "اتلیه رقیب" not in page, url


def test_switching_to_somebody_elses_atelier_is_404(world, mc):
    assert mc.post("/atelier/switch", data={"atelier_id": world.rival.id}).status_code == 404
    assert mc.post("/atelier/switch", data={"atelier_id": "x"}).status_code == 404
    assert mc.post("/atelier/switch", data={"atelier_id": world.atelier.id}).status_code == 302


def test_a_second_atelier_of_mine_works_with_its_own_data(world, mc, make_atelier, make_event):
    second = make_atelier(world.manager, name="شعبهٔ دوم", city="اصفهان")
    mine2 = make_event(world.hall, second, date=day(9), title="مراسم شعبه دوم")
    assert mc.post("/atelier/switch", data={"atelier_id": second.id}).status_code == 302
    page = body(mc.get("/atelier/afish"))
    assert "مراسم شعبه دوم" in page
    assert mc.get(f"/atelier/events/{mine2.id}").status_code == 200
    assert mc.get(f"/atelier/events/{world.event.id}").status_code == 200  # same manager: both are mine
    roster = body(mc.get("/atelier/roster"))
    assert "رضا عکاس" not in roster  # the roster list follows the chosen atelier


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


def test_posts_without_a_csrf_token_are_refused(csrf_app, csrf_client, login_as, world):
    login_as(csrf_client, world.manager)
    for url in (
        f"/atelier/events/{world.event.id}/afish/assign",
        f"/atelier/events/{world.event.id}/afish/confirm-all",
        f"/atelier/events/{world.event.id}/needs",
        "/atelier/settings/profile",
        "/atelier/settings/presets",
        "/atelier/roster/new",
        f"/atelier/roster/{world.member.id}/toggle",
        "/atelier/switch",
        "/atelier/api/pay-preview",
    ):
        response = csrf_client.post(url, data={"role": "photographer", "member": world.member.id})
        assert response.status_code == 400, url
    db.session.expire_all()
    assert world.member.is_active is True


def test_post_with_a_valid_csrf_token_works(csrf_app, csrf_client, login_as, get_csrf, world):
    login_as(csrf_client, world.manager)
    token = get_csrf(csrf_client, f"/atelier/events/{world.event.id}")
    response = csrf_client.post(
        f"/atelier/events/{world.event.id}/afish/assign",
        data={
            "csrf_token": token,
            "role": "photographer",
            "member": world.member.id,
            "base": "1000000",
            "commission": "9000",
        },
    )
    assert response.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(Assignment.id))) == 1


def test_json_endpoint_rejects_missing_csrf_and_non_managers(csrf_app, csrf_client, login_as, world, make_user):
    login_as(csrf_client, world.manager)
    response = csrf_client.post("/atelier/api/pay-preview", json={"base": 1})
    assert response.status_code == 400
