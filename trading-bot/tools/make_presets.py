"""Generate MetaTrader 5 preset (.set) files for the ApexAlgo EA.

Why a generator instead of hand-written .set files:
MT5 silently ignores any line whose name does not match an input. A typo in
"InpDailyLossLimitPct" would not raise an error - the EA would just run with
the default daily loss limit while you believe your own value is in force.
This script checks every key and every value against the EA source before
writing anything, so that failure mode cannot ship.

Output format matches what MT5 itself writes: UTF-16 LE with BOM, CRLF.

    python make_presets.py            # write presets/*.set
    python make_presets.py --check    # validate only, write nothing
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EA_SOURCE = os.path.join(ROOT, "mql5", "Experts", "ApexAlgo", "ApexAlgoEA.mq5")
OUT_DIR = os.path.join(ROOT, "presets")

# MQL5 enum values are written to .set files as integers.
TIMEFRAMES = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M15": 15, "M30": 30,
              "H1": 16385, "H4": 16388, "D1": 16408}
LOGLEVEL = {"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}
STRATMODE = {"AUTO": 0, "TREND_ONLY": 1, "RANGE_ONLY": 2}
OFFLINE = {"KEEP_TRADING": 0, "NO_NEW_TRADES": 1, "FLATTEN": 2}

ENUM_DOMAINS = {
    "ENUM_TIMEFRAMES": set(TIMEFRAMES.values()),
    "ENUM_APEX_LOGLEVEL": set(LOGLEVEL.values()),
    "ENUM_APEX_STRATMODE": set(STRATMODE.values()),
    "ENUM_APEX_OFFLINE": set(OFFLINE.values()),
}


# ----------------------------------------------------------------------
# Presets
# ----------------------------------------------------------------------
# Gold M1 fast scalping.
#
# The dominant cost in M1 gold scalping is the spread, not the strategy:
# ~25 points of spread against a ~180 point stop means every trade opens at
# about -0.14R. Every choice below is made to keep that cost from eating
# the account:
#   * small risk per trade, because trade count is high
#   * a hard spread ceiling plus a spread-to-ATR ceiling
#   * trading only in the London / New York hours, when gold spreads are
#     tightest
#   * a minimum ATR, so the bot does not scalp a dead market where the
#     spread is a larger fraction of every move
#   * break-even offset (0.15R) set just above the spread cost, so a
#     "break-even" exit really is break-even
#   * one position at a time: scalping several gold positions at once is
#     one oversized bet, not several small ones
GOLD_M1_SCALP: Dict[str, object] = {
    # --- general
    "InpMagic": 20260923,
    "InpSymbols": "",                 # empty = the chart's symbol, whatever the broker calls gold
    "InpSignalTF": TIMEFRAMES["M1"],
    "InpBiasTF": TIMEFRAMES["M15"],
    "InpTradeComment": "ApexScalp",
    "InpLogLevel": LOGLEVEL["INFO"],
    "InpLogToFile": True,

    # --- strategy: faster lookbacks for a one-minute chart
    "InpStrategyMode": STRATMODE["AUTO"],
    "InpRequireBias": True,
    "InpMinScore": 0.50,              # stricter than default: fewer, better scalps
    "InpAtrPeriod": 14,
    "InpAdxPeriod": 14,
    "InpRsiPeriod": 7,
    "InpBBPeriod": 20,
    "InpBBDev": 2.0,
    "InpEmaFast": 9,
    "InpEmaSlow": 21,
    "InpBiasEmaFast": 21,
    "InpBiasEmaSlow": 50,
    "InpDonchianPeriod": 15,
    "InpErPeriod": 10,
    "InpAtrAvgPeriod": 60,

    # --- regime
    "InpAdxTrendMin": 22.0,
    "InpAdxRangeMax": 17.0,
    "InpErTrendMin": 0.30,
    "InpErRangeMax": 0.20,
    "InpRsiOversold": 25.0,
    "InpRsiOverbought": 75.0,
    "InpPullbackLookback": 4,

    # --- geometry: tight stops, quick targets
    "InpAtrStopTrend": 1.5,
    "InpAtrStopRange": 1.2,
    "InpRRTrend": 1.5,
    "InpRRRange": 1.0,

    # --- risk
    "InpRiskPercent": 0.25,
    "InpMaxRiskPercent": 1.0,
    "InpDailyLossLimitPct": 3.0,
    "InpMaxDrawdownPct": 10.0,
    "InpDDThrottleStartPct": 3.0,
    "InpDDThrottleFloor": 0.35,
    "InpMaxPositionsTotal": 1,
    "InpMaxPositionsPerSym": 1,
    "InpMaxTradesPerDay": 40,
    "InpLossStreakLimit": 4,
    "InpCooldownMinutes": 30,
    "InpMaxLotsPerTrade": 2.0,
    "InpMaxMarginUtilPct": 20.0,
    "InpMaxExposurePerCcy": 1,

    # --- management: bank quickly, never give a winner back
    "InpUseBreakEven": True,
    "InpBeTriggerR": 0.7,
    "InpBeOffsetR": 0.15,             # covers the spread, so BE really is BE
    "InpUsePartial": True,
    "InpPartialTriggerR": 0.8,
    "InpPartialPercent": 50.0,
    "InpUseTrailing": True,
    "InpTrailStartR": 1.0,
    "InpTrailAtrMult": 1.2,
    "InpUseTimeStop": True,
    "InpMaxBarsInTrade": 25,          # 25 minutes: a scalp that has not worked has failed
    "InpTimeStopMinR": 0.3,

    # --- filters: the spread defence
    "InpMaxSpreadAtr": 0.35,
    "InpMaxSpreadPoints": 35,
    "InpMinAtrPoints": 40.0,
    "InpMaxAtrMultiple": 3.5,
    "InpMinBarsBetweenTrd": 2,
    "InpUseSessions": True,
    # Server time. Most gold brokers run GMT+2 (winter) / GMT+3 (summer),
    # so 10-19 covers London open through the New York overlap.
    # CHECK YOUR BROKER'S SERVER CLOCK - see docs/12-gold-scalping.md.
    "InpSess1Start": 10,
    "InpSess1End": 19,
    "InpSess2Start": 0,
    "InpSess2End": 0,
    "InpTradeSunday": False,
    "InpTradeMonday": True,
    "InpTradeTuesday": True,
    "InpTradeWednesday": True,
    "InpTradeThursday": True,
    "InpTradeFriday": True,
    "InpTradeSaturday": False,
    "InpWeekendFlat": True,
    "InpFridayCloseHour": 20,
    "InpMondayOpenHour": 2,
    "InpUseNewsFilter": True,
    "InpNewsMinutesBefore": 20,
    "InpNewsMinutesAfter": 20,
    "InpNewsMinImportance": 3,

    # --- execution: gold moves fast
    "InpSlippagePoints": 30,
    "InpMaxRetries": 3,
    "InpRetryDelayMs": 150,

    # --- remote: laptop-only setup needs no server
    "InpRemoteEnabled": False,
    "InpRemoteUrl": "http://127.0.0.1:8800",
    "InpRemoteToken": "",
    "InpBotId": "apex-gold",
    "InpRemoteTimeoutMs": 3000,
    "InpRemotePollSeconds": 10,
    "InpRemoteStaleSeconds": 180,
    "InpOfflinePolicy": OFFLINE["KEEP_TRADING"],

    # --- display
    "InpShowPanel": True,
    "InpPanelX": 12,
    "InpPanelY": 22,
}

# Gold M5: the same idea one step calmer. Fewer trades, and the spread is a
# much smaller fraction of each stop. The better starting point if M1
# results on demo are dominated by spread cost.
GOLD_M5_SCALP = dict(GOLD_M1_SCALP)
GOLD_M5_SCALP.update({
    "InpMagic": 20260924,
    "InpSignalTF": TIMEFRAMES["M5"],
    "InpBiasTF": TIMEFRAMES["H1"],
    "InpMinScore": 0.47,
    "InpRsiPeriod": 9,
    "InpDonchianPeriod": 20,
    "InpErPeriod": 14,
    "InpAtrAvgPeriod": 80,
    "InpAtrStopTrend": 1.8,
    "InpAtrStopRange": 1.4,
    "InpRRTrend": 1.8,
    "InpRRRange": 1.1,
    "InpMaxTradesPerDay": 20,
    "InpBeOffsetR": 0.10,
    "InpTrailAtrMult": 1.5,
    "InpMaxBarsInTrade": 24,          # 2 hours on M5
    "InpMaxSpreadAtr": 0.25,
    "InpMinAtrPoints": 80.0,
    "InpMinBarsBetweenTrd": 2,
    "InpTradeComment": "ApexScalpM5",
})

PRESETS = {
    "ApexAlgo_XAUUSD_Scalp_M1": GOLD_M1_SCALP,
    "ApexAlgo_XAUUSD_Scalp_M5": GOLD_M5_SCALP,
}


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def load_inputs() -> Dict[str, Tuple[str, str]]:
    src = open(EA_SOURCE, encoding="utf-8").read()
    rx = re.compile(r'^input\s+(?P<type>[\w_]+)\s+(?P<name>\w+)\s*=\s*(?P<default>[^;]+);', re.M)
    return {m.group("name"): (m.group("type"), m.group("default").strip()) for m in rx.finditer(src)}


def validate(name: str, preset: Dict[str, object], inputs) -> list:
    problems = []

    unknown = sorted(set(preset) - set(inputs))
    for key in unknown:
        problems.append(f"{name}: '{key}' is not an EA input - MT5 would silently ignore it")

    missing = sorted(set(inputs) - set(preset))
    for key in missing:
        problems.append(f"{name}: '{key}' is not set - the preset must pin every input")

    for key, value in preset.items():
        if key not in inputs:
            continue
        type_, _ = inputs[key]
        if type_ == "bool" and not isinstance(value, bool):
            problems.append(f"{name}: {key} must be a bool, got {value!r}")
        elif type_ in ("int", "long") and (isinstance(value, bool) or not isinstance(value, int)):
            problems.append(f"{name}: {key} must be an integer, got {value!r}")
        elif type_ == "double" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            problems.append(f"{name}: {key} must be a number, got {value!r}")
        elif type_ == "string" and not isinstance(value, str):
            problems.append(f"{name}: {key} must be a string, got {value!r}")
        elif type_ in ENUM_DOMAINS and value not in ENUM_DOMAINS[type_]:
            problems.append(f"{name}: {key}={value!r} is not a valid {type_}")

    # The cross-field rules below compare values, so they only make sense once
    # every field is present and correctly typed. Report field problems first
    # rather than crashing on them.
    if problems:
        return problems

    # Mirror the EA's own ValidateInputs() so a preset that the EA would
    # reject at load time is caught here instead.
    p = preset
    rules = [
        (0 < p["InpRiskPercent"] <= 10, "risk per trade must be in (0, 10]"),
        (p["InpMaxRiskPercent"] >= p["InpRiskPercent"], "max risk below base risk"),
        (0 < p["InpMaxDrawdownPct"] <= 90, "max drawdown must be in (0, 90]"),
        (0 < p["InpDailyLossLimitPct"] <= p["InpMaxDrawdownPct"], "daily loss limit invalid"),
        (p["InpEmaFast"] < p["InpEmaSlow"], "fast EMA must be below slow EMA"),
        (p["InpBiasEmaFast"] < p["InpBiasEmaSlow"], "bias fast EMA must be below bias slow EMA"),
        (p["InpAdxRangeMax"] < p["InpAdxTrendMin"], "ADX range ceiling must be below trend floor"),
        (p["InpErRangeMax"] < p["InpErTrendMin"], "ER range ceiling must be below trend floor"),
        (0 < p["InpMinScore"] < 1, "min score must be in (0, 1)"),
        (p["InpAtrStopTrend"] > 0 and p["InpAtrStopRange"] > 0, "ATR stop multipliers must be positive"),
        (p["InpMaxPositionsTotal"] >= 1 and p["InpMaxPositionsPerSym"] >= 1, "position limits below 1"),
        (_tf_seconds(p["InpSignalTF"]) <= _tf_seconds(p["InpBiasTF"]), "signal TF above bias TF"),
    ]
    for ok, message in rules:
        if not ok:
            problems.append(f"{name}: EA would reject this preset - {message}")

    # Scalping-specific sanity: a break-even that does not cover the spread
    # is a guaranteed small loss dressed up as a scratch.
    if p["InpUseBreakEven"] and p["InpBeOffsetR"] <= 0:
        problems.append(f"{name}: break-even offset must be positive to cover spread")

    return problems


def _tf_seconds(tf: int) -> int:
    minutes = {1: 1, 2: 2, 3: 3, 5: 5, 15: 15, 30: 30, 16385: 60, 16388: 240, 16408: 1440}
    return minutes.get(tf, 0) * 60


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def render(name: str, preset: Dict[str, object], inputs) -> str:
    lines = [
        f"; {name}",
        "; generated by tools/make_presets.py - edit the generator, not this file",
        "; load in MT5: EA properties > Inputs > Load",
    ]
    # Keep the EA's own input order so the file reads like the Inputs tab.
    for key in inputs:
        value = preset[key]
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, float):
            text = repr(value)
        else:
            text = str(value)
        lines.append(f"{key}={text}")
    return "\r\n".join(lines) + "\r\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate only")
    args = parser.parse_args()

    inputs = load_inputs()
    problems = []
    for name, preset in PRESETS.items():
        problems += validate(name, preset, inputs)

    if problems:
        print("Preset validation FAILED:")
        for problem in problems:
            print("  - " + problem)
        return 1

    print(f"{len(PRESETS)} preset(s) valid against {len(inputs)} EA inputs")
    if args.check:
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, preset in PRESETS.items():
        path = os.path.join(OUT_DIR, name + ".set")
        with open(path, "wb") as fh:
            # MT5 writes .set files as UTF-16 LE with a BOM
            fh.write(render(name, preset, inputs).encode("utf-16"))
        print(f"  wrote {os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
