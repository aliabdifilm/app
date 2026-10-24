"""app.services.permissions: role decorators and ownership helpers (404, never a leak)."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa
from flask import jsonify, request

from app.extensions import db
from app.models import Invoice, EventOffer, JobApplication, StockItem, Equipment
from app.services import nav, permissions as perm
from app.services.constants import (
    AssignmentStatus,
    EventStatus,
    OfferMode,
    OfferStatus,
    Role,
)
from app.services.permissions import (
    OnboardingRequired,
    admin_required,
    login_required,
    role_required,
)


@pytest.fixture
def routes(app):
    """A handful of protected test routes on the app."""

    @app.get("/t/open")
    def t_open():
        return "open"

    @app.get("/t/any")
    @login_required
    def t_any():
        return "any"

    @app.get("/t/any-pending-ok")
    @login_required(allow_pending=True)
    def t_any_pending():
        return "pending-ok"

    @app.get("/t/staff")
    @role_required(Role.STAFF)
    def t_staff():
        return "staff"

    @app.get("/t/managers")
    @role_required("hall_manager", "atelier_manager")
    def t_managers():
        return "managers"

    @app.get("/t/admin")
    @admin_required
    def t_admin():
        return "admin"

    @app.get("/t/pending-profile")
    @role_required(Role.STAFF, allow_pending=True)
    def t_pending_profile():
        return "profile"

    @app.get("/t/hall/<int:hall_id>")
    @role_required(Role.HALL_MANAGER)
    def t_hall(hall_id):
        return str(perm.hall_or_404(hall_id).id)

    @app.get("/t/atelier/<int:atelier_id>")
    @role_required(Role.ATELIER_MANAGER)
    def t_atelier(atelier_id):
        return str(perm.atelier_or_404(atelier_id).id)

    @app.get("/t/event/<int:event_id>")
    @login_required
    def t_event(event_id):
        capacities = request.args.get("as", "hall,atelier").split(",")
        return str(perm.event_or_404(event_id, as_=capacities, allow_admin=request.args.get("admin") == "1").id)

    @app.get("/t/assignment/<int:assignment_id>")
    @login_required
    def t_assignment(assignment_id):
        capacities = request.args.get("as", "atelier,assignee").split(",")
        return str(perm.assignment_or_404(assignment_id, as_=capacities).id)

    @app.get("/t/roster/<int:member_id>")
    @login_required
    def t_roster(member_id):
        return str(perm.roster_member_or_404(member_id).id)

    @app.get("/t/my-atelier")
    @role_required(Role.ATELIER_MANAGER)
    def t_my_atelier():
        return str(perm.get_my_atelier().id)

    @app.get("/t/my-atelier-optional")
    @role_required(Role.ATELIER_MANAGER)
    def t_my_atelier_optional():
        atelier = perm.get_my_atelier(required=False)
        return str(atelier.id) if atelier else "none"

    @app.get("/t/my-hall")
    @role_required(Role.HALL_MANAGER)
    def t_my_hall():
        return str(perm.get_my_hall().id)

    @app.post("/t/select/<int:atelier_id>")
    @role_required(Role.ATELIER_MANAGER)
    def t_select(atelier_id):
        perm.set_current_atelier(atelier_id)
        return "selected"

    @app.get("/t/owned/<int:pk>")
    @login_required
    def t_owned(pk):
        from app.models import Hall

        return str(perm.owned_or_404(Hall, pk, lambda hall, user: hall.manager_id == user.id).id)

    @app.get("/t/who")
    @login_required
    def t_who():
        return jsonify(halls=sorted(perm.my_hall_ids()), ateliers=sorted(perm.my_atelier_ids()))

    return app


def get(client, url, **kwargs):
    return client.get(url, **kwargs)


def path_of(response):
    return urlsplit(response.headers["Location"]).path


# --------------------------------------------------------------------------
# decorators
# --------------------------------------------------------------------------


def test_open_route_needs_nothing(routes, client):
    assert get(client, "/t/open").status_code == 200


@pytest.mark.parametrize("url", ["/t/any", "/t/staff", "/t/managers", "/t/admin", "/t/hall/1", "/t/atelier/1"])
def test_anonymous_is_sent_to_login_with_next(routes, client, url):
    response = get(client, url)
    assert response.status_code == 302
    assert path_of(response) == "/auth/login"
    assert f"next={url}" in response.headers["Location"]


def test_anonymous_json_gets_401_not_a_redirect(routes, client):
    response = get(client, "/t/staff", headers={"Accept": "application/json"})
    assert response.status_code == 401
    body = response.get_json()
    assert body["ok"] is False and body["code"] == "unauthorized"


def test_anonymous_xhr_gets_401(routes, client):
    assert get(client, "/t/staff", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 401


def test_anonymous_post_redirects_to_login_without_a_post_target(routes, client):
    routes.add_url_rule("/t/post-only", "t_post_only", role_required(Role.STAFF)(lambda: "ok"), methods=["POST"])
    response = client.post("/t/post-only", headers={"Referer": "http://localhost/t/open"})
    assert response.status_code == 302
    assert "next=/t/open" in response.headers["Location"]


@pytest.mark.parametrize(
    "role,url,status",
    [
        ("staff", "/t/staff", 200),
        ("staff", "/t/managers", 403),
        ("staff", "/t/admin", 403),
        ("hall_manager", "/t/managers", 200),
        ("atelier_manager", "/t/managers", 200),
        ("hall_manager", "/t/staff", 403),
        ("admin", "/t/admin", 200),
        ("admin", "/t/staff", 403),  # admins are not implicitly allowed everywhere
        ("admin", "/t/managers", 403),
        ("staff", "/t/any", 200),
        ("admin", "/t/any", 200),
    ],
)
def test_role_matrix(routes, client, make_user, login_as, role, url, status):
    login_as(client, make_user(role))
    assert get(client, url).status_code == status


def test_forbidden_page_and_json(routes, client, make_user, login_as):
    login_as(client, make_user("staff"))
    page = get(client, "/t/admin")
    assert page.status_code == 403 and "به این بخش دسترسی ندارید" in page.get_data(as_text=True)
    body = get(client, "/t/admin", headers={"Accept": "application/json"}).get_json()
    assert body["ok"] is False and body["code"] == "forbidden"


def test_role_required_without_roles_is_a_programming_error():
    with pytest.raises(ValueError):
        role_required()


def test_role_required_accepts_a_collection_and_strings():
    decorated = role_required(["staff", Role.ADMIN])(lambda: "x")
    assert decorated.__atelier_roles__ == frozenset({"staff", "admin"})
    with pytest.raises(ValueError):
        role_required("wizard")


def test_pending_accounts_are_parked_on_the_pending_page(routes, client, make_user, login_as):
    login_as(client, make_user("staff", status="pending"))
    for url in ("/t/any", "/t/staff"):
        response = get(client, url)
        assert response.status_code == 302 and path_of(response) == "/auth/pending", url
    assert get(client, "/t/any-pending-ok").status_code == 200
    assert get(client, "/t/pending-profile").status_code == 200  # onboarding pages opt in
    json = get(client, "/t/staff", headers={"Accept": "application/json"})
    assert json.status_code == 403 and json.get_json()["code"] == "pending_approval"


def test_pending_user_with_the_wrong_role_still_gets_403(routes, client, make_user, login_as):
    login_as(client, make_user("staff", status="pending"))
    assert get(client, "/t/admin").status_code == 403


def test_suspended_account_is_treated_as_anonymous(routes, client, make_user, login_as):
    user = make_user("staff")
    login_as(client, user)
    assert get(client, "/t/staff").status_code == 200
    user.status = "suspended"
    db.session.commit()
    assert get(client, "/t/staff").status_code == 302


def test_is_admin_helper(app, make_user):
    admin, staff = make_user("admin"), make_user("staff")
    with app.test_request_context("/"):
        assert perm.is_admin(admin) is True
        assert perm.is_admin(staff) is False
        assert perm.is_admin() is False  # nobody logged in


# --------------------------------------------------------------------------
# ownership: halls and ateliers
# --------------------------------------------------------------------------


def test_hall_or_404(routes, client, make_hall, make_user, login_as):
    mine = make_hall()
    theirs = make_hall()
    login_as(client, mine.manager)
    assert get(client, f"/t/hall/{mine.id}").status_code == 200
    assert get(client, f"/t/hall/{theirs.id}").status_code == 404
    assert get(client, "/t/hall/99999").status_code == 404  # same answer as for somebody else's


def test_atelier_or_404(routes, client, make_atelier, login_as):
    mine, theirs = make_atelier(), make_atelier()
    login_as(client, mine.manager)
    assert get(client, f"/t/atelier/{mine.id}").status_code == 200
    assert get(client, f"/t/atelier/{theirs.id}").status_code == 404


def test_other_users_objects_are_404_even_with_a_different_role(routes, client, make_hall, make_user, login_as):
    hall = make_hall()
    login_as(client, make_user("hall_manager"))
    assert get(client, f"/t/owned/{hall.id}").status_code == 404


def test_owned_or_404_requires_an_owner_check(routes):
    from app.models import Hall

    with pytest.raises(TypeError):
        perm.owned_or_404(Hall, 1)  # type: ignore[call-arg]


def test_owned_or_404_allow_admin(app, make_hall, make_user):
    from app.models import Hall

    hall, admin = make_hall(), make_user("admin")
    app.add_url_rule(
        "/t/admin-owned/<int:pk>",
        "t_admin_owned",
        perm.login_required(lambda pk: str(perm.owned_or_404(Hall, pk, lambda h, u: False, allow_admin=True).id)),
    )
    from app.services.testing import login_client

    client = app.test_client()
    login_client(client, admin)
    assert client.get(f"/t/admin-owned/{hall.id}").status_code == 200


def test_junk_and_missing_ids_are_404(app, make_hall):
    from flask_login import login_user
    from werkzeug.exceptions import NotFound

    from app.models import Hall

    hall = make_hall()
    with app.test_request_context("/"):
        login_user(hall.manager)
        for junk in ("abc", None, "", 0, -1, 10**9):
            with pytest.raises(NotFound):
                perm.owned_or_404(Hall, junk, lambda obj, user: True)
        assert perm.owned_or_404(Hall, str(hall.id), lambda obj, user: True).id == hall.id  # '7' is fine


def test_helpers_need_a_signed_in_user(app, make_hall):
    from werkzeug.exceptions import Unauthorized

    from app.models import Hall

    hall = make_hall()
    with app.test_request_context("/"):
        with pytest.raises(Unauthorized):
            perm.owned_or_404(Hall, hall.id, lambda obj, user: True)
        with pytest.raises(Unauthorized):
            perm.get_my_halls()


def test_my_ids_and_per_request_cache(routes, client, make_hall, make_atelier, make_user, login_as):
    hall_a = make_hall()
    make_hall()  # somebody else's hall must not show up
    login_as(client, hall_a.manager)
    assert get(client, "/t/who").get_json() == {"halls": [hall_a.id], "ateliers": []}
    manager = make_user("atelier_manager")
    first, second = make_atelier(manager), make_atelier(manager)
    login_as(client, manager)
    assert get(client, "/t/who").get_json() == {"halls": [], "ateliers": sorted([first.id, second.id])}


def test_my_ids_outside_a_request_is_not_cached(app, make_hall):
    hall = make_hall()
    with app.test_request_context("/"):
        from flask_login import login_user

        login_user(hall.manager)
        assert perm.my_hall_ids() == frozenset({hall.id})
    assert perm.my_hall_ids(hall.manager) == frozenset({hall.id})  # explicit user, no request
    new = make_hall(hall.manager)
    assert perm.my_hall_ids(hall.manager) == frozenset({hall.id, new.id})


def test_get_my_atelier_default_selection_and_switching(routes, client, make_atelier, make_user, login_as):
    manager = make_user("atelier_manager")
    first = make_atelier(manager, name="الف")
    second = make_atelier(manager, name="ب")
    foreign = make_atelier()
    login_as(client, manager)
    assert get(client, "/t/my-atelier").get_data(as_text=True) == str(first.id)
    assert client.post(f"/t/select/{second.id}").status_code == 200
    assert get(client, "/t/my-atelier").get_data(as_text=True) == str(second.id)
    assert client.post(f"/t/select/{foreign.id}").status_code == 404  # cannot select somebody else's
    assert get(client, "/t/my-atelier").get_data(as_text=True) == str(second.id)


def test_stale_selected_atelier_falls_back(routes, client, make_atelier, make_user, login_as):
    manager = make_user("atelier_manager")
    only = make_atelier(manager)
    login_as(client, manager)
    with client.session_transaction() as session:
        session["atelier_id"] = 424242
    assert get(client, "/t/my-atelier").get_data(as_text=True) == str(only.id)


def test_get_my_atelier_without_one_redirects_to_onboarding(app, routes, client, make_user, login_as, empty_nav):
    app.add_url_rule("/t/setup", "t_setup", lambda: "setup")
    nav.register_onboarding("atelier_manager", "t_setup", lambda user: False)
    login_as(client, make_user("atelier_manager"))
    response = get(client, "/t/my-atelier")
    assert response.status_code == 302 and path_of(response) == "/t/setup"
    assert get(client, "/t/my-atelier-optional").get_data(as_text=True) == "none"


def test_onboarding_required_json_and_no_onboarding_page(routes, client, make_user, login_as, empty_nav):
    login_as(client, make_user("atelier_manager"))
    body = get(client, "/t/my-atelier", headers={"Accept": "application/json"})
    assert body.status_code == 409 and body.get_json()["code"] == "onboarding_required"
    plain = get(client, "/t/my-atelier")  # nothing registered: a clear 404 page, never a crash
    assert plain.status_code == 404 and "ابتدا اطلاعات اتلیهٔ خود را ثبت کنید" in plain.get_data(as_text=True)


def test_get_my_hall(routes, client, make_hall, make_user, login_as, empty_nav):
    hall = make_hall()
    login_as(client, hall.manager)
    assert get(client, "/t/my-hall").get_data(as_text=True) == str(hall.id)
    login_as(client, make_user("hall_manager"))
    assert get(client, "/t/my-hall").status_code == 404


def test_get_my_atelier_with_explicit_id(app, make_atelier, make_user):
    manager = make_user("atelier_manager")
    mine, foreign = make_atelier(manager), make_atelier()
    from flask_login import login_user
    from werkzeug.exceptions import NotFound

    with app.test_request_context("/"):
        login_user(manager)
        assert perm.get_my_atelier(mine.id).id == mine.id
        with pytest.raises(NotFound):
            perm.get_my_atelier(foreign.id)
        assert [a.id for a in perm.get_my_ateliers()] == [mine.id]
        with pytest.raises(OnboardingRequired):
            login_user(make_user("atelier_manager"))
            perm.get_my_atelier()


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def test_event_access_by_capacity(routes, client, make_event, make_hall, make_atelier, make_assignment, make_user, login_as):
    hall, atelier = make_hall(), make_atelier()
    event = make_event(hall, atelier)
    staff = make_user("staff")
    make_assignment(event, "photographer", user=staff)
    stranger_hall, stranger_atelier, stranger_staff = make_hall(), make_atelier(), make_user("staff")

    def status(user, capacities):
        return login_as(client, user).get(f"/t/event/{event.id}?as={capacities}").status_code

    assert status(hall.manager, "hall") == 200
    assert status(hall.manager, "atelier") == 404
    assert status(atelier.manager, "atelier") == 200
    assert status(atelier.manager, "hall") == 404
    assert status(staff, "assignee") == 200
    assert status(staff, "hall,atelier") == 404
    assert status(stranger_hall.manager, "hall,atelier,assignee,offered") == 404
    assert status(stranger_atelier.manager, "hall,atelier,assignee") == 404
    assert status(stranger_staff, "hall,atelier,assignee,offered") == 404


def test_cancelled_assignment_no_longer_gives_event_access(routes, client, make_event, make_atelier, make_assignment, make_user, login_as):
    event = make_event(atelier=make_atelier())
    staff = make_user("staff")
    make_assignment(event, "photographer", user=staff, status=AssignmentStatus.CANCELLED)
    assert login_as(client, staff).get(f"/t/event/{event.id}?as=assignee").status_code == 404


def test_roster_member_access_reaches_the_linked_account(routes, client, make_event, make_atelier, make_assignment, make_roster_member, make_user, login_as):
    atelier = make_atelier()
    event = make_event(atelier=atelier)
    staff = make_user("staff")
    member = make_roster_member(atelier, user=staff)
    make_assignment(event, "editor", roster_member=member)
    assert login_as(client, staff).get(f"/t/event/{event.id}?as=assignee").status_code == 200


def test_open_events_are_visible_to_every_atelier_but_not_to_halls_or_staff(routes, client, make_event, make_hall, make_atelier, make_user, login_as):
    event = make_event(make_hall(), None, status=EventStatus.OPEN, offer_mode=OfferMode.OPEN)
    other_atelier = make_atelier()
    assert login_as(client, other_atelier.manager).get(f"/t/event/{event.id}?as=offered").status_code == 200
    assert login_as(client, make_user("staff")).get(f"/t/event/{event.id}?as=offered").status_code == 404
    assert login_as(client, make_hall().manager).get(f"/t/event/{event.id}?as=offered").status_code == 404


def test_direct_offer_is_visible_to_the_offered_atelier_only(routes, client, make_event, make_hall, make_atelier, login_as):
    event = make_event(make_hall(), None, status=EventStatus.OFFERED, offer_mode=OfferMode.DIRECT)
    offered, other = make_atelier(), make_atelier()
    db.session.add(EventOffer(event=event, atelier=offered, status=OfferStatus.PENDING))
    db.session.commit()
    assert login_as(client, offered.manager).get(f"/t/event/{event.id}?as=offered").status_code == 200
    assert login_as(client, other.manager).get(f"/t/event/{event.id}?as=offered").status_code == 404


def test_declined_offer_gives_no_access(routes, client, make_event, make_hall, make_atelier, login_as):
    event = make_event(make_hall(), None, status=EventStatus.DRAFT)
    atelier = make_atelier()
    db.session.add(EventOffer(event=event, atelier=atelier, status=OfferStatus.DECLINED))
    db.session.commit()
    assert login_as(client, atelier.manager).get(f"/t/event/{event.id}?as=offered").status_code == 404


def test_admin_passes_only_when_asked_to(routes, client, make_event, make_user, login_as):
    event = make_event()
    admin = make_user("admin")
    assert login_as(client, admin).get(f"/t/event/{event.id}").status_code == 404
    assert login_as(client, admin).get(f"/t/event/{event.id}?admin=1").status_code == 200


def test_event_access_object(app, make_event, make_hall, make_atelier):
    from flask_login import login_user

    hall, atelier = make_hall(), make_atelier()
    event = make_event(hall, atelier)
    with app.test_request_context("/"):
        assert perm.event_access(event).any is False  # anonymous
        login_user(hall.manager)
        access = perm.event_access(event)
        assert access.hall and not access.atelier and access.any
        assert access.allows(["atelier", "hall"]) and not access.allows(["atelier"])


# --------------------------------------------------------------------------
# assignments, roster, invoices, stock, equipment, job posts
# --------------------------------------------------------------------------


def test_assignment_or_404(routes, client, make_event, make_hall, make_atelier, make_assignment, make_user, login_as):
    hall, atelier = make_hall(), make_atelier()
    event = make_event(hall, atelier)
    staff = make_user("staff")
    assignment = make_assignment(event, "photographer", user=staff)
    url = f"/t/assignment/{assignment.id}"

    assert login_as(client, atelier.manager).get(url).status_code == 200
    assert login_as(client, staff).get(url).status_code == 200  # the assignee
    assert login_as(client, hall.manager).get(url).status_code == 404  # hall not in the default capacities
    assert login_as(client, hall.manager).get(url + "?as=hall").status_code == 200
    assert login_as(client, make_user("staff")).get(url).status_code == 404
    assert login_as(client, make_atelier().manager).get(url).status_code == 404
    assert login_as(client, staff).get(url + "?as=atelier").status_code == 404  # narrower capacity


def test_assignment_through_roster_member_belongs_to_the_linked_user(routes, client, make_event, make_atelier, make_assignment, make_roster_member, make_user, login_as):
    atelier = make_atelier()
    staff = make_user("staff")
    member = make_roster_member(atelier, user=staff)
    assignment = make_assignment(make_event(atelier=atelier), "editor", roster_member=member)
    assert login_as(client, staff).get(f"/t/assignment/{assignment.id}?as=assignee").status_code == 200


def test_roster_member_or_404(routes, client, make_atelier, make_roster_member, login_as):
    mine, theirs = make_atelier(), make_atelier()
    member, foreign = make_roster_member(mine), make_roster_member(theirs)
    login_as(client, mine.manager)
    assert get(client, f"/t/roster/{member.id}").status_code == 200
    assert get(client, f"/t/roster/{foreign.id}").status_code == 404


def test_atelier_scoped_helpers(app, make_atelier, make_event, make_user, make_job_post):
    from flask_login import login_user
    from werkzeug.exceptions import NotFound

    mine, theirs = make_atelier(), make_atelier()
    my_event, foreign_event = make_event(atelier=mine), make_event(atelier=theirs)
    invoice = Invoice(event=my_event, atelier=mine)
    foreign_invoice = Invoice(event=foreign_event, atelier=theirs)
    stock = StockItem(atelier=mine, kind="paper", name="کاغذ A4", unit="بسته")
    foreign_stock = StockItem(atelier=theirs, kind="paper", name="کاغذ", unit="بسته")
    gear = Equipment(atelier=mine, kind="camera", name="کانن")
    foreign_gear = Equipment(atelier=theirs, kind="camera", name="نیکون")
    db.session.add_all([invoice, foreign_invoice, stock, foreign_stock, gear, foreign_gear])
    db.session.commit()
    post, foreign_post = make_job_post(mine), make_job_post(theirs)

    with app.test_request_context("/"):
        login_user(mine.manager)
        assert perm.invoice_or_404(invoice.id).id == invoice.id
        assert perm.stock_item_or_404(stock.id).id == stock.id
        assert perm.equipment_or_404(gear.id).id == gear.id
        assert perm.job_post_or_404(post.id).id == post.id
        for helper, foreign in [
            (perm.invoice_or_404, foreign_invoice),
            (perm.stock_item_or_404, foreign_stock),
            (perm.equipment_or_404, foreign_gear),
            (perm.job_post_or_404, foreign_post),
        ]:
            with pytest.raises(NotFound):
                helper(foreign.id)
        with pytest.raises(NotFound):
            perm.invoice_or_404(99999)


def test_hall_manager_and_staff_never_see_the_books(app, make_atelier, make_event, make_hall, make_user):
    from flask_login import login_user
    from werkzeug.exceptions import NotFound

    atelier, hall = make_atelier(), make_hall()
    event = make_event(hall, atelier)
    invoice = Invoice(event=event, atelier=atelier)
    db.session.add(invoice)
    db.session.commit()
    for user in (hall.manager, make_user("staff")):
        with app.test_request_context("/"):
            login_user(user)
            with pytest.raises(NotFound):
                perm.invoice_or_404(invoice.id)


def test_job_application_or_404(app, make_atelier, make_job_post, make_user):
    from flask_login import login_user
    from werkzeug.exceptions import NotFound

    atelier = make_atelier()
    post = make_job_post(atelier)
    applicant, other = make_user("staff"), make_user("staff")
    application = JobApplication(post=post, user=applicant)
    db.session.add(application)
    db.session.commit()
    with app.test_request_context("/"):
        login_user(applicant)
        assert perm.job_application_or_404(application.id).id == application.id
    with app.test_request_context("/"):
        login_user(atelier.manager)
        assert perm.job_application_or_404(application.id).id == application.id
        with pytest.raises(NotFound):
            perm.job_application_or_404(application.id, as_=("applicant",))
    for outsider in (other, make_atelier().manager):
        with app.test_request_context("/"):
            login_user(outsider)
            with pytest.raises(NotFound):
                perm.job_application_or_404(application.id)


# --------------------------------------------------------------------------
# list scoping
# --------------------------------------------------------------------------


def test_scope_helpers_limit_list_queries(app, make_atelier, make_event, make_hall):
    from flask_login import login_user

    from app.models import Event

    mine, theirs = make_atelier(), make_atelier()
    hall = make_hall()
    own = [make_event(hall, mine), make_event(hall, mine)]
    make_event(hall, theirs)
    with app.test_request_context("/"):
        login_user(mine.manager)
        rows = db.session.execute(perm.scope_to_my_ateliers(sa.select(Event), Event.atelier_id)).scalars().all()
        assert {e.id for e in rows} == {e.id for e in own}
        assert db.session.execute(perm.scope_to_my_halls(sa.select(Event), Event.hall_id)).scalars().all() == []
    with app.test_request_context("/"):
        login_user(hall.manager)
        assert len(db.session.execute(perm.scope_to_my_halls(sa.select(Event), Event.hall_id)).scalars().all()) == 3
