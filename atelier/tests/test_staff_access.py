"""Access control of the staff panel: roles, own-data isolation (IDOR), CSRF, no 500 on any GET."""

from __future__ import annotations

import re

import pytest
from test_staff_support import body, day, make_line, now, past_job, png_bytes, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import PortfolioItem
from app.services import nav
from app.services.constants import AssignmentStatus, Role, Skill


def staff_rules(app, method):
    rules = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("staff.") and method in r.methods]
    return sorted(rules, key=lambda r: r.rule)


def fill(rule: str, ids: dict[str, int]) -> str:
    return re.sub(r"<(?:int:)?(\w+)>", lambda m: str(ids.get(m.group(1), 1)), rule)


def test_blueprint_is_mounted_under_me(app):
    assert staff_rules(app, "GET"), "the staff module registered no routes"
    for rule in staff_rules(app, "GET") + staff_rules(app, "POST"):
        assert rule.rule == "/me" or rule.rule.startswith("/me/"), rule.rule


@pytest.fixture
def ids(world, past_job):
    assignment, line = past_job()
    item = PortfolioItem(user_id=world.staff.id, title="x", image_path="portfolio/aa/" + "a" * 32 + ".png")
    db.session.add(item)
    db.session.commit()
    return {"assignment_id": world.mine.id, "line_id": line.id, "item_id": item.id}


def test_every_get_route_answers_for_the_staff_member(app, sc, ids):
    """GET every route with the right role and real ids: nothing may answer 500."""
    seen = 0
    for rule in staff_rules(app, "GET"):
        url = fill(rule.rule, ids)
        response = sc.get(url)
        assert response.status_code in (200, 302), (url, response.status_code)
        seen += 1
    assert seen >= 15


def test_anonymous_is_sent_to_login(app, client, ids):
    for rule in staff_rules(app, "GET"):
        response = client.get(fill(rule.rule, ids))
        assert response.status_code == 302, rule.rule
        assert "/auth/login" in response.headers["Location"], rule.rule


@pytest.mark.parametrize("role", [Role.HALL_MANAGER, Role.ATELIER_MANAGER, Role.ADMIN])
def test_other_roles_get_403_everywhere(app, client_for, make_user, ids, role):
    client = client_for(make_user(role))
    for method in ("GET", "POST"):
        for rule in staff_rules(app, method):
            response = client.open(fill(rule.rule, ids), method=method)
            assert response.status_code == 403, (method, rule.rule, response.status_code)


def test_anonymous_post_is_sent_to_login(app, client, ids):
    for rule in staff_rules(app, "POST"):
        response = client.post(fill(rule.rule, ids))
        assert response.status_code == 302 and "/auth/login" in response.headers["Location"], rule.rule


def test_post_without_csrf_token_is_rejected(app, csrf_app, client_for, world, ids):
    client = client_for(world.staff)
    for rule in staff_rules(csrf_app, "POST"):
        response = client.post(fill(rule.rule, ids))
        assert response.status_code == 400, (rule.rule, response.status_code)


def test_post_with_csrf_token_goes_through(csrf_app, client_for, get_csrf, world):
    client = client_for(world.staff)
    token = get_csrf(client, "/me/settings")
    response = client.post("/me/settings/notifications", data={"csrf_token": token, "groups": ["reminders"]})
    assert response.status_code == 302


def test_every_form_of_every_page_carries_a_csrf_field(sc, ids):
    for url in ("/me/invitations", "/me/assignments/%d" % ids["assignment_id"], "/me/availability", "/me/profile",
                "/me/portfolio", "/me/settings", "/me/settings/mobile", "/me/earnings/%d" % ids["line_id"]):
        html = body(sc.get(url))
        for form in re.findall(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", html, flags=re.S):
            assert 'name="csrf_token"' in form, (url, form[:120])


# -- somebody else's objects are 404, never 403 and never data --------------------------------------


@pytest.fixture
def intruder(world, client_for):
    return client_for(world.other)


@pytest.mark.parametrize(
    "method,pattern",
    [
        ("GET", "/me/assignments/{assignment_id}"),
        ("GET", "/me/assignments/{assignment_id}/event.ics"),
        ("POST", "/me/assignments/{assignment_id}/accept"),
        ("POST", "/me/assignments/{assignment_id}/decline"),
        ("POST", "/me/assignments/{assignment_id}/withdraw"),
        ("GET", "/me/earnings/{line_id}"),
        ("POST", "/me/earnings/{line_id}/flag"),
        ("POST", "/me/portfolio/{item_id}/update"),
        ("POST", "/me/portfolio/{item_id}/move"),
        ("POST", "/me/portfolio/{item_id}/delete"),
    ],
)
def test_foreign_objects_are_404(intruder, ids, method, pattern):
    response = intruder.open(pattern.format(**ids), method=method, data={"message": "hello there", "direction": "up"})
    assert response.status_code == 404


def test_foreign_ids_that_do_not_exist_are_404(sc):
    for url in ("/me/assignments/99999", "/me/earnings/99999", "/me/assignments/99999/event.ics"):
        assert sc.get(url).status_code == 404


def test_foreign_post_changes_nothing(intruder, world, ids):
    intruder.post(f"/me/assignments/{ids['assignment_id']}/withdraw")
    intruder.post(f"/me/portfolio/{ids['item_id']}/delete")
    assert reload(world.mine).status == AssignmentStatus.CONFIRMED
    assert db.session.get(PortfolioItem, ids["item_id"]) is not None


def test_pages_never_leak_other_peoples_assignments_or_pay(world, intruder, past_job, make_assignment, make_event):
    """The other person's own pages show only their own data (nothing of the first staff member)."""
    event = make_event(world.hall, world.atelier, date=day(30), title="مراسم دور")
    make_assignment(event, Skill.SELLER, user=world.other, status=AssignmentStatus.INVITED, base_pay=2_000_000)
    assignment, line = past_job(units=55)  # total 1,495,000 - a number that appears nowhere in the other's terms
    assert line.total == 1_495_000
    for url in ("/me", "/me/invitations", "/me/schedule", "/me/schedule?view=list", "/me/earnings", "/me/earnings.csv",
                "/me/schedule?view=list&when=past"):
        html = body(intruder.get(url))
        assert "علی عکاس" not in html, url
        assert "۱٬۴۹۵٬۰۰۰" not in html and "1495000" not in html, url
    assert "فروشنده" in body(intruder.get("/me/invitations"))
    assert "عکاس" not in body(intruder.get("/me/schedule?view=list"))
    assert "مراسم گذشته‌ای ثبت نشده" in body(intruder.get("/me/schedule?view=list&when=past"))  # empty state


def test_roster_linked_assignment_is_mine_but_not_the_namesakes(world, make_roster_member, make_assignment, make_event, client_for, make_user):
    """An afish row on a roster entry linked to my account is mine; one on an unlinked entry with my name is not."""
    linked = make_roster_member(world.atelier, user=world.staff, display_name="علی (ثابت)")
    namesake = make_roster_member(world.atelier, display_name="علی عکاس")
    event = make_event(world.hall, world.atelier, date=day(15))
    via_roster = make_assignment(event, Skill.EDITOR, roster_member=linked)
    stranger = make_assignment(event, Skill.SELLER, roster_member=namesake)
    client = client_for(world.staff)
    assert client.get(f"/me/assignments/{via_roster.id}").status_code == 200
    assert client.get(f"/me/assignments/{stranger.id}").status_code == 404


# -- pending accounts and navigation ------------------------------------------------------------------


def test_pending_staff_reaches_profile_pages_but_not_the_dashboard(make_user, client_for):
    pending = make_user(Role.STAFF, status="pending")
    client = client_for(pending)
    assert client.get("/me/profile").status_code == 200
    assert client.get("/me/portfolio").status_code == 200
    assert client.get("/me/settings").status_code == 200
    for url in ("/me", "/me/invitations", "/me/schedule", "/me/availability", "/me/earnings"):
        response = client.get(url)
        assert response.status_code == 302 and "/auth/pending" in response.headers["Location"], url


def test_navigation_lists_the_staff_pages(app, world):
    with app.test_request_context("/me"):
        items = nav.items_for(world.staff)
    endpoints = {item.endpoint for item in items}
    assert {"staff.dashboard", "staff.invitations", "staff.schedule", "staff.earnings_page", "staff.profile"} <= endpoints
    with app.test_request_context("/me"):
        assert nav.home_url(world.staff) == "/me/"


def test_onboarding_points_to_the_profile_until_a_skill_is_chosen(app, world, make_user):
    newcomer = make_user(Role.STAFF)
    with app.test_request_context("/me"):
        assert nav.onboarding_url(newcomer).endswith("/me/profile")
    from app.models import StaffProfile

    profile = StaffProfile(user=newcomer)
    profile.skills = ["editor"]
    db.session.add(profile)
    db.session.commit()
    with app.test_request_context("/me"):
        assert nav.onboarding_url(newcomer) is None


def test_invitation_badge_counts_open_invitations(app, world, make_assignment, make_event):
    event = make_event(world.hall, world.atelier, date=day(12))
    make_assignment(event, Skill.EDITOR, user=world.staff, status=AssignmentStatus.INVITED)
    with app.test_request_context("/me"):
        menu = nav.menu_for(world.staff, endpoint="staff.dashboard", path="/me")
    badge = next(item["badge"] for item in menu if item["endpoint"] == "staff.invitations")
    assert badge == "۱"


def test_pages_ship_no_inline_script_or_style_and_no_external_assets(sc, ids):
    for url in ("/me", "/me/invitations", "/me/assignments/%d" % ids["assignment_id"], "/me/schedule", "/me/availability",
                "/me/earnings", "/me/profile", "/me/portfolio", "/me/settings"):
        html = body(sc.get(url))
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), url
        assert " style=" not in html, url
        assert " onclick=" not in html, url
        assert not re.search(r"(?:src|href)=\"https?://(?!maps\.example)", html), url
