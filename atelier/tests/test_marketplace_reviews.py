"""Two-way reviews: only after a completed assignment, once per author, ratings recomputed, hidden respected."""

from __future__ import annotations

import sqlalchemy as sa
from test_marketplace_support import body, day, mc, now, reload, sc, world  # noqa: F401

from app.extensions import db
from app.models import AuditLog, Notification, Review
from app.modules.marketplace import queries
from app.services.constants import Role


def review_count() -> int:
    db.session.expire_all()
    return db.session.scalar(sa.select(sa.func.count(Review.id)))


def reviews_by(user):
    db.session.expire_all()
    return list(db.session.scalars(sa.select(Review).where(Review.author_id == user.id)))


def completed_for(world, make_assignment, user=None, role="photographer", event=None):
    return make_assignment(event or world.event, role, user=user or world.staff, status="completed")


# --------------------------------------------------------------------------
# writing a review
# --------------------------------------------------------------------------


def test_the_manager_reviews_the_person_and_the_rating_is_recomputed(world, mc, make_assignment):
    assignment = completed_for(world, make_assignment)
    html = body(mc.get(f"/jobs/reviews/new/{assignment.id}"))
    assert "ثبت نظر دربارهٔ سارا نیرو" in html and 'name="rating"' in html
    response = mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "4", "comment": "وقت‌شناس و حرفه‌ای"})
    assert response.status_code == 302 and "/jobs/reviews?tab=written" in response.headers["Location"]
    [review] = reviews_by(world.manager)
    assert (review.subject_id, review.rating, review.comment, review.is_hidden) == (world.staff.id, 4, "وقت‌شناس و حرفه‌ای", False)
    profile = reload(world.profile)
    assert (profile.rating_avg, profile.rating_count) == (4.0, 1)
    note = db.session.scalar(sa.select(Notification).where(Notification.user_id == world.staff.id, Notification.kind == "review_received"))
    assert "۴ از ۵" in note.title + note.body and note.url == "/jobs/reviews?tab=received"
    assert "review.created" in [row.action for row in db.session.scalars(sa.select(AuditLog))]


def test_the_person_reviews_the_atelier(world, sc, make_assignment):
    assignment = completed_for(world, make_assignment)
    html = body(sc.get(f"/jobs/reviews/new/{assignment.id}"))
    assert "ثبت نظر دربارهٔ اتلیه ستاره" in html
    assert sc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "5", "comment": "پرداخت به‌موقع"}).status_code == 302
    [review] = reviews_by(world.staff)
    assert review.subject_id == world.manager.id and review.rating == 5
    # the staff profile of the person is untouched; the atelier's rating is computed from the reviews
    assert reload(world.profile).rating_count == 0
    assert queries.atelier_ratings([world.atelier.id])[world.atelier.id] == queries.Rating(5.0, 1)


def test_each_side_reviews_once_and_both_can(world, mc, sc, make_assignment):
    assignment = completed_for(world, make_assignment)
    assert mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "5"}).status_code == 302
    assert sc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "3"}).status_code == 302
    assert review_count() == 2
    for client, rating in ((mc, "1"), (sc, "1")):
        again = client.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": rating, "comment": "دوباره"})
        assert again.status_code == 409
    assert review_count() == 2
    assert {r.rating for r in db.session.scalars(sa.select(Review))} == {5, 3}


def test_the_form_of_an_already_reviewed_assignment_shows_the_review_not_a_form(world, mc, make_assignment):
    assignment = completed_for(world, make_assignment)
    mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "4", "comment": "خوب بود"})
    html = body(mc.get(f"/jobs/reviews/new/{assignment.id}"))
    assert "نظر شما ثبت شده است" in html and "خوب بود" in html and 'name="rating"' not in html


def test_the_unique_constraint_backs_up_the_check(world, make_assignment):
    assignment = completed_for(world, make_assignment)
    db.session.add(Review(assignment=assignment, author=world.manager, subject=world.staff, rating=3))
    db.session.commit()
    db.session.add(Review(assignment=assignment, author=world.manager, subject=world.staff, rating=4))
    try:
        db.session.commit()
        raised = False
    except Exception:  # noqa: BLE001 - IntegrityError
        db.session.rollback()
        raised = True
    assert raised


def test_the_average_is_recomputed_from_all_visible_reviews(world, mc, make_assignment, make_event):
    for n, stars in enumerate((5, 4, 4)):
        assignment = make_assignment(make_event(world.hall, world.atelier, date=day(40 + n)), "photographer", user=world.staff, status="completed")
        assert mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": str(stars)}).status_code == 302
    profile = reload(world.profile)
    assert profile.rating_count == 3 and profile.rating_avg == 4.33  # 13 / 3, two decimals


def test_hidden_reviews_are_left_out_when_the_rating_is_refreshed(world, make_assignment, mc):
    a1 = completed_for(world, make_assignment, role="photographer")
    a2 = completed_for(world, make_assignment, role="seller")
    mc.post(f"/jobs/reviews/new/{a1.id}", data={"rating": "5"})
    mc.post(f"/jobs/reviews/new/{a2.id}", data={"rating": "1"})
    assert (reload(world.profile).rating_avg, reload(world.profile).rating_count) == (3.0, 2)
    bad = db.session.scalar(sa.select(Review).where(Review.rating == 1))
    bad.is_hidden = True  # what the admin module does
    db.session.commit()
    profile = reload(world.profile)
    profile.refresh_rating()
    db.session.commit()
    assert (reload(world.profile).rating_avg, reload(world.profile).rating_count) == (5.0, 1)


# --------------------------------------------------------------------------
# who may review, and when
# --------------------------------------------------------------------------


def test_only_completed_assignments_can_be_reviewed(world, mc, sc, make_assignment, make_event):
    for n, status in enumerate(("invited", "accepted", "confirmed", "cancelled", "declined")):
        event = make_event(world.hall, world.atelier, date=day(50 + n))
        assignment = make_assignment(event, "photographer", user=world.staff, status=status)
        for client in (mc, sc):
            page = client.get(f"/jobs/reviews/new/{assignment.id}")
            assert page.status_code == 200 and "هنوز زود است" in body(page) and 'name="rating"' not in body(page), status
            assert client.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "5"}).status_code == 409, status
    assert review_count() == 0


def test_strangers_get_404(world, make_assignment, client_for, make_user):
    assignment = completed_for(world, make_assignment)
    stranger = make_user(Role.STAFF)
    for user in (world.rival_manager, world.other, stranger):
        client = client_for(user)
        assert client.get(f"/jobs/reviews/new/{assignment.id}").status_code == 404
        assert client.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "5"}).status_code == 404
    assert client_for(world.hall_manager).get(f"/jobs/reviews/new/{assignment.id}").status_code == 403
    assert review_count() == 0


def test_a_roster_member_without_an_account_cannot_be_reviewed(world, mc, make_assignment, make_roster_member):
    member = make_roster_member(world.atelier, display_name="بدون حساب")
    assignment = make_assignment(world.event, "photographer", roster_member=member, status="completed")
    assert mc.get(f"/jobs/reviews/new/{assignment.id}").status_code == 404
    assert "بدون حساب" not in body(mc.get("/jobs/reviews"))


def test_a_roster_member_with_an_account_can_review_and_be_reviewed(world, mc, sc, make_assignment, make_roster_member):
    member = make_roster_member(world.atelier, user=world.staff)
    assignment = make_assignment(world.event, "photographer", roster_member=member, status="completed")
    assert mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "4"}).status_code == 302
    assert reviews_by(world.manager)[0].subject_id == world.staff.id
    assert sc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "5"}).status_code == 302
    assert reviews_by(world.staff)[0].subject_id == world.manager.id


def test_validation_of_the_rating_and_the_comment(world, mc, make_assignment):
    assignment = completed_for(world, make_assignment)
    url = f"/jobs/reviews/new/{assignment.id}"
    missing = mc.post(url, data={"comment": "متن من"})
    assert missing.status_code == 422 and "امتیاز را" in body(missing) and "متن من" in body(missing)
    for bad in ("0", "6", "-1", "abc", "4.5"):
        assert mc.post(url, data={"rating": bad}).status_code == 422, bad
    long = mc.post(url, data={"rating": "5", "comment": "ب" * 1001})
    assert long.status_code == 422 and "نباید بیش از" in body(long)
    assert review_count() == 0
    assert mc.post(url, data={"rating": "۵"}).status_code == 302  # Persian digits are fine


def test_a_review_comment_is_escaped_everywhere_it_is_shown(world, mc, sc, make_assignment):
    assignment = completed_for(world, make_assignment)
    payload = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "2", "comment": payload})
    for client, url in ((mc, "/jobs/reviews?tab=written"), (sc, "/jobs/reviews?tab=received"), (mc, f"/talent/{world.staff.id}")):
        html = body(client.get(url))
        assert "<img src=x" not in html and "<script>alert(2)" not in html, url
        assert "&lt;img src=x" in html, url


# --------------------------------------------------------------------------
# the reviews hub
# --------------------------------------------------------------------------


def test_the_hub_lists_pending_written_and_received(world, mc, sc, make_assignment):
    assignment = completed_for(world, make_assignment)
    assert "سارا نیرو" in body(mc.get("/jobs/reviews"))  # pending for the manager
    assert "اتلیه ستاره" in body(sc.get("/jobs/reviews"))  # pending for the person
    mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "4", "comment": "عالی"})
    assert "نظر بی‌پاسخی ندارید" in body(mc.get("/jobs/reviews"))
    assert "عالی" in body(mc.get("/jobs/reviews?tab=written"))
    assert "دربارهٔ سارا نیرو" in body(mc.get("/jobs/reviews?tab=written"))
    received = body(sc.get("/jobs/reviews?tab=received"))
    assert "عالی" in received and "اتلیه ستاره" in received  # the author is shown as the atelier
    assert "عالی" not in body(mc.get("/jobs/reviews?tab=received"))
    assert "هنوز نظری دریافت نکرده‌اید" in body(mc.get("/jobs/reviews?tab=received"))


def test_hidden_reviews_do_not_show_in_received_lists(world, mc, sc, make_assignment):
    assignment = completed_for(world, make_assignment)
    mc.post(f"/jobs/reviews/new/{assignment.id}", data={"rating": "1", "comment": "نظر بد"})
    review = db.session.scalar(sa.select(Review))
    review.is_hidden = True
    db.session.commit()
    assert "نظر بد" not in body(sc.get("/jobs/reviews?tab=received"))
    assert "نظر بد" not in body(mc.get(f"/talent/{world.staff.id}"))


def test_the_hub_is_scoped_to_the_viewer(world, make_assignment, client_for):
    completed_for(world, make_assignment)
    rival_view = body(client_for(world.rival_manager).get("/jobs/reviews"))
    other_view = body(client_for(world.other).get("/jobs/reviews"))
    assert "سارا نیرو" not in rival_view and "اتلیه ستاره" not in other_view


def test_the_hub_paginates_pending_reviews(world, mc, make_assignment, make_event, make_user):
    for n in range(12):
        person = make_user(Role.STAFF, full_name=f"نیرو {n}", mobile=f"0914000{n:04d}")
        make_assignment(make_event(world.hall, world.atelier, date=day(60 + n)), "photographer", user=person, status="completed")
    first = body(mc.get("/jobs/reviews"))
    second = body(mc.get("/jobs/reviews?page=2"))
    assert first.count("ثبت نظر</span>") == 10 and second.count("ثبت نظر</span>") == 2
    assert "۱۲" in first  # the tab badge


def test_unknown_tab_falls_back_to_pending(world, mc, make_assignment):
    completed_for(world, make_assignment)
    assert "سارا نیرو" in body(mc.get("/jobs/reviews?tab=bogus"))


# --------------------------------------------------------------------------
# atelier ratings are per atelier
# --------------------------------------------------------------------------


def test_atelier_ratings_are_per_atelier_and_ignore_the_other_direction(world, make_assignment, make_atelier, make_event):
    second = make_atelier(world.manager, name="شعبهٔ دوم", city="اصفهان")
    second_event = make_event(world.hall, second, date=day(30))
    a1 = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    a2 = make_assignment(second_event, "photographer", user=world.staff, status="completed")
    db.session.add_all([
        Review(assignment=a1, author=world.staff, subject=world.manager, rating=5),
        Review(assignment=a2, author=world.staff, subject=world.manager, rating=2),
        Review(assignment=a1, author=world.manager, subject=world.staff, rating=1),  # about the person, not the atelier
    ])
    db.session.commit()
    ratings = queries.atelier_ratings([world.atelier.id, second.id, world.rival.id])
    assert ratings[world.atelier.id] == queries.Rating(5.0, 1)
    assert ratings[second.id] == queries.Rating(2.0, 1)
    assert world.rival.id not in ratings
