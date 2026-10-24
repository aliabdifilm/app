"""app.services.nav: registry, role filtering, active item, home and onboarding."""

from __future__ import annotations

import pytest

from app.services import nav
from app.services.constants import Role


@pytest.fixture
def navapp(app, empty_nav):
    for path, name in [("/x", "root"), ("/x/events", "events"), ("/x/events/<int:i>", "event"),
                       ("/x/staff", "staff"), ("/x/staff/new", "staff_new"), ("/y", "other")]:
        app.add_url_rule(path, name, lambda **kw: "ok")
    return app


class User:
    is_authenticated = True

    def __init__(self, role):
        self.role = Role.coerce(role)


def labels(items):
    return [item["label"] for item in items]


def test_items_are_filtered_by_role_and_ordered(navapp):
    nav.register("atelier_manager", "نیروها", "staff", order=30)
    nav.register(["atelier_manager", "hall_manager"], "مراسم‌ها", "events", order=20)
    nav.register(Role.STAFF, "کارهای من", "other", order=10)
    nav.register(None, "حساب", "root", order=99)
    assert [i.label for i in nav.items_for(User("atelier_manager"))] == ["مراسم‌ها", "نیروها", "حساب"]
    assert [i.label for i in nav.items_for(User("hall_manager"))] == ["مراسم‌ها", "حساب"]
    assert [i.label for i in nav.items_for(User("staff"))] == ["کارهای من", "حساب"]
    assert [i.label for i in nav.items_for(User("admin"))] == ["حساب"]


def test_anonymous_gets_nothing(navapp):
    nav.register(None, "حساب", "root")

    class Anon:
        is_authenticated = False
        role = None

    assert nav.items_for(Anon()) == []
    assert nav.items_for(None) == []


def test_registering_twice_replaces(navapp):
    nav.register("staff", "قدیمی", "other", order=5)
    nav.register("staff", "جدید", "other", order=6)
    assert [i.label for i in nav.items_for(User("staff"))] == ["جدید"]


def test_same_endpoint_for_different_roles_can_have_different_labels(navapp):
    nav.register("staff", "کارهای من", "other")
    nav.register("hall_manager", "تالار من", "other")
    with navapp.test_request_context("/"):
        assert labels(nav.menu_for(User("staff"))) == ["کارهای من"]
        assert labels(nav.menu_for(User("hall_manager"))) == ["تالار من"]


def test_unregister(navapp):
    nav.register("staff", "الف", "other")
    nav.register("hall_manager", "ب", "other")
    assert nav.unregister("other") == 2
    assert nav.items_for(User("staff")) == []


def test_bad_arguments():
    with nav.isolated():
        with pytest.raises(ValueError):
            nav.register("staff", "", "x")
        with pytest.raises(ValueError):
            nav.register([], "x", "y")
        with pytest.raises(ValueError):
            nav.register("wizard", "x", "y")


def test_menu_dicts_have_what_base_html_needs(navapp):
    nav.register("staff", "کارها", "other", icon="briefcase", order=1)
    with navapp.test_request_context("/y"):
        (item,) = nav.menu_for(User("staff"), endpoint="other", path="/y")
    assert item == {"label": "کارها", "url": "/y", "icon": "briefcase", "active": True, "badge": None, "endpoint": "other"}


def test_exactly_one_active_item_longest_prefix_wins(navapp):
    nav.register("staff", "خانه", "root", order=1)
    nav.register("staff", "مراسم‌ها", "events", order=2)
    nav.register("staff", "نیروها", "staff", order=3)
    with navapp.test_request_context("/x/events/5"):
        items = nav.menu_for(User("staff"), endpoint="event", path="/x/events/5")
    assert [(i["label"], i["active"]) for i in items] == [("خانه", False), ("مراسم‌ها", True), ("نیروها", False)]


def test_prefix_matching_does_not_confuse_similar_names(navapp):
    navapp.add_url_rule("/x/events-archive", "archive", lambda: "ok")
    nav.register("staff", "مراسم‌ها", "events")
    with navapp.test_request_context("/x/events-archive"):
        items = nav.menu_for(User("staff"), endpoint="archive", path="/x/events-archive")
    assert not any(i["active"] for i in items)


def test_explicit_active_endpoints_beat_url_prefixes(navapp):
    nav.register("staff", "خانه", "root", order=1, active=("staff_new",))
    nav.register("staff", "نیروها", "staff", order=2)
    with navapp.test_request_context("/x/staff/new"):
        items = nav.menu_for(User("staff"), endpoint="staff_new", path="/x/staff/new")
    assert [i["active"] for i in items] == [True, False]


def test_active_endpoint_patterns(navapp):
    nav.register("staff", "مراسم‌ها", "events", active=("event*",))
    with navapp.test_request_context("/whatever"):
        items = nav.menu_for(User("staff"), endpoint="event_detail", path="/zzz")
    assert items[0]["active"] is True


def test_no_item_active_when_nothing_matches(navapp):
    nav.register("staff", "نیروها", "staff")
    with navapp.test_request_context("/y"):
        items = nav.menu_for(User("staff"), endpoint="other", path="/y")
    assert items[0]["active"] is False


def test_root_item_is_not_active_everywhere(navapp):
    nav.register("staff", "خانه", "root")  # "/x"
    with navapp.test_request_context("/y"):
        assert nav.menu_for(User("staff"), endpoint="other", path="/y")[0]["active"] is False
    with navapp.test_request_context("/x"):
        assert nav.menu_for(User("staff"), endpoint="root", path="/x")[0]["active"] is True


def test_unknown_endpoints_are_skipped_not_fatal(navapp, caplog):
    nav.register("staff", "گم‌شده", "nope.nothing")
    nav.register("staff", "هست", "other")
    with navapp.test_request_context("/y"):
        assert labels(nav.menu_for(User("staff"), endpoint="other", path="/y")) == ["هست"]
    assert "unknown endpoint" in caplog.text


def test_url_kwargs(navapp):
    nav.register("staff", "مراسم ۷", "event", values={"i": 7})
    with navapp.test_request_context("/"):
        assert nav.menu_for(User("staff"))[0]["url"] == "/x/events/7"


@pytest.mark.parametrize(
    "badge,expected",
    [(lambda u: 3, "۳"), (lambda u: 250, "۹۹+"), (lambda u: 0, None), (lambda u: None, None), (lambda u: "", None),
     (lambda u: "جدید", "جدید"), (lambda u: 1 / 0, None)],
)
def test_badges(navapp, badge, expected):
    nav.register("staff", "کارها", "other", badge=badge)
    with navapp.test_request_context("/"):
        assert nav.menu_for(User("staff"))[0]["badge"] == expected


def test_badge_receives_the_user(navapp):
    seen = []
    nav.register("staff", "کارها", "other", badge=lambda user: seen.append(user) or 1)
    user = User("staff")
    with navapp.test_request_context("/"):
        nav.menu_for(user)
    assert seen == [user]


# --------------------------------------------------------------------------
# home and onboarding
# --------------------------------------------------------------------------


def test_home_url_per_role_first_registration_wins(navapp):
    nav.register_home("staff", "other")
    nav.register_home(["staff", "hall_manager"], "events")
    with navapp.test_request_context("/"):
        assert nav.home_url(User("staff")) == "/y"
        assert nav.home_url(User("hall_manager")) == "/x/events"
        assert nav.home_url(User("admin")) is None


def test_home_with_values_and_bad_endpoint(navapp, caplog):
    nav.register_home("staff", "event", i=3)
    nav.register_home("hall_manager", "nope.missing")
    with navapp.test_request_context("/"):
        assert nav.home_url(User("staff")) == "/x/events/3"
        assert nav.home_url(User("hall_manager")) is None


def test_onboarding_returns_first_unfinished_step(navapp):
    state = {"profile": False, "skills": False}
    nav.register_onboarding("staff", "other", lambda user: state["profile"])
    nav.register_onboarding("staff", "events", lambda user: state["skills"])
    with navapp.test_request_context("/"):
        assert nav.onboarding_url(User("staff")) == "/y"
        state["profile"] = True
        assert nav.onboarding_url(User("staff")) == "/x/events"
        state["skills"] = True
        assert nav.onboarding_url(User("staff")) is None
        assert nav.onboarding_url(User("hall_manager")) is None
        assert nav.has_onboarding(User("staff")) and not nav.has_onboarding(User("hall_manager"))


def test_onboarding_check_that_crashes_counts_as_done(navapp, caplog):
    nav.register_onboarding("staff", "other", lambda user: 1 / 0)
    with navapp.test_request_context("/"):
        assert nav.onboarding_url(User("staff")) is None


def test_isolated_restores_everything(navapp):
    nav.register("staff", "باقی", "other")
    nav.register_home("staff", "other")
    with nav.isolated():
        assert nav.items_for(User("staff")) == []
        nav.register("staff", "موقت", "root")
    assert [i.label for i in nav.items_for(User("staff"))] == ["باقی"]
