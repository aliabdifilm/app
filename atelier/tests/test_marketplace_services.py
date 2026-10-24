"""Unit tests of the marketplace helpers, forms and services (no HTTP)."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from test_marketplace_support import BIG_TERMS, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Assignment, JobApplication
from app.modules.marketplace import common, forms, queries, services
from app.services import payroll, staffing
from app.services.constants import (
    ApplicationStatus,
    AssignmentSource,
    AssignmentStatus,
    JobPostStatus,
    Role,
    Skill,
)
from app.services.forms import Form

# --------------------------------------------------------------------------
# common: wording and examples (the numbers come from payroll)
# --------------------------------------------------------------------------


def test_terms_lines_are_plain_persian():
    assert common.terms_lines(BIG_TERMS) == [
        "پایهٔ ثابت ۱٬۰۰۰٬۰۰۰ تومان",
        "به‌ازای هر عکس فروخته‌شده ۹٬۰۰۰ تومان پورسانت",
        "پاداش: با فروش ۷۰ عکس به بالا، ۱۵۰٬۰۰۰ تومان",
    ]
    assert common.terms_lines({"base": 0, "commission_per_unit": 0, "bonus_tiers": []}) == []
    assert common.terms_text(None) == "مبلغ با توافق تعیین می‌شود"


def test_the_example_is_the_payroll_result():
    example = common.pay_example(BIG_TERMS)
    expected = payroll.compute_pay(payroll.terms_from_dict(BIG_TERMS), 80)
    assert example["total"] == expected.total == 1_870_000
    assert (example["base"], example["commission"], example["bonus"]) == (1_000_000, 720_000, 150_000)
    assert example["sentence"] == "اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان"
    assert example["formula"] == "۱٬۰۰۰٬۰۰۰ + ۷۲۰٬۰۰۰ + ۱۵۰٬۰۰۰"


@pytest.mark.parametrize("units,total", [(0, 1_000_000), (69, 1_621_000), (70, 1_780_000), (80, 1_870_000)])
def test_the_bonus_boundary_is_inclusive(units, total):
    assert common.pay_example(BIG_TERMS, units)["total"] == total  # "70 and above" includes 70


def test_only_the_highest_reached_tier_counts():
    terms = {"base": 0, "commission_per_unit": 1000, "bonus_tiers": [{"threshold": 50, "amount": 100}, {"threshold": 100, "amount": 300}]}
    assert common.pay_example(terms, 80)["bonus"] == 100
    assert common.pay_example(terms, 100)["bonus"] == 300
    assert common.has_stacking_note(terms) and not common.has_stacking_note(BIG_TERMS)


def test_empty_or_broken_terms_have_no_example():
    assert common.pay_example(None) is None
    assert common.pay_example({"base": 0, "commission_per_unit": 0, "bonus_tiers": []}) is None
    assert common.pay_example({"base": "x"}) is None
    assert common.reference_total(None) == 0


def test_scenarios_always_include_the_reference_volume_and_the_thresholds():
    rows = common.pay_scenarios(BIG_TERMS)
    assert [row["units"] for row in rows] == [40, 70, 80, 120]
    many = {"base": 1, "commission_per_unit": 1, "bonus_tiers": [{"threshold": t, "amount": t} for t in (10, 20, 30, 45, 55, 65, 90)]}
    units = [row["units"] for row in common.pay_scenarios(many)]
    assert len(units) <= common.MAX_SCENARIOS and 80 in units


def test_person_name_never_falls_back_to_the_mobile(make_user):
    nameless = make_user(Role.STAFF, full_name="", mobile="09121234567")
    assert nameless.display_name == "09121234567"  # the trap
    assert common.person_name(nameless) == "کاربر بی‌نام"
    typed_number = make_user(Role.STAFF, full_name="0912 123 4567", mobile="09121234568")
    assert common.person_name(typed_number) == "کاربر بی‌نام"
    assert common.person_name(make_user(Role.STAFF, full_name="  علی  ")) == "علی"


def test_clip():
    assert common.clip("a  b\n c") == "a b c"
    assert common.clip("x" * 200, 10) == "xxxxxxxxx…"
    assert common.clip(None) == ""


def test_the_tiers_query_of_the_afish_contract():
    assert common.parse_tiers_query("100:300000,70:150000") == [
        {"threshold": 70, "amount": 150000},
        {"threshold": 100, "amount": 300000},
    ]
    assert common.parse_tiers_query("۷۰:۱۵۰۰۰۰") == [{"threshold": 70, "amount": 150000}]
    assert common.parse_tiers_query("a:b,70,:5,0:5,70:1,70:2") == [{"threshold": 70, "amount": 1}]
    assert common.parse_tiers_query(None) == []
    assert len(common.parse_tiers_query(",".join(f"{n}:1" for n in range(1, 30)))) == 8


def test_the_limits_come_from_the_config(app):
    assert common.limit("MARKETPLACE_MAX_POSTS_PER_DAY") == 10
    app.config["MARKETPLACE_MAX_POSTS_PER_DAY"] = "7"
    assert common.limit("MARKETPLACE_MAX_POSTS_PER_DAY") == 7
    app.config["MARKETPLACE_MAX_POSTS_PER_DAY"] = "junk"
    assert common.limit("MARKETPLACE_MAX_POSTS_PER_DAY") == 10


# --------------------------------------------------------------------------
# forms
# --------------------------------------------------------------------------


def test_read_terms_accepts_persian_digits_and_sorts_the_tiers(app):
    from werkzeug.datastructures import MultiDict

    data = MultiDict([("base", "۱٬۰۰۰٬۰۰۰"), ("commission", "9000"), ("tier_threshold[]", "۱۰۰"), ("tier_amount[]", "۳۰۰٬۰۰۰"),
                      ("tier_threshold[]", "70"), ("tier_amount[]", "150000"), ("tier_threshold[]", ""), ("tier_amount[]", "")])
    form = Form(data)
    terms = forms.read_terms(form)
    assert terms == {"base": 1_000_000, "commission_per_unit": 9_000,
                     "bonus_tiers": [{"threshold": 70, "amount": 150_000}, {"threshold": 100, "amount": 300_000}]}
    assert form.ok


def test_read_terms_reports_each_problem(app):
    from werkzeug.datastructures import MultiDict

    form = Form(MultiDict([("base", "-5"), ("commission", "1.5")]))
    assert forms.read_terms(form) is None
    assert "base" in form.errors and "commission" in form.errors
    form = Form(MultiDict([("tier_threshold[]", "70"), ("tier_amount[]", "")]))
    assert forms.read_terms(form) is None and "tiers" in form.errors
    too_many = MultiDict([(k, str(n)) for n in range(1, 10) for k in ("tier_threshold[]", "tier_amount[]")])
    form = Form(too_many)
    assert forms.read_terms(form) is None and "حداکثر" in form.errors["tiers"]


def test_money_in_terms_is_always_an_integer(app):
    from werkzeug.datastructures import MultiDict

    terms = forms.read_terms(Form(MultiDict([("base", "1000000"), ("commission", "9000")])))
    assert all(isinstance(terms[key], int) for key in ("base", "commission_per_unit"))
    assert forms.read_terms(Form(MultiDict([("base", "1000000.5")]))) is None


def test_board_filters_normalise_digits_and_dates(app):
    filters = forms.read_board_filters({"min_pay": "۱٬۸۰۰٬۰۰۰", "date_from": "۱۴۰۴/۰۴/۲۶", "role": "seller", "sort": "pay", "city": " شیراز "})
    assert (filters.min_pay, filters.date_from, filters.role, filters.sort, filters.city) == (1_800_000, dt.date(2025, 7, 17), Skill.SELLER, "pay", "شیراز")
    assert filters.active and not filters.errors
    junk = forms.read_board_filters({"role": "x", "min_pay": "abc", "date_from": "yy", "sort": "?"})
    assert junk.role is None and junk.min_pay is None and junk.date_from is None and junk.sort == "date"
    assert set(junk.errors) == {"role", "min_pay", "date_from"}


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------


def test_sync_post_status_follows_accepted_count(world, make_user, make_staff_profile):
    post = world.post
    post.slots = 2
    db.session.commit()
    users = [world.staff]
    for n in range(2):
        user = make_user(Role.STAFF, full_name=f"p{n}", mobile=f"0915000000{n}")
        make_staff_profile(user, skills=(Skill.PHOTOGRAPHER,))
        users.append(user)
    applications = [JobApplication(post=post, user=u, status=ApplicationStatus.PENDING) for u in users]
    db.session.add_all(applications)
    db.session.commit()
    services.sync_post_status(post)
    assert post.status == JobPostStatus.OPEN
    applications[0].status = ApplicationStatus.ACCEPTED
    db.session.flush()
    services.sync_post_status(post)
    assert post.status == JobPostStatus.OPEN
    applications[1].status = ApplicationStatus.ACCEPTED
    db.session.flush()
    services.sync_post_status(post)
    assert post.status == JobPostStatus.FILLED
    applications[1].status = ApplicationStatus.WITHDRAWN
    db.session.flush()
    services.sync_post_status(post)
    assert post.status == JobPostStatus.OPEN


def test_sync_leaves_closed_and_hidden_posts_alone(world):
    for status in (JobPostStatus.CLOSED, JobPostStatus.HIDDEN):
        world.post.status = status
        db.session.flush()
        services.sync_post_status(world.post)
        assert world.post.status == status


def test_the_bus_handler_is_idempotent_and_ignores_other_changes(world, make_staff_profile):
    application = JobApplication(post=world.post, user=world.staff, status=ApplicationStatus.ACCEPTED)
    db.session.add(application)
    db.session.commit()
    assignment = Assignment(
        event=world.event, role=Skill.PHOTOGRAPHER, user=world.staff, status=AssignmentStatus.CANCELLED,
        source=AssignmentSource.MARKETPLACE, job_application_id=application.id, base_pay=1, commission_per_unit=1,
    )
    db.session.add(assignment)
    db.session.commit()
    services.on_assignment_changed(assignment.id, change="terms", new_status="cancelled")
    assert reload(application).status == ApplicationStatus.ACCEPTED  # not a status change
    services.on_assignment_changed(assignment.id, change="status", new_status="confirmed")
    assert reload(application).status == ApplicationStatus.ACCEPTED
    services.on_assignment_changed(assignment.id, change="status", new_status="cancelled")
    db.session.commit()
    assert reload(application).status == ApplicationStatus.WITHDRAWN
    services.on_assignment_changed(assignment.id, change="status", new_status="cancelled")  # twice: no error
    services.on_assignment_changed(999999, change="status", new_status="cancelled")  # unknown id: no error


def test_the_bus_handler_ignores_assignments_that_did_not_come_from_an_application(world):
    assignment = Assignment(event=world.event, role=Skill.SELLER, user=world.staff, status=AssignmentStatus.CANCELLED,
                            base_pay=1, commission_per_unit=1)
    db.session.add(assignment)
    db.session.commit()
    services.on_assignment_changed(assignment.id, change="status", new_status="cancelled")  # nothing to do, no error


def test_the_handler_is_registered_on_the_real_bus(app):
    from app.services import eventbus

    assert services.on_assignment_changed in eventbus.subscribers("assignment.changed")


def test_check_can_apply_for_a_non_staff_user(world):
    with pytest.raises(services.MarketplaceError) as excinfo:
        services.check_can_apply(world.post, world.manager)
    assert excinfo.value.status == 403


def test_a_marketplace_error_carries_a_code_and_status():
    error = services.MarketplaceError("پیام", "some_code", 422)
    assert (str(error), error.code, error.status) == ("پیام", "some_code", 422)


def test_describe_staffing_error_covers_the_known_failures(world):
    assert "قفل" in services.describe_staffing_error(staffing.TermsLocked("x"))
    assert "همین نقش" in services.describe_staffing_error(staffing.DuplicateAssignment([]))
    assert "دیگری افیش" in services.describe_staffing_error(staffing.AssignmentConflict([]))
    assert "قابل افیش" in services.describe_staffing_error(staffing.EventNotStaffable("x"))
    assert services.describe_staffing_error(RuntimeError("x")) == "عملیات انجام نشد."


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------


def test_like_pattern_escapes_wildcards():
    assert queries.like_pattern("50%_off") == "%50\\%\\_off%"


def test_relations(world, make_assignment):
    manager, rival, hall, admin = world.manager, world.rival_manager, world.hall_manager, world.admin
    staff_id = world.staff.id
    assert queries.relation_to(manager, staff_id) == queries.Relation(False, False)
    make_assignment(world.event, "seller", user=world.staff, status="invited")
    assert queries.relation_to(manager, staff_id) == queries.Relation(True, False)
    assert queries.relation_to(rival, staff_id) == queries.Relation(False, False)
    assert queries.relation_to(hall, staff_id) == queries.Relation(True, False)  # an invitation at the hall's event connects
    assignment = db.session.scalar(sa.select(Assignment))
    assignment.status = AssignmentStatus.CONFIRMED
    db.session.commit()
    assert queries.relation_to(manager, staff_id).contact is True
    assert queries.relation_to(hall, staff_id).contact is True
    assert queries.relation_to(rival, staff_id).contact is False
    assert queries.relation_to(admin, staff_id) == queries.Relation(True, True)
    assert queries.relation_to(None, staff_id) == queries.Relation()
    assert queries.committed_partners(manager, [staff_id, world.other.id]) == {staff_id}
    assert queries.committed_partners(world.staff, [staff_id]) == set()  # staff are nobody's partner here


def test_get_visible_post_rules(world, make_user):
    assert queries.get_visible_post(world.post.id, None).id == world.post.id
    assert queries.get_visible_post(99999, None) is None
    world.post.status = JobPostStatus.HIDDEN
    db.session.commit()
    assert queries.get_visible_post(world.post.id, None) is None
    assert queries.get_visible_post(world.post.id, world.manager) is not None
    assert queries.get_visible_post(world.post.id, world.admin) is not None
    assert queries.get_visible_post(world.post.id, world.rival_manager) is None


def test_the_list_queries_compile_for_postgresql(app, world):
    """No SQLite-only SQL: every list statement compiles against the PostgreSQL dialect."""
    from sqlalchemy.dialects import postgresql

    today = dt.date(2025, 7, 10)
    statements = [
        queries.board_select(today, city="شیراز", role=Skill.PHOTOGRAPHER, date_from=today, date_to=today),
        queries.manage_select([1, 2], "closed", today),
        queries.inbox_select([1], "pending", 3),
        queries.my_applications_select(1, "accepted"),
        queries.invitations_select(1, today),
        queries.talent_select(skill=Skill.EDITOR, city="شیراز", min_rating=4, on_date=today, name="سا%", sort="experience"),
        queries.reviews_about_user(1),
        queries.reviews_by_user(1),
        queries.pending_reviews_select(world.manager),
        queries.pending_reviews_select(world.staff),
    ]
    for statement in statements:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "SELECT" in sql
    for sqlite_only in ("GROUP_CONCAT", "IFNULL(", "strftime", "PRAGMA", "rowid"):
        assert not any(sqlite_only.lower() in str(s.compile(dialect=postgresql.dialect())).lower() for s in statements)
