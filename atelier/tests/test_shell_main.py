"""The ``main`` module: dashboard dispatch, notifications, shell context, test fixtures."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from app.extensions import db
from app.models import Notification
from app.services import nav, notifications


# --------------------------------------------------------------------------
# login_as / fixtures
# --------------------------------------------------------------------------


def test_login_as_signs_the_client_in_with_strong_session_protection(app, client, make_user, login_as):
    assert app.config["SESSION_PROTECTION"] == "strong"
    user = make_user("staff")
    assert client.get("/dashboard").status_code == 302  # anonymous: bounced to the login page
    login_as(client, user)
    response = client.get("/dashboard")
    assert response.status_code == 200  # fallback dashboard
    assert user.full_name in response.get_data(as_text=True)
    # a second request is still signed in
    assert client.get("/auth/account").status_code == 200


def test_clients_do_not_share_login_state(app, make_user, client_for):
    alice, bob = make_user("staff"), make_user("hall_manager")
    a, b = client_for(alice), client_for(bob)
    anonymous = app.test_client()
    # follow the redirect to the role's own home (modules register one, e.g. /hall/ for hall managers)
    assert alice.full_name in a.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert bob.full_name in b.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert anonymous.get("/dashboard").status_code == 302


def test_request_without_commit_does_not_persist(app, client, make_user, login_as):
    """The test client gives every request its own session: a view that forgets commit() fails here."""

    @app.post("/forgetful")
    def forgetful():
        from app.models import Setting

        Setting.set("forgotten", True)
        return "ok"

    response = client.post("/forgetful")
    assert response.status_code == 200
    from app.models import Setting

    assert db.session.execute(db.select(Setting).where(Setting.key == "forgotten")).first() is None


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


def test_dashboard_requires_login_and_remembers_where_you_were(client):
    response = client.get("/dashboard")
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert location.path == "/auth/login"
    assert parse_qs(location.query) == {"next": ["/dashboard"]}


def test_dashboard_lists_menu_entries_of_the_role(app, client, make_user, login_as, empty_nav):
    @app.get("/x/calendar")
    def calendar_page():
        return "calendar"

    nav.register("atelier_manager", "تقویم مراسم‌ها", "calendar_page", icon="calendar", order=10)
    nav.register("staff", "فقط نیرو", "calendar_page", icon="user", order=10)
    manager = make_user("atelier_manager")
    login_as(client, manager)
    html = client.get("/dashboard").get_data(as_text=True)
    assert "تقویم مراسم‌ها" in html
    assert "فقط نیرو" not in html


def test_dashboard_redirects_to_registered_home(app, client, make_user, login_as, empty_nav):
    @app.get("/x/home")
    def my_home():
        return "home"

    nav.register_home("staff", "my_home")
    login_as(client, make_user("staff"))
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/x/home")


def test_dashboard_home_pointing_at_itself_does_not_loop(app, client, make_user, login_as, empty_nav):
    nav.register_home("staff", "main.dashboard")
    login_as(client, make_user("staff"))
    assert client.get("/dashboard").status_code == 200


def test_dashboard_shows_onboarding_banner_until_complete(app, client, make_user, login_as, empty_nav):
    @app.get("/x/profile")
    def profile_setup():
        return "setup"

    done = {"value": False}
    nav.register_onboarding("staff", "profile_setup", lambda user: done["value"])
    login_as(client, make_user("staff"))
    assert "/x/profile" in client.get("/dashboard").get_data(as_text=True)
    done["value"] = True
    assert "/x/profile" not in client.get("/dashboard").get_data(as_text=True)


def test_dashboard_for_pending_user_goes_to_pending_page(client, make_user, login_as):
    login_as(client, make_user("atelier_manager", status="pending"))
    response = client.get("/dashboard")
    assert response.status_code == 302 and response.headers["Location"].endswith("/auth/pending")


# --------------------------------------------------------------------------
# shell context: menu + unread badge
# --------------------------------------------------------------------------


def test_shell_context_has_menu_with_exactly_one_active_item(app, client, make_user, login_as, empty_nav):
    @app.get("/x/events")
    def events_page():
        return render_page()

    @app.get("/x/events/<int:event_id>")
    def event_page(event_id):
        return render_page()

    @app.get("/x/staff")
    def staff_page():
        return render_page()

    def render_page():
        from flask import render_template_string

        return render_template_string("{% extends 'base.html' %}{% block content %}<p>x</p>{% endblock %}")

    nav.register("atelier_manager", "مراسم‌ها", "events_page", icon="calendar", order=10)
    nav.register("atelier_manager", "نیروها", "staff_page", icon="users", order=20)
    login_as(client, make_user("atelier_manager"))
    html = client.get("/x/events/5").get_data(as_text=True)  # a child page of "مراسم‌ها"
    active = re.findall(r'class="nav__link is-active"[^>]*>', html)
    assert len(active) == 1
    assert 'href="/x/events"' in html and 'href="/x/staff"' in html
    assert re.search(r'href="/x/events" title="مراسم‌ها" aria-current="page"', html)


def test_unread_badge_in_the_top_bar(client, make_user, login_as, make_notification):
    user = make_user("staff")
    make_notification(user, "یک")
    make_notification(user, "دو")
    make_notification(user, "خوانده", read=True)
    login_as(client, user)
    html = client.get("/dashboard").get_data(as_text=True)
    assert "۲ خوانده‌نشده" in html


def test_shell_survives_a_broken_menu_entry(app, client, make_user, login_as, empty_nav, caplog):
    nav.register("staff", "خراب", "does.not.exist", order=1)
    nav.register("staff", "خراب‌تر", "main.dashboard", order=2, badge=lambda user: 1 / 0)
    login_as(client, make_user("staff"))
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "خراب‌تر" in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


def test_notifications_page_lists_only_my_notifications(client, make_user, login_as, make_notification):
    me, other = make_user("staff"), make_user("staff")
    make_notification(me, "اعلان من")
    make_notification(other, "اعلان دیگران")
    login_as(client, me)
    html = client.get("/notifications").get_data(as_text=True)
    assert "اعلان من" in html and "اعلان دیگران" not in html


def test_notifications_empty_state(client, make_user, login_as):
    login_as(client, make_user("staff"))
    assert "اعلانی ندارید" in client.get("/notifications").get_data(as_text=True)


def test_notifications_requires_login(client):
    assert client.get("/notifications").status_code == 302
    assert client.post("/notifications/read").status_code == 302


def test_mark_all_read_and_single_read(client, make_user, login_as, make_notification):
    me = make_user("staff")
    first = make_notification(me, "الف")
    make_notification(me, "ب")
    login_as(client, me)
    response = client.post("/notifications/read", data={"ids": str(first.id)})
    assert response.status_code == 302
    assert notifications.unread_count(me) == 1
    client.post("/notifications/read")
    assert notifications.unread_count(me) == 0


def test_cannot_mark_somebody_elses_notification(client, make_user, login_as, make_notification):
    me, other = make_user("staff"), make_user("staff")
    theirs = make_notification(other, "خصوصی")
    login_as(client, me)
    client.post("/notifications/read", data={"ids": str(theirs.id)})
    assert notifications.unread_count(other) == 1


def test_notifications_json(client, make_user, login_as, make_notification):
    me = make_user("staff")
    make_notification(me, "الف", url="/x/y")
    make_notification(me, "ب", url="https://evil.example/phish")
    login_as(client, me)
    body = client.get("/notifications", headers={"Accept": "application/json"}).get_json()
    assert body["ok"] is True and body["data"]["unread"] == 2
    urls = {item["title"]: item["url"] for item in body["data"]["items"]}
    assert urls == {"الف": "/x/y", "ب": None}  # off-site links are never handed out


def test_notification_external_url_is_not_rendered_as_link(client, make_user, login_as, make_notification):
    me = make_user("staff")
    make_notification(me, "فیشینگ", url="https://evil.example/phish")
    login_as(client, me)
    html = client.get("/notifications").get_data(as_text=True)
    assert "evil.example" not in html


def test_notifications_pagination(client, make_user, login_as):
    me = make_user("staff")
    for index in range(45):
        db.session.add(Notification(user_id=me.id, kind="system", title=f"اعلان {index}"))
    db.session.commit()
    login_as(client, me)
    page1 = client.get("/notifications").get_data(as_text=True)
    page3 = client.get("/notifications?page=3").get_data(as_text=True)
    card = re.compile(r'class="card[^"]* notification"')
    assert len(card.findall(page1)) == 20
    assert len(card.findall(page3)) == 5
