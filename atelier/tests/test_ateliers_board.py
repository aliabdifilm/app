"""Read side of the board (completeness, busy index, directory, prefill, copy plan) and the form helpers."""

from __future__ import annotations


import pytest
from test_ateliers_support import BIG_TERMS, body, day, mc, now, world  # noqa: F401
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import StaffAvailability
from app.modules.ateliers import board as boards
from app.modules.ateliers import forms, links
from app.modules.ateliers.common import terms_text
from app.services import staffing
from app.services.constants import AssignmentStatus as S
from app.services.constants import Role, Skill
from app.services.forms import Form

# --------------------------------------------------------------------------
# completeness: same numbers as staffing.staffing_summary
# --------------------------------------------------------------------------


def _states(world, make_roster_member, make_user, plan):
    """Create assignments from ``[(role, status)]`` for people made on the fly."""
    created = []
    for role, status in plan:
        member = make_roster_member(world.atelier, display_name=f"{role}-{status}-{len(created)}", skills=(Skill.coerce(role),))
        assignment = staffing.assign_person(world.event, role, roster_member=member, terms=BIG_TERMS, actor=world.manager)
        if status != "confirmed":
            assignment.status = S(status)
        created.append(assignment)
    db.session.commit()


@pytest.mark.parametrize(
    "plan",
    [
        [],
        [("photographer", "confirmed")],
        [("photographer", "confirmed"), ("photographer", "invited"), ("photographer", "declined")],
        [("photographer", "accepted"), ("seller", "invited"), ("editor", "cancelled"), ("presenter", "completed")],
        [("editor", "invited")] * 1 + [("editor", "accepted")] * 1,  # surplus over the head-count of one
    ],
)
def test_batch_summary_matches_the_service(world, make_roster_member, make_user, plan):
    _states(world, make_roster_member, make_user, plan)
    reference = staffing.staffing_summary(world.event)
    board = boards.load_board(world.event)
    for role, slots in reference.roles.items():
        mine = board.summary.roles[role]
        assert (mine.needed, mine.filled, mine.invited, mine.missing, mine.shortfall, mine.surplus) == (
            slots.needed,
            slots.filled,
            slots.invited,
            slots.missing,
            slots.shortfall,
            slots.surplus,
        )
    c = board.completeness
    assert c.filled == reference.filled_total and c.missing == reference.missing_total
    assert c.needed == reference.needed_total
    assert c.filled + min(c.invited, c.needed - c.filled) + c.missing >= 0
    assert len(c.pips()) == c.needed  # one pip per slot, never more


def test_completeness_tone_and_percent():
    full = boards.Completeness(needed=4, filled=4, invited=0, missing=0)
    waiting = boards.Completeness(needed=4, filled=2, invited=2, missing=0)
    gaps = boards.Completeness(needed=4, filled=1, invited=1, missing=2)
    none = boards.Completeness(needed=0, filled=0, invited=0, missing=0)
    assert [c.tone for c in (full, waiting, gaps, none)] == ["success", "warning", "danger", "success"]
    assert [c.percent for c in (full, waiting, gaps, none)] == [100, 50, 25, 100]
    assert full.is_complete and not waiting.is_complete


def test_load_boards_handles_no_events():
    assert boards.load_boards([]) == {}


# --------------------------------------------------------------------------
# busy index agrees with staffing.conflicts_for
# --------------------------------------------------------------------------


def _agree(world, member=None, user=None, expected=None):
    index = boards.busy_index(world.event.date, exclude_event_id=world.event.id)
    reference = bool(
        staffing.conflicts_for(
            world.event.date,
            user_id=user.id if user else None,
            roster_member_id=member.id if member else None,
            exclude_event_id=world.event.id,
        )
    )
    mine = index.member_busy(member) if member is not None else index.user_busy(user)
    assert mine == reference
    if expected is not None:
        assert mine == expected


def test_busy_index_when_nothing_is_booked(world):
    _agree(world, member=world.member, expected=False)
    _agree(world, user=world.staff, expected=False)


def test_busy_index_for_the_members_own_assignment_elsewhere_that_day(world):
    world.later.date = world.event.date
    db.session.commit()
    staffing.assign_person(world.later, "editor", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    _agree(world, member=world.member, expected=True)


def test_busy_index_through_the_linked_account(world):
    world.member.user = world.staff
    db.session.commit()
    staffing.assign_person(world.rival_event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.rival_manager)
    db.session.commit()
    _agree(world, member=world.member, expected=True)
    _agree(world, user=world.staff, expected=True)


def test_busy_index_through_the_same_mobile_number(world, make_roster_member):
    twin = make_roster_member(world.rival, display_name="همان نفر", mobile=world.staff.mobile)
    staffing.assign_person(world.rival_event, "photographer", roster_member=twin, terms=BIG_TERMS, actor=world.rival_manager)
    db.session.commit()
    _agree(world, user=world.staff, expected=True)


def test_busy_index_ignores_cancelled_and_declined_rows(world):
    a = staffing.assign_person(world.rival_event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.rival_manager)
    a.status = S.DECLINED
    db.session.commit()
    _agree(world, user=world.staff, expected=False)
    a.status = S.INVITED
    db.session.commit()
    _agree(world, user=world.staff, expected=True)
    staffing.change_event_status(world.rival_event, "cancelled", world.rival_manager)
    db.session.commit()
    _agree(world, user=world.staff, expected=False)


def test_unavailable_user_ids(world):
    db.session.add(StaffAvailability(user_id=world.staff.id, date=world.event.date))
    db.session.commit()
    assert boards.unavailable_user_ids([world.staff.id, None], world.event.date) == {world.staff.id}
    assert boards.unavailable_user_ids([world.staff.id], world.later.date) == set()
    assert boards.unavailable_user_ids([], world.event.date) == set()


def test_describe_conflicts_hides_other_ateliers(world):
    mine = staffing.assign_person(world.later, "editor", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    theirs = staffing.assign_person(world.rival_event, "photographer", user=world.staff, terms=BIG_TERMS, actor=world.rival_manager)
    db.session.commit()
    rows = boards.describe_conflicts([mine, theirs], [world.atelier.id])
    assert rows[0]["own"] is True and rows[0]["hall"] == "تالار گل‌ها" and rows[0]["role"] == "ادیتور"
    assert rows[1] == {"own": False}


# --------------------------------------------------------------------------
# directory
# --------------------------------------------------------------------------


@pytest.fixture
def crowd(world, make_user, make_staff_profile):
    people = {}
    for key, name, city, skills, listed, status in [
        ("a", "علی عکاس", "شیراز", ("photographer",), True, "active"),
        ("b", "بهمن ادیتور", "تهران", ("editor",), True, "active"),
        ("hidden", "پنهان", "شیراز", ("photographer",), False, "active"),
        ("susp", "معلق", "شیراز", ("photographer",), True, "suspended"),
        ("pending", "در انتظار", "شیراز", ("photographer",), True, "pending"),
    ]:
        user = make_user(Role.STAFF, full_name=name, status=status, city=city)
        make_staff_profile(user, skills=tuple(Skill.coerce(s) for s in skills), is_listed=listed, city=city)
        people[key] = user
    people["boss"] = make_user(Role.HALL_MANAGER, full_name="مدیر")
    return people


def names(hits):
    return sorted(hit.user.full_name for hit in hits)


def test_directory_lists_only_listed_active_staff(world, crowd):
    found = names(boards.search_directory(world.manager.id, None, ""))
    assert found == sorted(["علی عکاس", "بهمن ادیتور", "سارا نیرو"])


def test_directory_filters_by_skill_and_text(world, crowd):
    photographers = names(boards.search_directory(world.manager.id, Skill.PHOTOGRAPHER, ""))
    assert photographers == sorted(["علی عکاس", "سارا نیرو"])
    assert names(boards.search_directory(world.manager.id, None, "تهران")) == ["بهمن ادیتور"]
    assert names(boards.search_directory(world.manager.id, None, "ادیتور")) == ["بهمن ادیتور"]


def test_directory_search_treats_wildcards_literally(world, crowd):
    assert boards.search_directory(world.manager.id, None, "%") == []
    assert boards.search_directory(world.manager.id, None, "_") == []


def test_exact_mobile_finds_unlisted_staff_only_with_a_token(world, crowd):
    (hit,) = boards.search_directory(world.manager.id, None, crowd["hidden"].mobile.replace("09", "+989", 1))
    assert hit.user.id == crowd["hidden"].id and not hit.listed and hit.token
    assert boards.directory_user(world.manager.id, crowd["hidden"].id, hit.token).id == crowd["hidden"].id
    assert boards.directory_user(world.manager.id, crowd["hidden"].id) is None
    assert boards.directory_user(world.manager.id, crowd["hidden"].id, "garbage") is None
    assert boards.directory_user(world.rival_manager.id, crowd["hidden"].id, hit.token) is None  # bound to the manager
    assert boards.directory_user(world.manager.id, "x") is None


def test_token_expires(world, crowd, monkeypatch):
    token = boards.directory_token(world.manager.id, crowd["hidden"].id)
    monkeypatch.setattr(boards, "TOKEN_MAX_AGE", -1)
    assert boards.directory_user(world.manager.id, crowd["hidden"].id, token) is None


def test_exact_mobile_never_returns_suspended_or_non_staff_accounts(world, crowd):
    assert boards.search_directory(world.manager.id, None, crowd["susp"].mobile) == []
    assert boards.search_directory(world.manager.id, None, crowd["boss"].mobile) == []
    assert boards.directory_user(world.manager.id, crowd["susp"].id) is None
    assert boards.directory_user(world.manager.id, crowd["boss"].id) is None
    assert boards.directory_user(world.manager.id, crowd["a"].id).id == crowd["a"].id  # listed: no token needed


# --------------------------------------------------------------------------
# terms prefill, copy plan, share text
# --------------------------------------------------------------------------


def test_prefill_order_previous_member_preset_profile_none(world, make_user, make_staff_profile):
    atelier, member = world.atelier, world.member
    photographer, editor = Skill.PHOTOGRAPHER, Skill.EDITOR
    terms, source = boards.prefill_terms(atelier, photographer)
    assert source == "preset" and terms == BIG_TERMS

    member.default_terms = {"photographer": {"base": 1, "commission_per_unit": 2, "bonus_tiers": []}}
    terms, source = boards.prefill_terms(atelier, photographer, member=member)
    assert source == "member" and terms["base"] == 1

    previous = staffing.assign_person(world.event, "photographer", roster_member=member, terms=BIG_TERMS, actor=world.manager)
    terms, source = boards.prefill_terms(atelier, photographer, member=member, previous=previous)
    assert source == "previous" and terms == BIG_TERMS

    world.staff.staff_profile.expected_base = 750_000
    terms, source = boards.prefill_terms(atelier, editor, user=world.staff)
    assert source == "profile" and terms["base"] == 750_000 and terms["bonus_tiers"] == []

    terms, source = boards.prefill_terms(atelier, editor)
    assert source == "none" and terms == {"base": 0, "commission_per_unit": 0, "bonus_tiers": []}
    # the returned dict is a copy: editing it must not change the atelier's preset
    preset, _ = boards.prefill_terms(atelier, photographer)
    preset["bonus_tiers"].append({"threshold": 1, "amount": 1})
    assert atelier.preset_for(photographer)["bonus_tiers"] == BIG_TERMS["bonus_tiers"]


def test_copy_plan_flags_busy_duplicate_and_inactive_people(world, make_roster_member):
    other = make_roster_member(world.atelier, display_name="دومی", skills=(Skill.EDITOR,))
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    staffing.assign_person(world.event, "editor", roster_member=other, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    plan = {row.assignment.person_name: row for row in boards.copy_plan(world.event, world.later)}
    assert plan["رضا عکاس"].copyable and plan["دومی"].copyable
    other.is_active = False
    world.later.date = world.event.date  # now a clash for the photographer as well
    db.session.commit()
    plan = {row.assignment.person_name: row for row in boards.copy_plan(world.event, world.later)}
    assert "تداخل" in plan["رضا عکاس"].skip_reason or "مراسم دیگری" in plan["رضا عکاس"].skip_reason
    assert "غیرفعال" in plan["دومی"].skip_reason


def test_share_text_lists_roles_without_pay_or_phone_numbers(world):
    staffing.assign_person(world.event, "photographer", roster_member=world.member, terms=BIG_TERMS, actor=world.manager)
    db.session.commit()
    text = boards.share_text(world.event, boards.load_board(world.event))
    assert "اتلیه ستاره" in text and "تالار گل‌ها" in text and "عکاس: رضا عکاس" in text and "پرزنتور: —" in text
    assert "1000000" not in text and "۱٬۰۰۰٬۰۰۰" not in text and "09" not in text


# --------------------------------------------------------------------------
# form helpers
# --------------------------------------------------------------------------


def make_form(**fields):
    return Form(MultiDict(fields))


def test_read_terms_happy_path_with_persian_digits_and_sorting():
    form = make_form(**{"base": "۱٬۰۰۰٬۰۰۰", "commission": "۹۰۰۰"})
    form.data.setlist("tier_threshold[]", ["۱۰۰", "۷۰", "", ""])
    form.data.setlist("tier_amount[]", ["۳۰۰٬۰۰۰", "۱۵۰٬۰۰۰", "", ""])
    terms = forms.read_terms(form)
    assert form.ok and terms == {
        "base": 1_000_000,
        "commission_per_unit": 9_000,
        "bonus_tiers": [{"threshold": 70, "amount": 150_000}, {"threshold": 100, "amount": 300_000}],
    }
    assert all(isinstance(v, int) for v in (terms["base"], terms["commission_per_unit"]))


@pytest.mark.parametrize(
    "thresholds, amounts",
    [
        (["70"], [""]),
        ([""], ["100"]),
        (["abc"], ["100"]),
        (["0"], ["100"]),
        (["70", "70"], ["1", "2"]),
        (["70"], ["1.5"]),
        (["999999999"], ["1"]),
    ],
)
def test_read_terms_rejects_bad_tier_rows(thresholds, amounts):
    form = make_form(base="1", commission="1")
    form.data.setlist("tier_threshold[]", thresholds)
    form.data.setlist("tier_amount[]", amounts)
    assert forms.read_terms(form) is None
    assert "tiers" in form.errors
    assert form.values["tiers"]  # the rows are kept for redisplay


def test_read_terms_defaults_to_zero_and_labels_errors_with_the_role():
    assert forms.read_terms(make_form()) == {"base": 0, "commission_per_unit": 0, "bonus_tiers": []}
    form = make_form(**{"base__seller": "x"})
    assert forms.read_terms(form, "__seller", "فروشنده") is None
    assert form.errors["base__seller"].startswith("فروشنده:")


def test_terms_empty_and_to_form():
    assert forms.terms_are_empty(None) and forms.terms_are_empty({"base": 0, "commission_per_unit": 0, "bonus_tiers": []})
    assert not forms.terms_are_empty({"base": 1, "commission_per_unit": 0, "bonus_tiers": []})
    values = forms.terms_to_form(BIG_TERMS, "__seller")
    assert values["base__seller"] == 1_000_000 and values["tiers__seller"] == [{"threshold": 70, "amount": 150_000}]
    assert forms.terms_to_form({"base": 0, "commission_per_unit": 0, "bonus_tiers": []}, blank_zero=True)["base"] == ""


def test_read_skills_and_ids():
    form = make_form()
    form.data.setlist("skills", ["seller", "photographer", "seller"])
    assert forms.read_skills(form) == ["photographer", "seller"]  # canonical order, no duplicates
    bad = make_form()
    bad.data.setlist("skills", ["pilot"])
    assert forms.read_skills(bad) == [] and "skills" in bad.errors
    assert forms.read_id(make_form(x="۱۲"), "x") == 12
    assert forms.read_id(make_form(x="-1"), "x") is None and forms.read_id(make_form(x="0"), "x") is None
    assert forms.read_id(make_form(), "x") is None


def test_terms_text_is_persian_and_integer_only():
    assert terms_text(BIG_TERMS) == "پایه ۱٬۰۰۰٬۰۰۰ تومان · پورسانت هر عکس ۹٬۰۰۰ تومان · پاداش ۷۰ عکس به بالا ۱۵۰٬۰۰۰ تومان"
    assert terms_text({"base": 0, "commission_per_unit": 0, "bonus_tiers": []}) == "بدون مبلغ تعیین‌شده"
    assert terms_text(None) == "بدون مبلغ تعیین‌شده"


# --------------------------------------------------------------------------
# links to other modules degrade gracefully
# --------------------------------------------------------------------------


def test_links_are_none_when_the_other_modules_are_missing(world, app):
    with app.test_request_context():
        assert links.inventory_url() is None or links.inventory_url().startswith("/inventory")
        assert links.route_exists("/definitely/not/a/route") is False
        assert links.route_exists("/atelier/") is True
        url = links.marketplace_job_url(world.event, Skill.SELLER, BIG_TERMS, slots=2)
        assert url is None or "role=seller" in url


def test_tiers_query_format():
    assert links.tiers_query([{"threshold": 70, "amount": 150000}, {"threshold": 100, "amount": 3}]) == "70:150000,100:3"


# --------------------------------------------------------------------------
# the example endpoint uses payroll.compute_pay (AGENTS.md vector: 80 photos -> 1,870,000)
# --------------------------------------------------------------------------


def test_pay_preview_matches_the_agents_md_vector(world, mc):
    response = mc.post("/atelier/api/pay-preview", json={**{"base": 1_000_000, "commission_per_unit": 9_000, "bonus_tiers": [{"threshold": 70, "amount": 150_000}]}, "units": 80})
    data = response.get_json()
    assert response.status_code == 200 and data["ok"] is True
    assert data["data"]["total"] == 1_870_000 and data["data"]["commission"] == 720_000 and data["data"]["bonus"] == 150_000
    below = mc.post("/atelier/api/pay-preview", json={"base": 1_000_000, "commission_per_unit": 9_000, "bonus_tiers": [{"threshold": 70, "amount": 150_000}], "units": 69})
    assert below.get_json()["data"]["bonus"] == 0  # 70 and above means >= 70
    exact = mc.post("/atelier/api/pay-preview", json={"base": 0, "commission_per_unit": 0, "bonus_tiers": [{"threshold": 70, "amount": 5}], "units": 70})
    assert exact.get_json()["data"]["bonus"] == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"base": 1.5},
        {"base": -1},
        {"base": "abc"},
        {"base": 1, "units": -1},
        {"base": 1, "units": 10**9},
        {"base": 1, "bonus_tiers": "x"},
        {"base": 1, "bonus_tiers": [{"threshold": 70}]},
        {"base": True},
    ],
)
def test_pay_preview_rejects_bad_input_with_json_errors(world, mc, payload):
    response = mc.post("/atelier/api/pay-preview", json=payload)
    assert response.status_code == 422
    assert response.get_json()["ok"] is False and response.get_json()["error"]
