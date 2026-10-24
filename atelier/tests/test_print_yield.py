"""Tests for app.services.print_yield (sheets and paper packs, assumption A4)."""
import copy
import json
import random

import pytest

from app.services.print_yield import (
    DEFAULT_YIELD,
    PrintPlan,
    default_yield_table,
    normalize_paper,
    normalize_size,
    packs_needed,
    plan_for_sales,
    sheet_yield,
    sheets_needed,
)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def test_default_yield_matches_assumption_a4():
    assert DEFAULT_YIELD == {
        "A4": {"16x21": 2, "20x30": 1},
        "A3": {"16x21": 4, "20x30": 2, "30x40": 1},
    }


def test_default_yield_table_is_an_independent_copy():
    table = default_yield_table()
    assert table == DEFAULT_YIELD
    table["A4"]["16x21"] = 99
    assert DEFAULT_YIELD["A4"]["16x21"] == 2


# --------------------------------------------------------------------------
# normalize_size / normalize_paper
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("16x21", "16x21"),
        ("۱۶×۲۱", "16x21"),  # Persian digits and the multiplication sign
        ("١٦×٢١", "16x21"),  # Arabic-Indic digits
        ("16*21", "16x21"),
        ("16 x 21", "16x21"),
        ("16 × 21", "16x21"),
        ("16X21", "16x21"),
        ("21x16", "16x21"),  # short side first
        ("۲۱×۱۶", "16x21"),
        ("21 * 16", "16x21"),
        ("20x30", "20x30"),
        ("۳۰×۲۰", "20x30"),
        ("30x40", "30x40"),
        ("40*30", "30x40"),
        ("  20 x 30  ", "20x30"),
        ("‏۲۰×۳۰‎", "20x30"),
        ("20.0x30", "20x30"),
        ("12.5x18", "12.5x18"),
        ("۱۸×۱۲٫۵", "12.5x18"),  # Arabic decimal separator
        ("10.50x15", "10.5x15"),
        ("16x16", "16x16"),
        ("100x70", "70x100"),
    ],
)
def test_normalize_size(text, expected):
    assert normalize_size(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "16", "16x", "x21", "16x21x3", "0x10", "16x0", "16-21", "16 21",
     "A4", "16x21cm", "-16x21", "1e1x2", "16..5x21", "5000x30", None, 1621, 16.21, ("16", "21")],
)
def test_normalize_size_rejects_garbage(text):
    with pytest.raises(ValueError):
        normalize_size(text)


@pytest.mark.parametrize(
    "text, expected",
    [("A4", "A4"), ("a4", "A4"), (" a3 ", "A3"), ("A 4", "A4"), ("b5", "B5")],
)
def test_normalize_paper(text, expected):
    assert normalize_paper(text) == expected


@pytest.mark.parametrize("bad", ["", "  ", None, 4])
def test_normalize_paper_rejects_garbage(bad):
    with pytest.raises(ValueError):
        normalize_paper(bad)


# --------------------------------------------------------------------------
# sheet_yield
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "size, paper, expected",
    [
        ("16x21", "A4", 2),
        ("20x30", "A4", 1),
        ("30x40", "A4", 0),  # does not fit
        ("16x21", "A3", 4),
        ("20x30", "A3", 2),
        ("30x40", "A3", 1),
        ("۲۱×۱۶", "a4", 2),
        ("13x18", "A4", 0),  # unknown custom size
        ("16x21", "A5", 0),  # unknown paper
    ],
)
def test_sheet_yield(size, paper, expected):
    assert sheet_yield(size, paper) == expected


# --------------------------------------------------------------------------
# sheets_needed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prints, paper, expected",
    [
        ({"16x21": 5}, "A4", 3),  # ceil(5 / 2)
        ({"16x21": 4}, "A4", 2),
        ({"16x21": 2}, "A4", 1),
        ({"16x21": 1}, "A4", 1),
        ({"16x21": 0}, "A4", 0),
        ({}, "A4", 0),
        ({"20x30": 3}, "A4", 3),  # one 20x30 per A4 sheet
        ({"20x30": 1}, "A4", 1),
        ({"16x21": 5, "20x30": 3}, "A4", 6),  # sizes never share a sheet
        ({"16x21": 1, "20x30": 1}, "A4", 2),  # not 1: no mixing on a sheet
        ({"16x21": 5}, "A3", 2),  # ceil(5 / 4)
        ({"16x21": 4}, "A3", 1),
        ({"20x30": 3}, "A3", 2),
        ({"30x40": 3}, "A3", 3),
        ({"16x21": 5, "20x30": 3, "30x40": 2}, "A3", 2 + 2 + 2),
        ({"16x21": 1000}, "A4", 500),
        ({"16x21": 1001}, "A4", 501),
        # keys in other notations are merged: 3 + 2 + 1 = 6 prints -> 3 sheets
        ({"۱۶×۲۱": 3, "16x21": 2, "21x16": 1}, "A4", 3),
        ({"16*21": 5}, "a4", 3),
    ],
)
def test_sheets_needed(prints, paper, expected):
    assert sheets_needed(prints, paper) == expected


def test_sheets_needed_defaults_to_a4():
    assert sheets_needed({"16x21": 5}) == 3


def test_sheets_needed_custom_table_replaces_the_default():
    table = {"A4": {"16x21": 3, "13x18": 4}}
    assert sheets_needed({"16x21": 7}, "A4", table) == 3  # ceil(7 / 3)
    assert sheets_needed({"13x18": 9}, "A4", table) == 3  # ceil(9 / 4)
    with pytest.raises(ValueError):  # 20x30 is not in the custom table (replace, not merge)
        sheets_needed({"20x30": 1}, "A4", table)


def test_sheets_needed_custom_table_keys_are_normalised():
    table = {"a4": {"۲۱×۱۶": 2}}
    assert sheets_needed({"16x21": 5}, "A4", table) == 3


@pytest.mark.parametrize(
    "prints, paper",
    [
        ({"30x40": 1}, "A4"),  # does not fit on A4
        ({"13x18": 1}, "A4"),  # custom size without a table entry
        ({"16x21": 1}, "A5"),  # unknown paper
        ({"foo": 1}, "A4"),  # unreadable size
    ],
)
def test_sheets_needed_raises_for_unprintable_sizes(prints, paper):
    with pytest.raises(ValueError):
        sheets_needed(prints, paper)


def test_sheets_needed_ignores_unprintable_sizes_with_zero_prints():
    assert sheets_needed({"30x40": 0, "16x21": 2}, "A4") == 1


@pytest.mark.parametrize("bad", [-1, 1.5, "5", None, True])
def test_sheets_needed_rejects_bad_counts(bad):
    with pytest.raises(ValueError):
        sheets_needed({"16x21": bad}, "A4")


def test_sheets_needed_rejects_non_mapping():
    with pytest.raises(ValueError):
        sheets_needed([("16x21", 2)], "A4")


# --------------------------------------------------------------------------
# packs_needed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sheets, per_pack, expected",
    [
        (0, 20, 0),
        (1, 20, 1),
        (19, 20, 1),
        (20, 20, 1),
        (21, 20, 2),
        (40, 20, 2),
        (41, 20, 3),
        (0, 50, 0),
        (1, 50, 1),
        (20, 50, 1),
        (21, 50, 1),
        (50, 50, 1),
        (51, 50, 2),
        (100, 50, 2),
        (120, 50, 3),
        (1, 1, 1),
        (7, 1, 7),
    ],
)
def test_packs_needed(sheets, per_pack, expected):
    assert packs_needed(sheets, per_pack) == expected


def test_pack_size_changes_the_pack_count():
    assert packs_needed(45, 20) == 3
    assert packs_needed(45, 50) == 1


@pytest.mark.parametrize(
    "sheets, per_pack",
    [(5, 0), (5, -20), (5, 20.0), (5, "20"), (-1, 20), (1.5, 20), (True, 20), (None, 20)],
)
def test_packs_needed_rejects_bad_input(sheets, per_pack):
    with pytest.raises(ValueError):
        packs_needed(sheets, per_pack)


# --------------------------------------------------------------------------
# plan_for_sales
# --------------------------------------------------------------------------

def test_plan_basic_rows_and_totals():
    plan = plan_for_sales({"16x21": 5, "20x30": 3}, paper="A4", sheets_per_pack=20)
    assert isinstance(plan, PrintPlan)
    assert [(r.size, r.prints, r.per_sheet, r.sheets) for r in plan.rows] == [
        ("16x21", 5, 2, 3),
        ("20x30", 3, 1, 3),
    ]
    assert plan.total_sheets == 6
    assert plan.packs == 1
    assert plan.leftover_sheets_in_last_pack == 14
    assert plan.unsupported == []
    assert plan.ok
    assert plan.total_prints == 8


@pytest.mark.parametrize(
    "prints, per_pack, sheets, packs, leftover",
    [
        ({}, 20, 0, 0, 0),
        ({"16x21": 2}, 20, 1, 1, 19),
        ({"16x21": 40}, 20, 20, 1, 0),  # exactly one full pack
        ({"16x21": 41}, 20, 21, 2, 19),
        ({"16x21": 41}, 50, 21, 1, 29),  # 20-sheet vs 50-sheet packs
        ({"16x21": 100}, 50, 50, 1, 0),
        ({"16x21": 101}, 50, 51, 2, 49),
        ({"20x30": 25}, 20, 25, 2, 15),
        ({"20x30": 25}, 50, 25, 1, 25),
        ({"16x21": 7, "20x30": 7}, 20, 4 + 7, 1, 9),
    ],
)
def test_plan_packs_and_leftovers(prints, per_pack, sheets, packs, leftover):
    plan = plan_for_sales(prints, "A4", per_pack)
    assert plan.total_sheets == sheets
    assert plan.packs == packs
    assert plan.leftover_sheets_in_last_pack == leftover
    assert plan.packs * per_pack - plan.total_sheets == plan.leftover_sheets_in_last_pack


def test_plan_defaults_are_a4_and_twenty_sheet_packs():
    plan = plan_for_sales({"16x21": 41})
    assert plan.paper == "A4"
    assert (plan.total_sheets, plan.packs, plan.leftover_sheets_in_last_pack) == (21, 2, 19)


def test_plan_30x40_on_a4_is_unsupported():
    plan = plan_for_sales({"30x40": 3}, paper="A4")
    assert plan.rows == []
    assert plan.total_sheets == 0 and plan.packs == 0
    assert plan.unsupported_sizes == ["30x40"]
    assert len(plan.unsupported) == 1
    reason = plan.unsupported[0]
    assert "30x40" in reason and "A4" in reason
    assert "A3" in reason  # hints at the paper where it does fit
    assert not plan.ok


def test_plan_30x40_on_a3_is_supported():
    plan = plan_for_sales({"30x40": 5}, paper="A3")
    assert plan.unsupported == []
    assert [(r.size, r.per_sheet, r.sheets) for r in plan.rows] == [("30x40", 1, 5)]


def test_plan_supported_and_unsupported_together():
    plan = plan_for_sales({"16x21": 5, "30x40": 2, "13x18": 4, "foo": 1}, paper="A4")
    assert [r.size for r in plan.rows] == ["16x21"]
    assert plan.total_sheets == 3
    assert sorted(plan.unsupported_sizes) == ["13x18", "30x40", "foo"]
    assert len(plan.unsupported) == 3
    joined = " ".join(plan.unsupported)
    assert "30x40" in joined and "13x18" in joined and "foo" in joined


def test_plan_unknown_custom_size_is_reported_not_raised():
    plan = plan_for_sales({"13x18": 4}, paper="A4")
    assert plan.rows == [] and plan.unsupported_sizes == ["13x18"]
    assert "13x18" in plan.unsupported[0]


def test_plan_custom_size_works_with_custom_table():
    table = {"A4": {"16x21": 2, "20x30": 1, "13x18": 4}}
    plan = plan_for_sales({"13x18": 9}, "A4", 20, table)
    assert plan.unsupported == []
    assert [(r.size, r.per_sheet, r.sheets) for r in plan.rows] == [("13x18", 4, 3)]


def test_plan_explicit_zero_yield_means_does_not_fit():
    table = {"A4": {"16x21": 2, "30x40": 0}}
    plan = plan_for_sales({"30x40": 1, "16x21": 2}, "A4", 20, table)
    assert plan.unsupported_sizes == ["30x40"]
    assert plan.total_sheets == 1


def test_plan_unknown_paper_reports_every_size():
    plan = plan_for_sales({"16x21": 5, "20x30": 1}, paper="A5")
    assert plan.rows == []
    assert sorted(plan.unsupported_sizes) == ["16x21", "20x30"]
    assert all("A5" in reason for reason in plan.unsupported)


def test_plan_merges_equivalent_size_keys():
    plan = plan_for_sales({"۱۶×۲۱": 3, "16x21": 2, "21x16": 1})
    assert [(r.size, r.prints, r.sheets) for r in plan.rows] == [("16x21", 6, 3)]


def test_plan_skips_zero_count_sizes():
    plan = plan_for_sales({"16x21": 0, "20x30": 2, "garbage": 0})
    assert [r.size for r in plan.rows] == ["20x30"]
    assert plan.unsupported == []


def test_plan_rows_are_sorted_by_size():
    plan = plan_for_sales({"30x40": 1, "20x30": 1, "16x21": 1}, paper="A3")
    assert [r.size for r in plan.rows] == ["16x21", "20x30", "30x40"]


def test_plan_paper_by_size_mixes_papers():
    plan = plan_for_sales({"16x21": 5, "30x40": 2}, paper="A4", paper_by_size={"30x40": "A3"})
    assert plan.unsupported == []
    assert {(r.size, r.paper, r.sheets) for r in plan.rows} == {("16x21", "A4", 3), ("30x40", "A3", 2)}
    assert plan.total_sheets == 5
    assert set(plan.by_paper) == {"A3", "A4"}
    assert plan.by_paper["A4"].sheets == 3 and plan.by_paper["A3"].sheets == 2
    # separate packs per paper type
    assert plan.packs == 2
    assert plan.leftover_sheets_in_last_pack == 17 + 18


def test_plan_pack_size_per_paper():
    plan = plan_for_sales(
        {"16x21": 5, "30x40": 2},
        paper="A4",
        sheets_per_pack={"A4": 20, "A3": 50},
        paper_by_size={"30x40": "a3"},
    )
    assert plan.by_paper["A4"].leftover_sheets == 17
    assert plan.by_paper["A3"].leftover_sheets == 48
    assert plan.by_paper["A3"].sheets_per_pack == 50
    assert plan.leftover_sheets_in_last_pack == 17 + 48


def test_plan_does_not_mutate_inputs_or_defaults():
    snapshot = copy.deepcopy(DEFAULT_YIELD)
    prints = {"16x21": 5, "۲۰×۳۰": 2}
    prints_before = dict(prints)
    plan_for_sales(prints, "A4", 20)
    sheets_needed(prints, "A4")
    assert DEFAULT_YIELD == snapshot
    assert prints == prints_before


def test_plan_as_dict_is_json_serialisable():
    plan = plan_for_sales({"16x21": 5, "30x40": 2}, "A4", 20)
    data = json.loads(json.dumps(plan.as_dict()))
    assert data["total_sheets"] == 3
    assert data["rows"] == [{"size": "16x21", "prints": 5, "per_sheet": 2, "sheets": 3, "paper": "A4"}]
    assert data["packs"] == 1 and data["leftover_sheets_in_last_pack"] == 17
    assert data["unsupported_sizes"] == ["30x40"]
    assert data["by_paper"]["A4"]["packs"] == 1


@pytest.mark.parametrize("bad", [-1, 1.5, "3", None, True])
def test_plan_rejects_bad_counts(bad):
    with pytest.raises(ValueError):
        plan_for_sales({"16x21": bad})


@pytest.mark.parametrize("per_pack", [0, -1, 2.5, "20", None, True])
def test_plan_rejects_bad_pack_size(per_pack):
    with pytest.raises(ValueError):
        plan_for_sales({"16x21": 2}, "A4", per_pack)


@pytest.mark.parametrize(
    "table",
    [
        {"A4": {"16x21": -1}},
        {"A4": {"16x21": 1.5}},
        {"A4": {"bad": 1}},
        {"A4": [("16x21", 2)]},
        {"A4": {"16x21": 2, "21x16": 3}},  # same size, conflicting yields
        {"": {"16x21": 2}},
        [("A4", {})],
    ],
)
def test_invalid_yield_tables_are_rejected(table):
    with pytest.raises(ValueError):
        plan_for_sales({"16x21": 1}, "A4", 20, table)
    with pytest.raises(ValueError):
        sheets_needed({"16x21": 1}, "A4", table)


def test_plan_matches_sheets_needed_on_random_orders():
    rng = random.Random(7)
    sizes = ["16x21", "20x30", "30x40"]
    for _ in range(200):
        paper = rng.choice(["A4", "A3"])
        prints = {s: rng.randrange(0, 60) for s in sizes if paper == "A3" or s != "30x40"}
        per_pack = rng.choice([20, 50])
        plan = plan_for_sales(prints, paper, per_pack)
        assert plan.unsupported == []
        assert plan.total_sheets == sheets_needed(prints, paper)
        assert plan.packs == packs_needed(plan.total_sheets, per_pack)
        assert 0 <= plan.leftover_sheets_in_last_pack < per_pack
        assert plan.packs * per_pack - plan.total_sheets == plan.leftover_sheets_in_last_pack
        # No sheet is wasted beyond one partial sheet per size.
        for row in plan.rows:
            assert row.sheets * row.per_sheet >= row.prints
            assert (row.sheets - 1) * row.per_sheet < row.prints
