"""Access control of the hall panel: roles, tenant isolation (IDOR), CSRF, no 500 on any GET."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from test_halls_support import body, day, fill, hc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Event, EventOffer, HallAgreement, Review
from app.services.constants import EventStatus, Role


def hall_rules(app, method):
    rules = [rule for rule in app.url_map.iter_rules() if rule.endpoint.startswith("halls.") and method in rule.methods]
    return sorted(rules, key=lambda r: r.rule)


@pytest.fixture
def full_world(world, make_event, make_atelier):
    """``world`` plus an offer, an agreement, a review and a photo so that every page has something to show."""
    from app.models import HallPhoto
    from app.services import event_offers

    event_offers.offer_events([world.draft], [world.atelier, world.rival], world.manager, message="سلام")
    agreement = HallAgreement(hall=world.hall, atelier=world.rival, start_date=day(-3), end_date=day(100), is_exclusive=False, rent_per_event=3_000_000)
    db.session.add(agreement)
    db.session.add(HallPhoto(hall=world.hall, image_path="hall/aa/" + "a" * 32 + ".jpg"))
    review = Review(event_id=world.past.id, atelier_id=world.atelier.id, author_id=world.manager.id, subject_id=world.atelier_manager.id, rating=4, comment="خوب")
    db.session.add(review)
    db.session.commit()
    world.agreement = agreement
    world.offer = db.session.scalar(sa.select(EventOffer).where(EventOffer.event_id == world.draft.id, EventOffer.atelier_id == world.atelier.id))
    return world


# --------------------------------------------------------------------------
# anonymous, wrong role, pending
# --------------------------------------------------------------------------


def test_anonymous_is_sent_to_login(client, world, app):
    for rule in hall_rules(app, "GET"):
        url = fill(rule.rule, {"event_id": world.draft.id, "hall_id": world.hall.id})
        response = client.get(url)
        assert response.status_code == 302 and "/auth/login" in response.headers["Location"], url


def test_anonymous_json_endpoint_answers_401(client):
    response = client.post("/hall/api/dates/expand", json={})
    assert response.status_code == 401
    assert response.get_json()["code"] == "unauthorized"


@pytest.mark.parametrize("role", [Role.STAFF, Role.ATELIER_MANAGER, Role.ADMIN])
def test_other_roles_get_403_everywhere(role, world, app, make_user, client_for):
    client = client_for(make_user(role))
    for rule in hall_rules(app, "GET"):
        url = fill(rule.rule, {"event_id": world.draft.id, "hall_id": world.hall.id})
        assert client.get(url).status_code == 403, url
    for rule in hall_rules(app, "POST"):
        url = fill(rule.rule, {"event_id": world.draft.id, "hall_id": world.hall.id, "offer_id": 1})
        assert client.post(url, data={}).status_code == 403, url


def test_pending_hall_manager_is_sent_to_the_waiting_page_but_may_register_a_hall(make_user, client_for, make_hall):
    pending = make_user(Role.HALL_MANAGER, status="pending")
    client = client_for(pending)
    response = client.get("/hall/calendar")
    assert response.status_code == 302 and "/auth/pending" in response.headers["Location"]
    assert client.get("/hall/setup").status_code == 200  # onboarding stays reachable
    response = client.post("/hall/events/new", data={})
    assert response.status_code == 302 and "/auth/pending" in response.headers["Location"]


def test_manager_without_a_hall_is_sent_to_setup(make_user, client_for):
    client = client_for(make_user(Role.HALL_MANAGER))
    for url in ("/hall/", "/hall/calendar", "/hall/events", "/hall/offers", "/hall/reports", "/hall/ateliers"):
        response = client.get(url)
        assert response.status_code == 302 and response.headers["Location"].endswith("/hall/setup"), url


# --------------------------------------------------------------------------
# no 500 on any GET
# --------------------------------------------------------------------------


def test_every_get_route_renders_for_the_hall_manager(hc, full_world, app):
    w = full_world
    ids = {
        "event_id": w.confirmed.id,
        "hall_id": w.hall.id,
        "offer_id": w.offer.id,
        "atelier_id": w.atelier.id,
        "agreement_id": w.agreement.id,
    }
    seen = []
    for rule in hall_rules(app, "GET"):
        url = fill(rule.rule, ids)
        if rule.rule == "/hall/agreements/new":
            url += f"?atelier={w.atelier.id}"  # without an atelier it sends you to the directory
        response = hc.get(url)
        expected = 302 if rule.rule == "/hall/setup" else 200  # the first-hall page sends managers who have one on
        assert response.status_code == expected, f"{url} -> {response.status_code}"
        seen.append(rule.rule)
    assert "/hall/" in seen and "/hall/events/bulk" in seen and "/hall/reports" in seen


@pytest.mark.parametrize(
    "query",
    [
        "/hall/events?tab=free",
        "/hall/events?tab=past",
        "/hall/events?tab=cancelled",
        "/hall/events?tab=offered&q=%25_",
        "/hall/events?m=4&y=1404",
        "/hall/events?page=99",
        "/hall/calendar?y=1403&m=12",
        "/hall/calendar?y=9999&m=99",
        "/hall/offers?tab=all",
        "/hall/offers?tab=open",
        "/hall/offers?tab=accepted",
        "/hall/offers/new?event=1&event=2&event=999999",
        "/hall/offers/new?m=5",
        "/hall/ateliers?q=ستاره&sort=name",
        "/hall/ateliers?verified=1&city=شیراز",
        "/hall/reports?y=1404",
        "/hall/reports?y=abc",
        "/hall/agreements?tab=past",
    ],
)
def test_query_variants_never_crash(query, hc, full_world):
    assert hc.get(query).status_code in (200, 404)


# --------------------------------------------------------------------------
# tenant isolation: hall A cannot touch hall B
# --------------------------------------------------------------------------


@pytest.fixture
def intruder(world, client_for):
    """The other hall manager, signed in; ``world.draft`` etc. belong to somebody else."""
    return client_for(world.outsider)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/hall/events/{event}"),
        ("get", "/hall/events/{event}/edit"),
        ("post", "/hall/events/{event}/edit"),
        ("post", "/hall/events/{event}/cancel"),
        ("post", "/hall/events/{event}/reactivate"),
        ("post", "/hall/events/{event}/delete"),
        ("post", "/hall/events/{event}/open"),
        ("post", "/hall/events/{event}/close-open"),
        ("post", "/hall/events/{event}/review"),
        ("get", "/hall/halls/{hall}"),
        ("get", "/hall/halls/{hall}/edit"),
        ("post", "/hall/halls/{hall}/edit"),
        ("post", "/hall/halls/{hall}/delete"),
        ("post", "/hall/halls/{hall}/photos"),
        ("post", "/hall/halls/{hall}/photos/1/remove"),
        ("post", "/hall/offers/{offer}/withdraw"),
        ("post", "/hall/offers/{offer}/reoffer"),
        ("get", "/hall/agreements/{agreement}/edit"),
        ("post", "/hall/agreements/{agreement}/edit"),
        ("post", "/hall/agreements/{agreement}/end"),
    ],
)
def test_foreign_objects_answer_404(method, path, intruder, full_world):
    w = full_world
    url = path.format(event=w.draft.id, hall=w.hall.id, offer=w.offer.id, agreement=w.agreement.id)
    response = getattr(intruder, method)(url, **({"data": {}} if method == "post" else {}))
    assert response.status_code == 404, url


def test_foreign_ids_in_lists_and_actions_are_404_not_silently_ignored(intruder, full_world, hc):
    w = full_world
    # bulk action over one of my events and one of somebody else's
    response = intruder.post("/hall/events/action", data={"action": "cancel", "event_ids": [w.other_event.id, w.draft.id]})
    assert response.status_code == 404
    assert reload(w.other_event).status == EventStatus.DRAFT
    # the offer page with a foreign event
    assert intruder.get(f"/hall/offers/new?event={w.draft.id}").status_code == 404
    # filtering by someone else's hall
    assert intruder.get(f"/hall/calendar?hall={w.hall.id}").status_code == 404
    assert intruder.get(f"/hall/events?hall={w.hall.id}").status_code == 404
    assert intruder.get(f"/hall/reports?hall={w.hall.id}").status_code == 404


def test_lists_show_only_my_rows(intruder, full_world, hc):
    w = full_world
    html = body(intruder.get("/hall/events?tab=upcoming"))
    assert str(w.other_event.id) in html or "تالار دیگر" in html
    assert "تالار گل‌ها" not in html
    html = body(hc.get("/hall/events?tab=upcoming"))
    assert "تالار دیگر" not in html
    offers_html = body(intruder.get("/hall/offers?tab=all"))
    assert "اتلیه ستاره" not in offers_html and "اتلیه رقیب" not in offers_html


def test_creating_an_event_in_a_foreign_hall_is_refused(intruder, world):
    response = intruder.post(
        "/hall/events/new",
        data={"hall_id": world.hall.id, "date": day(30).isoformat(), "ceremony_type": "wedding"},
    )
    # the outsider has one hall, so the posted id is ignored and the event lands in *their* hall
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.scalar(sa.select(sa.func.count(Event.id)).where(Event.hall_id == world.hall.id, Event.date == day(30))) == 0


def test_creating_an_event_with_a_foreign_hall_id_among_several_halls_404(client_for, world, make_hall):
    make_hall(world.outsider, name="تالار دوم دیگر")
    client = client_for(world.outsider)
    response = client.post(
        "/hall/events/new",
        data={"hall_id": world.hall.id, "date": day(30).isoformat(), "ceremony_type": "wedding"},
    )
    assert response.status_code == 404


def test_offering_to_a_foreign_event_changes_nothing(intruder, full_world):
    w = full_world
    response = intruder.post("/hall/offers/new", data={"event_id": [w.later.id], "atelier_id": [w.atelier.id]})
    assert response.status_code == 404
    db.session.expire_all()
    assert db.session.scalar(sa.select(sa.func.count(EventOffer.id)).where(EventOffer.event_id == w.later.id)) == 0


def test_agreement_for_a_foreign_hall_is_refused(client_for, world, make_hall):
    make_hall(world.outsider, name="تالار دوم دیگر")
    client = client_for(world.outsider)
    response = client.post(
        "/hall/agreements/new",
        data={"atelier_id": world.atelier.id, "hall_id": world.hall.id, "start_date": day(1).isoformat()},
    )
    assert response.status_code == 404


def test_media_of_a_hall_photo_needs_a_signed_in_user(client, world):
    response = client.get("/media/hall/aa/" + "a" * 32 + ".jpg")
    assert response.status_code in (302, 404)


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


POST_URLS = [
    "/hall/events/{event}/cancel",
    "/hall/events/{event}/open",
    "/hall/events/{event}/delete",
    "/hall/events/new",
    "/hall/events/bulk",
    "/hall/events/action",
    "/hall/offers/new",
    "/hall/halls/{hall}/delete",
]


@pytest.mark.parametrize("path", POST_URLS)
def test_state_changing_posts_need_a_csrf_token(path, csrf_client, world, login_as):
    login_as(csrf_client, world.manager)
    url = path.format(event=world.draft.id, hall=world.hall2.id)
    response = csrf_client.post(url, data={})
    assert response.status_code == 400
    assert reload(world.draft).status == EventStatus.DRAFT
    assert reload(world.hall2) is not None


def test_a_post_with_a_valid_token_goes_through(csrf_client, world, login_as, get_csrf):
    login_as(csrf_client, world.manager)
    token = get_csrf(csrf_client, "/hall/events/new")
    response = csrf_client.post(
        "/hall/events/new",
        data={"csrf_token": token, "hall_id": world.hall.id, "date": day(40).isoformat(), "ceremony_type": "wedding"},
    )
    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.scalar(sa.select(sa.func.count(Event.id)).where(Event.date == day(40))) == 1


def test_state_changes_are_never_reachable_by_get(hc, full_world, app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("halls.") and "POST" in rule.methods and "GET" not in rule.methods:
            url = fill(rule.rule, {"event_id": full_world.draft.id, "hall_id": full_world.hall.id, "offer_id": 1, "agreement_id": 1, "photo_id": 1})
            assert hc.get(url).status_code == 405, url


def test_pages_ship_no_inline_script_style_or_external_assets(hc, full_world):
    import re

    for url in ("/hall/", "/hall/events/bulk", f"/hall/events/{full_world.draft.id}", "/hall/offers/new?event=%d" % full_world.draft.id, "/hall/halls/%d" % full_world.hall.id):
        html = body(hc.get(url))
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), url
        assert " style=" not in html, url
        assert not re.search(r"\son[a-z]+=\"", html), url
        assert not re.search(r'(?:src|href)="https?://', html), url

