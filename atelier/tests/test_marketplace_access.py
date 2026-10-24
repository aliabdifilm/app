"""Access control of the marketplace: every GET answers without a 500, wrong roles get 403, anonymous
visitors are sent to the login page where a page is private, and POSTs are CSRF protected."""

from __future__ import annotations

import re

import pytest
from test_marketplace_support import body, mc, now, world  # noqa: F401

from app.extensions import db
from app.models import JobApplication
from app.services.constants import ApplicationStatus

ROLES = ("anonymous", "admin", "hall_manager", "atelier_manager", "staff")
# who may call each endpoint (None = everybody, including anonymous visitors)
OPEN_TO = {
    "jobs": None,
    "job_detail": None,
    "job_apply": {"staff"},
    "my_applications": {"staff"},
    "application_withdraw": {"staff"},
    "my_invitations": {"staff"},
    "invitation_accept": {"staff"},
    "invitation_decline": {"staff"},
    "manage_jobs": {"atelier_manager"},
    "inbox": {"atelier_manager"},
    "job_new": {"atelier_manager"},
    "job_edit": {"atelier_manager"},
    "job_close": {"atelier_manager"},
    "job_reopen": {"atelier_manager"},
    "job_applicants": {"atelier_manager"},
    "manage_application": {"atelier_manager"},
    "application_accept": {"atelier_manager"},
    "application_reject": {"atelier_manager"},
    "pay_preview": {"atelier_manager"},
    "talent_index": {"atelier_manager", "hall_manager", "admin"},
    "talent_profile": {"atelier_manager", "hall_manager", "admin"},
    "talent_invite": {"atelier_manager"},
    "reviews": {"atelier_manager", "staff"},
    "review_new": {"atelier_manager", "staff"},
}


def marketplace_rules(app, method):
    rules = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("marketplace.") and method in r.methods]
    return sorted(rules, key=lambda r: r.rule)


def fill(rule: str, ids: dict[str, int]) -> str:
    return re.sub(r"<(?:int:)?(\w+)>", lambda m: str(ids.get(m.group(1), 1)), rule)


@pytest.fixture
def full_world(world, make_user, make_assignment):
    """``world`` plus an application, an invitation and a completed assignment so every id in a URL exists."""
    application = JobApplication(post=world.post, user=world.staff, message="سلام", status=ApplicationStatus.PENDING)
    db.session.add(application)
    db.session.commit()
    invitation = make_assignment(world.event, "seller", user=world.staff, status="invited")
    done = make_assignment(world.event, "editor", user=world.other, status="completed")
    users = {
        "anonymous": None,
        "admin": world.admin,
        "hall_manager": world.hall_manager,
        "atelier_manager": world.manager,
        "staff": world.staff,
    }
    ids = {
        "post_id": world.post.id,
        "application_id": application.id,
        "assignment_id": invitation.id,
        "user_id": world.staff.id,
    }
    return world, users, ids, done


def clients_for(app, client_for, users):
    return {name: (client_for(user) if user is not None else app.test_client()) for name, user in users.items()}


def test_every_get_route_answers_without_a_500(app, full_world, client_for):
    world, users, ids, _done = full_world
    clients = clients_for(app, client_for, users)
    failures = []
    for rule in marketplace_rules(app, "GET"):
        url = fill(rule.rule, ids)
        for role, client in clients.items():
            response = client.get(url)
            if response.status_code >= 500:
                failures.append((rule.rule, role, response.status_code))
    assert failures == []


@pytest.mark.parametrize("endpoint", sorted(OPEN_TO))
def test_roles_are_enforced_on_every_endpoint(app, full_world, client_for, endpoint):
    world, users, ids, _done = full_world
    clients = clients_for(app, client_for, users)
    allowed = OPEN_TO[endpoint]
    rules = [r for r in app.url_map.iter_rules() if r.endpoint == f"marketplace.{endpoint}"]
    assert rules, endpoint
    for rule in rules:
        for method in sorted(rule.methods & {"GET", "POST"}):
            url = fill(rule.rule, ids)
            for role, client in clients.items():
                response = client.open(url, method=method)
                if allowed is None:
                    assert response.status_code < 500
                elif role in allowed:
                    assert response.status_code not in (401, 403) and response.status_code < 500, (url, method, role)
                elif role == "anonymous":
                    assert response.status_code == 302 and "/auth/login" in response.headers["Location"], (url, method)
                else:
                    assert response.status_code == 403, (url, method, role, response.status_code)


def test_private_pages_are_not_indexed(app, full_world, client_for):
    world, users, ids, _ = full_world
    manager = client_for(world.manager)
    for url in ("/jobs/manage", "/jobs/manage/inbox", "/talent", "/jobs/reviews"):
        assert 'content="noindex"' in body(manager.get(url)), url


def test_posts_need_a_csrf_token_when_protection_is_on(csrf_app, csrf_client, world, client_for, login_as):
    login_as(csrf_client, world.staff)
    response = csrf_client.post(f"/jobs/{world.post.id}/apply", data={"message": "سلام"})
    assert response.status_code == 400
    assert application_count() == 0


def application_count():
    import sqlalchemy as sa

    db.session.expire_all()
    return db.session.scalar(sa.select(sa.func.count(JobApplication.id)))


def test_every_post_route_needs_a_csrf_token(csrf_app, full_world, login_as):
    """Each state-changing route answers 400 without a token, for the role that may call it, and changes nothing."""
    world, users, ids, _done = full_world
    before = application_count()
    checked = 0
    for rule in marketplace_rules(csrf_app, "POST"):
        endpoint = rule.endpoint.split(".", 1)[1]
        allowed = OPEN_TO.get(endpoint) or {"staff"}  # job_report / review_report: any signed-in role will do
        role = sorted(allowed)[0]
        client = login_as(csrf_app.test_client(), users[role])
        response = client.post(fill(rule.rule, ids), data={"reason": "fake", "rating": "5"})
        assert response.status_code == 400, (rule.rule, role, response.status_code)
        checked += 1
    assert checked >= 15
    assert application_count() == before


def test_an_atelier_manager_whose_atelier_is_inactive_is_told_instead_of_getting_a_404(world, mc):
    world.atelier.is_active = False
    db.session.commit()
    response = mc.get("/jobs/new", follow_redirects=True)
    assert response.status_code == 200 and "اتلیهٔ شما غیرفعال است" in body(response)
