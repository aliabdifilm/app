"""Parity between the browser helpers (app/static/js/{jalali,money}.js) and the
Python services (app/services/{jalali,money}.py).

The browser and the server must agree on every date and every amount, because
the date picker shows Jalali but submits ISO Gregorian, and the money widget
shows grouped Persian digits but submits a raw integer.  node runs
tests/js/parity.mjs and tests/js/money.mjs, which dump JSON; Python recomputes
the same values with the server code and compares.

The whole module is skipped (with the reason) when node or the Python services
are not available.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "tests" / "js"

NODE = (
    os.environ.get("NODE_BIN")
    or shutil.which("node")
    or ("/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None)
)

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is not installed (set NODE_BIN or add node to PATH) - JS parity tests skipped",
)


def _run_node(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _node_json(script: str):
    proc = _run_node(str(JS_DIR / script))
    assert proc.returncode == 0, f"{script} failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def py_jalali():
    return pytest.importorskip(
        "app.services.jalali",
        reason="app/services/jalali.py is not available - Jalali parity skipped",
    )


@pytest.fixture(scope="module")
def py_money():
    return pytest.importorskip(
        "app.services.money",
        reason="app/services/money.py is not available - money parity skipped",
    )


@pytest.fixture(scope="module")
def jalali_dump():
    return _node_json("parity.mjs")


@pytest.fixture(scope="module")
def money_dump():
    return _node_json("money.mjs")


# --------------------------------------------------------------------------
# stand-alone JS unit tests
# --------------------------------------------------------------------------


def test_node_unit_tests_pass():
    proc = _run_node("--test", str(JS_DIR / "unit.test.mjs"))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


def test_front_end_scripts_are_syntactically_valid():
    """`node --check` on every shipped script (catches a typo before a browser does)."""
    scripts = sorted((ROOT / "app" / "static" / "js").glob("*.js"))
    assert {p.name for p in scripts} >= {"core.js", "jalali.js", "money.js", "sw.js", "theme-init.js"}
    for script in scripts:
        proc = _run_node("--check", str(script))
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"


def test_core_js_has_no_modern_only_syntax():
    """Cheap Android phones run old WebViews: core.js sticks to ES2015-ish syntax."""
    source = (ROOT / "app" / "static" / "js" / "core.js").read_text(encoding="utf-8")
    for needle in ("?.", "??", "async ", "await "):
        assert needle not in source.replace("'?.'", ""), f"core.js uses {needle!r}"


# --------------------------------------------------------------------------
# Jalali
# --------------------------------------------------------------------------


def test_daily_sweep_1300_to_1500_matches_python(py_jalali, jalali_dump):
    sweep = jalali_dump["sweep"]
    n = len(sweep["jalali"])
    # 201 years: 1300..1500 inclusive
    assert 73000 < n < 73500, n
    assert n == len(sweep["greg"]) == len(sweep["back"]) == len(sweep["weekday"])

    mismatches = []
    for j, g, back, wd in zip(sweep["jalali"], sweep["greg"], sweep["back"], sweep["weekday"]):
        expected_g = py_jalali.jalali_to_gregorian(*j)
        expected_back = py_jalali.gregorian_to_jalali(*g)
        expected_wd = py_jalali.weekday_index(date(*g))
        if (tuple(g), tuple(back), wd) != (expected_g, expected_back, expected_wd):
            mismatches.append((j, g, back, wd, expected_g, expected_back, expected_wd))
            if len(mismatches) >= 5:
                break
    assert not mismatches, f"JS and Python disagree, first cases: {mismatches}"


def test_sweep_is_contiguous_in_js(jalali_dump):
    """Guards the sweep itself: consecutive Jalali days map to consecutive Gregorian days."""
    greg = [date(*g) for g in jalali_dump["sweep"]["greg"]]
    gaps = [(a, b) for a, b in zip(greg, greg[1:]) if (b - a).days != 1]
    assert not gaps, gaps[:3]


def test_full_range_years_match_python(py_jalali, jalali_dump):
    """Leap flag, month lengths and Nowruz of every supported year 1..3000."""
    years = jalali_dump["years"]
    assert len(years) == py_jalali.MAX_JALALI_YEAR - py_jalali.MIN_JALALI_YEAR + 1
    bad = []
    for key, info in years.items():
        jy = int(key)
        expected = {
            "leap": py_jalali.is_jalali_leap(jy),
            "lengths": [py_jalali.jalali_month_length(jy, m) for m in range(1, 13)],
            "nowruz": list(py_jalali.jalali_to_gregorian(jy, 1, 1)),
        }
        if info != expected:
            bad.append((jy, info, expected))
            if len(bad) >= 5:
                break
    assert not bad, bad


def test_parse_matches_python(py_jalali, jalali_dump):
    bad = []
    for case in jalali_dump["parse"]:
        try:
            d = py_jalali.parse_jalali(case["text"])
            expected = [d.year, d.month, d.day]
        except ValueError:
            expected = None
        if case["result"] != expected:
            bad.append((case["text"], case["result"], expected))
    assert not bad, bad


def test_format_matches_python(py_jalali, jalali_dump):
    bad = []
    for case in jalali_dump["format"]:
        expected = py_jalali.to_jalali_str(case["iso"], case["fmt"], persian_digits=case["fa"])
        if case["text"] != expected:
            bad.append((case, expected))
    assert not bad, bad[:5]
    assert len(jalali_dump["format"]) > 50


def test_js_names_match_python_constants(py_jalali):
    proc = _run_node(
        "-e",
        "const J=require('./app/static/js/jalali.js');"
        "process.stdout.write(JSON.stringify({m:J.MONTHS,w:J.WEEKDAYS}))",
    )
    assert proc.returncode == 0, proc.stderr
    names = json.loads(proc.stdout)
    assert tuple(names["m"]) == tuple(py_jalali.JALALI_MONTHS)
    assert tuple(names["w"]) == tuple(py_jalali.JALALI_WEEKDAYS)


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


def _py_parse(py_money, text, **kw):
    try:
        return True, py_money.parse_toman(text, **kw)
    except ValueError:
        return False, None


def test_money_parse_matches_python(py_money, money_dump):
    assert len(money_dump["parse"]) > 30
    bad = []
    for case in money_dump["parse"]:
        ok, value = _py_parse(py_money, case["text"])
        if (case["ok"], case["value"]) != (ok, value):
            bad.append((case["text"], (case["ok"], case["value"]), (ok, value)))
    assert not bad, bad


def test_money_parse_negative_and_required_match_python(py_money, money_dump):
    bad = []
    for case in money_dump["parse_negative"]:
        ok, value = _py_parse(py_money, case["text"], allow_negative=True)
        if (case["ok"], case["value"]) != (ok, value):
            bad.append(("neg", case["text"], (case["ok"], case["value"]), (ok, value)))
    for case in money_dump["parse_required"]:
        ok, value = _py_parse(py_money, case["text"], allow_blank=False)
        if (case["ok"], case["value"]) != (ok, value):
            bad.append(("req", case["text"], (case["ok"], case["value"]), (ok, value)))
    assert not bad, bad


def test_money_format_matches_python(py_money, money_dump):
    assert len(money_dump["format"]) > 60
    bad = []
    for case in money_dump["format"]:
        try:
            expected = py_money.format_toman(
                case["n"], persian_digits=case["fa"], suffix=case["suffix"]
            )
        except ValueError:
            expected = None
        if case["text"] != expected:
            bad.append((case, expected))
    assert not bad, bad[:5]


def test_agents_md_payroll_vector_formats_identically(py_money):
    """AGENTS.md test vector: 1,000,000 + 80 x 9,000 + 150,000 = 1,870,000 Toman."""
    proc = _run_node(
        "-e",
        "const M=require('./app/static/js/money.js');"
        "process.stdout.write(M.formatToman(1000000+80*9000+150000,{fa:true,suffix:true}))",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == py_money.format_toman(1_870_000, persian_digits=True, suffix=True)
