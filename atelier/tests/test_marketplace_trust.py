"""Trust and safety (reports, verified badges), the menu entries and the other cross-module contracts."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from test_marketplace_support import body, mc, now, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import AuditLog, Notification, Review
from app.services import nav
from app.services.constants import JobPostStatus


def reports():
    db.session.expire_all()
    return list(db.session.scalars(sa.select(AuditLog).where(AuditLog.action.like("%.reported")).order_by(AuditLog.id)))


# --------------------------------------------------------------------------
# reporting a post
# --------------------------------------------------------------------------


def test_a_signed_in_user_can_report_a_post_and_the_admins_are_told(world, sc):
    response = sc.post(f"/jobs/{world.post.id}/report", data={"reason": "money", "details": "پیش‌پرداخت خواست"})
    assert response.status_code == 302
    [report] = reports()
    assert (report.action, report.entity_type, report.entity_id, report.actor_id) == ("job_post.reported", "job_posts", world.post.id, world.staff.id)
    assert report.data == {"reason": "money", "details": "پیش‌پرداخت خواست"}
    note = db.session.scalar(sa.select(Notification).where(Notification.user_id == world.admin.id))
    assert "گزارش آگهی" in note.title and "درخواست پول" in note.body and note.url == f"/jobs/{world.post.id}"
    assert reload(world.post).status == JobPostStatus.OPEN  # nothing is hidden automatically


def test_reporting_twice_is_refused(world, sc):
    sc.post(f"/jobs/{world.post.id}/report", data={"reason": "fake"})
    again = sc.post(f"/jobs/{world.post.id}/report", json={"reason": "fake"})
    assert again.status_code == 409 and again.get_json()["code"] == "duplicate"
    assert len(reports()) == 1


def test_an_unknown_reason_or_a_missing_one_is_422(world, sc):
    for data in ({"reason": "bogus"}, {}):
        assert sc.post(f"/jobs/{world.post.id}/report", json=data).status_code == 422
    assert reports() == []


def test_the_daily_report_limit(app, world, sc):
    app.config["MARKETPLACE_MAX_REPORTS_PER_DAY"] = 1
    assert sc.post(f"/jobs/{world.post.id}/report", data={"reason": "fake"}).status_code == 302
    blocked = sc.post(f"/jobs/{world.rival_post.id}/report", json={"reason": "fake"})
    assert blocked.status_code == 429
    assert len(reports()) == 1


def test_the_owner_cannot_report_his_own_post_and_visitors_must_log_in(app, world, mc):
    assert mc.post(f"/jobs/{world.post.id}/report", data={"reason": "fake"}).status_code == 404
    anonymous = app.test_client().post(f"/jobs/{world.post.id}/report", data={"reason": "fake"})
    assert anonymous.status_code == 302 and "/auth/login" in anonymous.headers["Location"]
    assert app.test_client().post("/jobs/99999/report", data={"reason": "fake"}).status_code == 302
    assert reports() == []


def test_the_report_button_is_shown_to_members_only(app, world, sc, mc):
    assert "گزارش این آگهی" in body(sc.get(f"/jobs/{world.post.id}"))
    assert "گزارش این آگهی" not in body(mc.get(f"/jobs/{world.post.id}"))
    assert "گزارش این آگهی" not in body(app.test_client().get(f"/jobs/{world.post.id}"))


# --------------------------------------------------------------------------
# reporting a review
# --------------------------------------------------------------------------


def test_the_person_a_review_is_about_can_report_it(world, mc, sc, client_for, make_assignment):
    assignment = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "1", "comment": "ناعادلانه"})
    review = db.session.scalar(sa.select(Review))
    assert "گزارش این نظر" in body(sc.get("/jobs/reviews?tab=received"))
    assert client_for(world.other).post(f"/jobs/reviews/{review.id}/report", data={"reason": "abuse"}).status_code == 404
    assert mc.post(f"/jobs/reviews/{review.id}/report", data={"reason": "abuse"}).status_code == 404  # the author is not the subject
    assert sc.post(f"/jobs/reviews/{review.id}/report", data={"reason": "abuse", "details": "توهین"}).status_code == 302
    [report] = reports()
    assert (report.action, report.entity_type, report.entity_id) == ("review.reported", "reviews", review.id)
    assert sc.post(f"/jobs/reviews/{review.id}/report", json={"reason": "abuse"}).status_code == 409


def test_a_hidden_review_cannot_be_reported(world, sc, make_assignment):
    assignment = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    review = Review(assignment=assignment, author=world.manager, subject=world.staff, rating=1, is_hidden=True)
    db.session.add(review)
    db.session.commit()
    assert sc.post(f"/jobs/reviews/{review.id}/report", data={"reason": "abuse"}).status_code == 404


# --------------------------------------------------------------------------
# badges and the menu
# --------------------------------------------------------------------------


def test_the_verified_badge_follows_the_user_flag(app, world):
    assert "تأییدشده" in body(app.test_client().get("/jobs"))
    world.manager.is_verified = False
    db.session.commit()
    html = body(app.test_client().get(f"/jobs/{world.post.id}"))
    assert "تأییدشده" not in html


def test_menu_entries_per_role(app, world):
    def endpoints(user):
        return {item.endpoint for item in nav.items_for(user) if item.endpoint.startswith("marketplace.")}

    assert endpoints(world.staff) == {"marketplace.jobs", "marketplace.my_applications"}
    assert endpoints(world.manager) == {"marketplace.manage_jobs", "marketplace.talent_index"}
    assert endpoints(world.hall_manager) == {"marketplace.talent_index"}


def test_the_links_the_atelier_panel_builds_resolve_to_this_module(app, world):
    """``ateliers.links`` finds ``marketplace.job_new`` and ``marketplace.manage_jobs`` by name."""
    links = pytest.importorskip("app.modules.ateliers.links")

    with app.test_request_context():
        url = links.marketplace_job_url(world.event, "photographer", {"base": 1, "commission_per_unit": 2, "bonus_tiers": [{"threshold": 70, "amount": 3}]})
        assert url.startswith("/jobs/new?") and "tiers=70:3" in url and f"event={world.event.id}" in url
        assert links.job_posts_url() == "/jobs/manage"


def test_the_afish_prefill_roundtrip_creates_the_post_the_atelier_panel_asked_for(world, mc):
    links = pytest.importorskip("app.modules.ateliers.links")

    with mc.application.test_request_context():
        url = links.marketplace_job_url(world.event, "editor", {"base": 500_000, "commission_per_unit": 4_000, "bonus_tiers": [{"threshold": 50, "amount": 100_000}]}, slots=2)
    html = body(mc.get(url))
    assert 'value="500000"' in html and 'value="4000"' in html and 'value="۵۰"' in html and 'value="100000"' in html
    assert '<option value="editor" selected>' in html
