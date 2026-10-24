"""Tests for app.services.payroll - the pay formula of AGENTS.md.

    units      = sum(qty * weight) over lines that count
    commission = units * commission_per_unit
    bonus      = amount of the highest tier with threshold <= units (A1: not cumulative)
    total      = base + commission + bonus
"""
import dataclasses
import json
import random
from types import SimpleNamespace

import pytest

from app.services.payroll import (
    BonusTier,
    PayBreakdown,
    PayTerms,
    SaleLine,
    bonus_for,
    compute_event_payroll,
    compute_pay,
    terms_from_dict,
    terms_to_dict,
    total_payroll,
    units_from_lines,
)

# The deterministic test vector of AGENTS.md.
VECTOR = PayTerms(base=1_000_000, commission_per_unit=9_000, bonus_tiers=(BonusTier(70, 150_000),))


# --------------------------------------------------------------------------
# AGENTS.md test vector and the 69/70/71 boundary
# --------------------------------------------------------------------------

def test_agents_md_test_vector():
    pay = compute_pay(VECTOR, 80)
    assert isinstance(pay, PayBreakdown)
    assert pay.units == 80
    assert pay.base == 1_000_000
    assert pay.commission == 720_000
    assert pay.bonus == 150_000
    assert pay.total == 1_870_000
    assert pay.applied_tier == BonusTier(70, 150_000)


@pytest.mark.parametrize(
    "units, commission, bonus, total, tier_applied",
    [
        (0, 0, 0, 1_000_000, False),
        (1, 9_000, 0, 1_009_000, False),
        (69, 621_000, 0, 1_621_000, False),
        (70, 630_000, 150_000, 1_780_000, True),  # "70 and above" includes 70
        (71, 639_000, 150_000, 1_789_000, True),
        (80, 720_000, 150_000, 1_870_000, True),
        (1000, 9_000_000, 150_000, 10_150_000, True),
    ],
)
def test_single_tier_boundaries(units, commission, bonus, total, tier_applied):
    pay = compute_pay(VECTOR, units)
    assert pay.commission == commission
    assert pay.bonus == bonus
    assert pay.total == total
    assert (pay.applied_tier is not None) is tier_applied


# --------------------------------------------------------------------------
# bonus_for: highest tier wins, not cumulative, order irrelevant
# --------------------------------------------------------------------------

TIERS = [BonusTier(50, 50_000), BonusTier(70, 150_000), BonusTier(100, 300_000)]


@pytest.mark.parametrize(
    "units, amount, threshold",
    [
        (0, 0, None),
        (49, 0, None),
        (50, 50_000, 50),
        (69, 50_000, 50),
        (70, 150_000, 70),  # not 50_000 + 150_000 (A1: tiers do not add up)
        (99, 150_000, 70),
        (100, 300_000, 100),
        (101, 300_000, 100),
        (500, 300_000, 100),
    ],
)
@pytest.mark.parametrize("order", ["given", "reversed", "shuffled"])
def test_bonus_for_highest_tier_any_order(units, amount, threshold, order):
    tiers = list(TIERS)
    if order == "reversed":
        tiers.reverse()
    elif order == "shuffled":
        tiers = [tiers[1], tiers[2], tiers[0]]
    got_amount, got_tier = bonus_for(units, tiers)
    assert got_amount == amount
    if threshold is None:
        assert got_tier is None
    else:
        assert got_tier.threshold == threshold
        assert got_tier.amount == amount


def test_bonus_for_without_tiers():
    assert bonus_for(500, []) == (0, None)
    assert bonus_for(500, ()) == (0, None)
    assert bonus_for(500, None) == (0, None)


@pytest.mark.parametrize(
    "tiers, units, expected_amount",
    [
        # Same threshold twice: the larger amount wins, whatever the order.
        ([BonusTier(70, 100_000), BonusTier(70, 150_000)], 70, 150_000),
        ([BonusTier(70, 150_000), BonusTier(70, 100_000)], 70, 150_000),
        # Exact duplicates are harmless.
        ([BonusTier(70, 150_000)] * 3, 80, 150_000),
        ([BonusTier(70, 150_000)] * 3, 69, 0),
        # A tier with amount 0 is still "reached".
        ([BonusTier(10, 0)], 10, 0),
        # A higher threshold with a lower amount still wins (highest tier reached).
        ([BonusTier(50, 200_000), BonusTier(70, 100_000)], 70, 100_000),
        # Threshold 0 is reached with no sales at all.
        ([BonusTier(0, 5_000)], 0, 5_000),
    ],
)
def test_bonus_for_edge_tiers(tiers, units, expected_amount):
    assert bonus_for(units, tiers)[0] == expected_amount


def test_bonus_for_returns_the_applied_tier_object():
    amount, tier = bonus_for(75, TIERS)
    assert tier == BonusTier(70, 150_000)
    assert amount == tier.amount


def test_bonus_for_accepts_dict_and_pair_tiers():
    assert bonus_for(80, [{"threshold": 70, "amount": 150_000}])[0] == 150_000
    assert bonus_for(80, [(70, 150_000), (90, 1)])[0] == 150_000


# --------------------------------------------------------------------------
# units_from_lines
# --------------------------------------------------------------------------

def test_units_from_sale_lines_and_weights():
    lines = [SaleLine(50), SaleLine(20), SaleLine(5, weight=2)]
    assert units_from_lines(lines) == 80
    assert units_from_lines([SaleLine(10, weight=3), SaleLine(5)]) == 35


@pytest.mark.parametrize(
    "lines, expected",
    [
        ([], 0),
        (None, 0),
        ([SaleLine(0)], 0),
        ([SaleLine(7)], 7),
        ([SaleLine(4, weight=0)], 0),  # weight can be lowered to 0
        ([SaleLine(4, weight=5)], 20),
        ([SaleLine(10, counts=False)], 0),
        ([SaleLine(10, counts=False), SaleLine(3)], 3),
        ([SaleLine(100, weight=9, counts=False)], 0),
    ],
)
def test_units_from_lines_table(lines, expected):
    assert units_from_lines(lines) == expected


def test_units_from_dict_lines():
    lines = [
        {"qty": 10, "weight": 3},
        {"qty": 5},  # weight defaults to 1
        {"qty": 99, "counts": False},
        {"qty": 2, "weight": None, "counts": None},  # None means "use the default"
        {"qty": 4, "counts_for_commission": True},
        {"qty": 50, "counts_for_commission": False},
    ]
    assert units_from_lines(lines) == 10 * 3 + 5 + 2 + 4


def test_units_from_object_lines():
    class Row:  # stands in for an ORM model
        def __init__(self, qty, weight=1, counts_for_commission=True):
            self.qty = qty
            self.weight = weight
            self.counts_for_commission = counts_for_commission

    lines = [
        Row(10),
        Row(5, weight=2),
        Row(7, counts_for_commission=False),
        SimpleNamespace(qty=3, weight=1, counts=True),
        SimpleNamespace(qty=4),
        SimpleNamespace(qty=9, counts=False),
    ]
    assert units_from_lines(lines) == 10 + 10 + 3 + 4


def test_units_from_mixed_line_types():
    lines = [SaleLine(1), {"qty": 2}, SimpleNamespace(qty=3, weight=2)]
    assert units_from_lines(lines) == 1 + 2 + 6


def test_units_from_generator_input():
    assert units_from_lines(SaleLine(q) for q in (1, 2, 3)) == 6


def test_non_counting_lines_are_not_validated():
    # A flagged-out line never affects pay, even if its data is odd.
    assert units_from_lines([{"qty": -5, "counts": False}, {"qty": 2}]) == 2


@pytest.mark.parametrize(
    "bad_line",
    [
        {"weight": 2},  # no qty
        {"qty": None},
        {"qty": -1},
        {"qty": 1.5},
        {"qty": "3"},
        {"qty": True},
        {"qty": 2, "weight": -1},
        {"qty": 2, "weight": 1.5},
        {"qty": 2, "counts": "yes"},
        {"qty": 2, "counts": 2},
        object(),
        42,
        "5",
    ],
)
def test_units_from_lines_rejects_bad_lines(bad_line):
    with pytest.raises(ValueError):
        units_from_lines([bad_line])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"qty": -1},
        {"qty": 1.0},
        {"qty": "2"},
        {"qty": True},
        {"qty": None},
        {"qty": 1, "weight": -1},
        {"qty": 1, "weight": 2.5},
        {"qty": 1, "counts": "no"},
    ],
)
def test_sale_line_validation(kwargs):
    with pytest.raises(ValueError):
        SaleLine(**kwargs)


def test_sale_line_defaults():
    line = SaleLine(3)
    assert (line.qty, line.weight, line.counts) == (3, 1, True)


# --------------------------------------------------------------------------
# compute_pay
# --------------------------------------------------------------------------

def test_no_tiers_means_no_bonus():
    pay = compute_pay(PayTerms(500_000, 4_000), 100)
    assert pay.bonus == 0
    assert pay.applied_tier is None
    assert pay.total == 500_000 + 400_000


def test_zero_everything():
    pay = compute_pay(PayTerms(0, 0), 0)
    assert pay.total == 0 and pay.commission == 0 and pay.bonus == 0


def test_base_only_person():
    pay = compute_pay(PayTerms(2_000_000, 0, (BonusTier(10, 100_000),)), 25)
    assert (pay.commission, pay.bonus, pay.total) == (0, 100_000, 2_100_000)


def test_large_numbers_stay_exact_integers():
    terms = PayTerms(
        base=10**12,
        commission_per_unit=10**9,
        bonus_tiers=(BonusTier(10**6, 10**13),),
    )
    pay = compute_pay(terms, 10**6)
    assert pay.commission == 10**15
    assert pay.bonus == 10**13
    assert pay.total == 10**12 + 10**15 + 10**13
    assert isinstance(pay.total, int)


def test_very_large_values_do_not_lose_precision():
    big = 10**30 + 1
    pay = compute_pay(PayTerms(big, 3), big)
    assert pay.total == big + 3 * big


def test_compute_pay_accepts_snapshot_dict():
    snap = {"base": 1_000_000, "commission_per_unit": 9_000,
            "bonus_tiers": [{"threshold": 70, "amount": 150_000}]}
    assert compute_pay(snap, 80).total == 1_870_000


def test_pay_breakdown_as_dict():
    assert compute_pay(VECTOR, 80).as_dict() == {
        "units": 80,
        "base": 1_000_000,
        "commission": 720_000,
        "bonus": 150_000,
        "total": 1_870_000,
        "applied_tier": {"threshold": 70, "amount": 150_000},
    }
    assert compute_pay(VECTOR, 10).as_dict()["applied_tier"] is None
    json.dumps(compute_pay(VECTOR, 80).as_dict())  # JSON friendly


def test_breakdown_is_internally_consistent():
    pay = compute_pay(VECTOR, 73)
    assert pay.total == pay.base + pay.commission + pay.bonus


def test_dataclasses_are_frozen():
    pay = compute_pay(VECTOR, 80)
    for obj, attr in [(pay, "total"), (VECTOR, "base"), (BonusTier(1, 1), "amount"), (SaleLine(1), "qty")]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, 0)


def test_pay_terms_normalise_tiers_to_a_tuple():
    terms = PayTerms(1, 2, [BonusTier(5, 6), {"threshold": 7, "amount": 8}, (9, 10)])
    assert terms.bonus_tiers == (BonusTier(5, 6), BonusTier(7, 8), BonusTier(9, 10))
    assert PayTerms(1, 2, None).bonus_tiers == ()
    assert PayTerms(1, 2).bonus_tiers == ()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PayTerms(-1, 0),
        lambda: PayTerms(0, -1),
        lambda: PayTerms(1.5, 0),
        lambda: PayTerms(0, 2.5),
        lambda: PayTerms("100", 0),
        lambda: PayTerms(True, 0),
        lambda: PayTerms(None, 0),
        lambda: PayTerms(0, 0, (BonusTier(-1, 5),)),
        lambda: PayTerms(0, 0, (BonusTier(5, -1),)),
        lambda: PayTerms(0, 0, "tiers"),
        lambda: PayTerms(0, 0, [object()]),
        lambda: PayTerms(0, 0, [{"threshold": 5}]),
        lambda: BonusTier(1.5, 0),
        lambda: BonusTier(0, 1.5),
        lambda: BonusTier(True, 0),
        lambda: BonusTier("70", 0),
    ],
)
def test_invalid_terms_are_rejected(factory):
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize("units", [-1, 1.5, 70.0, "70", None, True])
def test_invalid_units_are_rejected(units):
    with pytest.raises(ValueError):
        compute_pay(VECTOR, units)
    with pytest.raises(ValueError):
        bonus_for(units, TIERS)


def test_compute_pay_rejects_unreadable_terms():
    with pytest.raises(ValueError):
        compute_pay("terms", 5)
    with pytest.raises(ValueError):
        compute_pay(None, 5)


# --------------------------------------------------------------------------
# Snapshot (JSON) round trip
# --------------------------------------------------------------------------

def test_terms_to_dict_shape():
    assert terms_to_dict(VECTOR) == {
        "base": 1_000_000,
        "commission_per_unit": 9_000,
        "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
    }
    assert terms_to_dict(PayTerms(1, 2)) == {"base": 1, "commission_per_unit": 2, "bonus_tiers": []}


@pytest.mark.parametrize(
    "terms",
    [
        PayTerms(0, 0),
        VECTOR,
        PayTerms(1_000_000, 9_000, (BonusTier(100, 300_000), BonusTier(50, 50_000), BonusTier(70, 150_000))),
        PayTerms(10**12, 10**9, (BonusTier(10**6, 10**13),)),
    ],
)
def test_terms_snapshot_survives_json(terms):
    wire = json.dumps(terms_to_dict(terms))
    restored = terms_from_dict(json.loads(wire))
    assert restored == terms
    assert compute_pay(restored, 80) == compute_pay(terms, 80)


def test_terms_snapshot_keeps_tier_order():
    terms = PayTerms(0, 0, (BonusTier(100, 3), BonusTier(50, 1), BonusTier(70, 2)))
    assert [t["threshold"] for t in terms_to_dict(terms)["bonus_tiers"]] == [100, 50, 70]


def test_terms_from_dict_without_tiers():
    assert terms_from_dict({"base": 5, "commission_per_unit": 6}) == PayTerms(5, 6)
    assert terms_from_dict({"base": 5, "commission_per_unit": 6, "bonus_tiers": None}) == PayTerms(5, 6)


def test_terms_from_dict_ignores_unknown_keys():
    assert terms_from_dict({"base": 1, "commission_per_unit": 2, "note": "x"}) == PayTerms(1, 2)


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"base": 1},
        {"commission_per_unit": 1},
        {"base": "1", "commission_per_unit": 1},
        {"base": 1.5, "commission_per_unit": 1},
        {"base": -1, "commission_per_unit": 1},
        {"base": 1, "commission_per_unit": 1, "bonus_tiers": [{"threshold": 1}]},
        {"base": 1, "commission_per_unit": 1, "bonus_tiers": [{"threshold": -1, "amount": 1}]},
        None,
        "base=1",
        [1, 2],
    ],
)
def test_terms_from_dict_rejects_bad_snapshots(snapshot):
    with pytest.raises(ValueError):
        terms_from_dict(snapshot)


def test_old_snapshot_is_not_changed_by_new_agreement():
    """Snapshots are plain data: a later raise does not rewrite history."""
    old = terms_to_dict(PayTerms(1_000_000, 9_000))
    new_terms = PayTerms(1_000_000, 12_000)
    assert compute_pay(old, 10).commission == 90_000
    assert compute_pay(new_terms, 10).commission == 120_000


# --------------------------------------------------------------------------
# compute_event_payroll
# --------------------------------------------------------------------------

def test_event_payroll_everyone_gets_the_vector_amount():
    people = {role: VECTOR for role in ("photographer", "editor", "presenter", "seller")}
    lines = [SaleLine(50), SaleLine(20), SaleLine(5, weight=2), SaleLine(100, counts=False)]
    payroll = compute_event_payroll(people, lines)
    assert list(payroll) == ["photographer", "editor", "presenter", "seller"]
    assert all(p.units == 80 for p in payroll.values())
    assert all(p.total == 1_870_000 for p in payroll.values())
    assert total_payroll(payroll) == 4 * 1_870_000


def test_event_payroll_individual_terms():
    people = {
        1: PayTerms(1_000_000, 9_000, (BonusTier(70, 150_000),)),
        2: PayTerms(800_000, 5_000),
        3: PayTerms(0, 12_000, (BonusTier(50, 10_000), BonusTier(90, 99_000))),
        "freelancer-x": {"base": 300_000, "commission_per_unit": 1_000, "bonus_tiers": []},
    }
    payroll = compute_event_payroll(people, [{"qty": 60}, {"qty": 20, "weight": 1}])
    assert payroll[1].total == 1_870_000
    assert payroll[2].total == 800_000 + 400_000
    assert payroll[3].total == 80 * 12_000 + 10_000
    assert payroll["freelancer-x"].total == 300_000 + 80_000
    assert total_payroll(payroll) == sum(p.total for p in payroll.values())


def test_event_payroll_with_no_sales_pays_only_base():
    people = {"a": PayTerms(500_000, 9_000, (BonusTier(10, 1),))}
    assert compute_event_payroll(people, [])["a"].total == 500_000


def test_event_payroll_with_no_people():
    assert compute_event_payroll({}, [SaleLine(5)]) == {}
    assert total_payroll({}) == 0


def test_event_payroll_propagates_invalid_data():
    with pytest.raises(ValueError):
        compute_event_payroll({"a": VECTOR}, [{"qty": -1}])
    with pytest.raises(ValueError):
        compute_event_payroll({"a": "not terms"}, [SaleLine(1)])


# --------------------------------------------------------------------------
# Property check against an independent oracle
# --------------------------------------------------------------------------

def _oracle(base, rate, tiers, qtys_weights):
    units = sum(q * w for q, w in qtys_weights)
    bonus = 0
    for threshold, amount in sorted(tiers, reverse=True):  # highest threshold first
        if threshold <= units:
            bonus = amount
            break
    return units, base + units * rate + bonus


def test_random_cases_match_oracle():
    rng = random.Random(20240802)
    for _ in range(300):
        base = rng.randrange(0, 5_000_000)
        rate = rng.randrange(0, 20_000)
        tiers = [(rng.randrange(0, 150), rng.randrange(0, 500_000)) for _ in range(rng.randrange(0, 5))]
        lines = [(rng.randrange(0, 40), rng.randrange(0, 4)) for _ in range(rng.randrange(0, 8))]
        terms = PayTerms(base, rate, tuple(BonusTier(t, a) for t, a in tiers))
        sale_lines = [SaleLine(q, w) for q, w in lines]
        units, expected = _oracle(base, rate, tiers, lines)
        pay = compute_pay(terms, units_from_lines(sale_lines))
        assert pay.units == units
        assert pay.total == expected
