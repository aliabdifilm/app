"""Direct MetaTrader 5 bridge - analytics, export, and an independent watchdog.

Runs on the Windows machine that hosts the terminal, alongside the EA. It uses
the official `MetaTrader5` package, which talks to a running terminal over IPC.

Why this exists when the EA already reports everything:

  * The EA reports what it believes. This reads what the TERMINAL believes.
    When the two disagree, something is wrong and you want to know.
  * Performance statistics (profit factor, expectancy, max drawdown, Sharpe)
    are far easier to compute here than in MQL5.
  * `guard` is a second, independent pair of eyes. If the EA hangs, crashes,
    or is removed from the chart, positions can still be left open. This
    process can enforce a hard equity floor on its own.

Usage
    python mt5_bridge.py status
    python mt5_bridge.py positions
    python mt5_bridge.py history --days 30 --csv deals.csv
    python mt5_bridge.py stats --days 90 --magic 20260921
    python mt5_bridge.py guard --max-drawdown 12 --daily-loss 5          (dry run)
    python mt5_bridge.py guard --max-drawdown 12 --daily-loss 5 --enforce

IMPORTANT: the `MetaTrader5` package is Windows-only, because the MT5 terminal
it attaches to is a Windows application. On Linux/macOS run it inside the same
Wine prefix as the terminal, or simply use the EA's own panel instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - depends on the host platform
    mt5 = None


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------
def connect(path: str = "", login: int = 0, password: str = "", server: str = "") -> bool:
    if mt5 is None:
        print("The MetaTrader5 package is not installed (or this is not Windows).")
        print("  pip install MetaTrader5")
        return False

    kwargs: Dict[str, Any] = {}
    if path:
        kwargs["path"] = path
    if login:
        kwargs.update(login=int(login), password=password, server=server)

    if not mt5.initialize(**kwargs):
        print(f"initialize() failed: {mt5.last_error()}")
        print("Is the MetaTrader 5 terminal running and logged in?")
        return False
    return True


def disconnect() -> None:
    if mt5 is not None:
        mt5.shutdown()


# ----------------------------------------------------------------------
# Read-only views
# ----------------------------------------------------------------------
def account_snapshot() -> Dict[str, Any]:
    info = mt5.account_info()
    if info is None:
        return {}
    return {
        "login": info.login,
        "server": info.server,
        "company": info.company,
        "currency": info.currency,
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "free_margin": info.margin_free,
        "margin_level": info.margin_level,
        "profit": info.profit,
        "leverage": info.leverage,
        "trade_allowed": bool(info.trade_allowed),
        "trade_expert": bool(info.trade_expert),
    }


def open_positions(magic: int = 0) -> List[Dict[str, Any]]:
    positions = mt5.positions_get()
    if positions is None:
        return []
    out = []
    for p in positions:
        if magic and p.magic != magic:
            continue
        out.append(
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "open": p.price_open,
                "current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": p.swap,
                "magic": p.magic,
                "opened": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
                "comment": p.comment,
            }
        )
    return out


def closed_deals(days: int = 30, magic: int = 0) -> List[Dict[str, Any]]:
    """Every closing deal in the window, newest last."""
    to = datetime.now(timezone.utc) + timedelta(days=1)
    frm = datetime.now(timezone.utc) - timedelta(days=days)

    deals = mt5.history_deals_get(frm, to)
    if deals is None:
        return []

    out = []
    for d in deals:
        if magic and d.magic != magic:
            continue
        if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
            continue
        out.append(
            {
                "ticket": d.ticket,
                "position": d.position_id,
                "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                "timestamp": d.time,
                "symbol": d.symbol,
                "side": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                "volume": d.volume,
                "price": d.price,
                "profit": d.profit,
                "commission": d.commission,
                "swap": d.swap,
                "net": d.profit + d.commission + d.swap,
                "magic": d.magic,
                "comment": d.comment,
            }
        )
    out.sort(key=lambda r: r["timestamp"])
    return out


# ----------------------------------------------------------------------
# Performance statistics
# ----------------------------------------------------------------------
def compute_stats(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The numbers that actually tell you whether a system is working.

    Win rate on its own is meaningless - a 90% win rate with a 10:1 loss ratio
    loses money. Profit factor and expectancy are what matter, and max
    drawdown is what decides whether you can survive to collect them.
    """
    if not deals:
        return {"trades": 0}

    nets = [d["net"] for d in deals]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(nets)

    # equity curve from a zero base, for drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []
    for n in nets:
        equity += n
        curve.append(equity)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    mean = total / len(nets)
    variance = sum((n - mean) ** 2 for n in nets) / len(nets) if len(nets) > 1 else 0.0
    stdev = math.sqrt(variance)

    # longest run of consecutive losers - the streak that breaks discipline
    worst_streak = streak = 0
    for n in nets:
        streak = streak + 1 if n < 0 else 0
        worst_streak = max(worst_streak, streak)

    return {
        "trades": len(nets),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(nets), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(total, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy_per_trade": round(mean, 3),
        "average_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "payoff_ratio": round(
            (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 3
        ) if wins and losses else None,
        "max_drawdown": round(max_dd, 2),
        # Per-trade Sharpe. Not annualised on purpose: annualising a handful of
        # trades produces a confident-looking number that means nothing.
        "sharpe_per_trade": round(mean / stdev, 3) if stdev > 0 else None,
        "worst_losing_streak": worst_streak,
        "final_equity_delta": round(curve[-1], 2),
    }


# ----------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------
def close_position(position: Any, deviation: int = 20) -> bool:
    symbol = position.symbol
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return False

    is_long = position.type == mt5.POSITION_TYPE_BUY
    order_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
    price = tick.bid if is_long else tick.ask

    # Same filling-mode dance as the EA: hard-coding FOK is how you get 10030.
    if info.filling_mode & 2:
        filling = mt5.ORDER_FILLING_IOC
    elif info.filling_mode & 1:
        filling = mt5.ORDER_FILLING_FOK
    else:
        filling = mt5.ORDER_FILLING_RETURN

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": symbol,
        "volume": position.volume,
        "type": order_type,
        "price": price,
        "deviation": deviation,
        "magic": position.magic,
        "comment": "apex-guard",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    result = mt5.order_send(request)
    if result is None:
        print(f"  close {position.ticket} failed: {mt5.last_error()}")
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"  close {position.ticket} rejected: {result.retcode} {result.comment}")
        return False
    print(f"  closed {position.ticket} {symbol} {position.volume}")
    return True


def run_guard(max_drawdown_pct: float, daily_loss_pct: float, interval: int,
              enforce: bool, magic: int, state_path: str) -> None:
    """Independent safety net.

    Deliberately dumb: it knows nothing about strategy, only about an equity
    floor. That is exactly why it is useful - it keeps working when the
    clever part is the thing that broke.
    """
    state = {"peak_equity": 0.0, "day": "", "day_start_balance": 0.0}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state.update(json.load(fh))
        except (OSError, ValueError):
            pass

    mode = "ENFORCING" if enforce else "DRY RUN (no orders will be sent)"
    print(f"[guard] {mode}: max drawdown {max_drawdown_pct}%, daily loss {daily_loss_pct}%")
    print("[guard] Ctrl-C to stop.")

    try:
        while True:
            info = mt5.account_info()
            if info is None:
                print("[guard] terminal not reachable, retrying")
                time.sleep(interval)
                continue

            today = datetime.now().strftime("%Y-%m-%d")
            if state["day"] != today:
                state["day"] = today
                state["day_start_balance"] = info.balance
                print(f"[guard] new day, start balance {info.balance:.2f}")

            state["peak_equity"] = max(state["peak_equity"], info.equity)

            drawdown = 0.0
            if state["peak_equity"] > 0:
                drawdown = (state["peak_equity"] - info.equity) / state["peak_equity"] * 100.0

            day_loss = 0.0
            if state["day_start_balance"] > 0:
                day_loss = (state["day_start_balance"] - info.equity) / state["day_start_balance"] * 100.0

            breach = None
            if max_drawdown_pct > 0 and drawdown >= max_drawdown_pct:
                breach = f"drawdown {drawdown:.2f}% >= {max_drawdown_pct}%"
            elif daily_loss_pct > 0 and day_loss >= daily_loss_pct:
                breach = f"daily loss {day_loss:.2f}% >= {daily_loss_pct}%"

            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[guard {stamp}] equity {info.equity:.2f} dd {drawdown:.2f}% "
                  f"day {-day_loss:+.2f}%" + (f"  BREACH: {breach}" if breach else ""))

            if breach:
                positions = mt5.positions_get() or []
                targets = [p for p in positions if not magic or p.magic == magic]
                if targets:
                    print(f"[guard] BREACH ({breach}) - {len(targets)} position(s) affected")
                    if enforce:
                        for p in targets:
                            close_position(p)
                    else:
                        for p in targets:
                            print(f"  would close {p.ticket} {p.symbol} {p.volume}")

            try:
                with open(state_path, "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
            except OSError:
                pass

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[guard] stopped")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def print_table(rows: List[Dict[str, Any]], columns: List[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def main() -> int:
    parser = argparse.ArgumentParser(description="ApexAlgo MetaTrader 5 bridge")
    parser.add_argument("--path", default="", help="terminal64.exe path (optional)")
    parser.add_argument("--login", type=int, default=0)
    parser.add_argument("--password", default="")
    parser.add_argument("--server", default="")
    parser.add_argument("--magic", type=int, default=0, help="filter to one EA instance")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="account summary")
    sub.add_parser("positions", help="open positions")

    p_hist = sub.add_parser("history", help="closed deals")
    p_hist.add_argument("--days", type=int, default=30)
    p_hist.add_argument("--csv", default="", help="also write a CSV file")

    p_stats = sub.add_parser("stats", help="performance statistics")
    p_stats.add_argument("--days", type=int, default=90)
    p_stats.add_argument("--json", action="store_true")

    p_guard = sub.add_parser("guard", help="independent equity watchdog")
    p_guard.add_argument("--max-drawdown", type=float, default=12.0)
    p_guard.add_argument("--daily-loss", type=float, default=5.0)
    p_guard.add_argument("--interval", type=int, default=15, help="seconds between checks")
    p_guard.add_argument("--enforce", action="store_true",
                         help="actually close positions (omit for a dry run)")
    p_guard.add_argument("--state", default="apex_guard_state.json")

    args = parser.parse_args()

    if not connect(args.path, args.login, args.password, args.server):
        return 1

    try:
        if args.command == "status":
            snapshot = account_snapshot()
            if not snapshot:
                print("no account info")
                return 1
            width = max(len(k) for k in snapshot)
            for key, value in snapshot.items():
                print(f"{key.ljust(width)} : {value}")
            if not snapshot["trade_expert"]:
                print("\nWARNING: this account does not allow Expert Advisor trading.")

        elif args.command == "positions":
            rows = open_positions(args.magic)
            print_table(rows, ["ticket", "symbol", "side", "volume", "open",
                               "current", "sl", "tp", "profit"])
            if rows:
                print(f"\ntotal floating P/L: {sum(r['profit'] for r in rows):+.2f}")

        elif args.command == "history":
            rows = closed_deals(args.days, args.magic)
            print_table(rows, ["time", "symbol", "side", "volume", "price", "net"])
            print(f"\n{len(rows)} closing deal(s), net {sum(r['net'] for r in rows):+.2f}")
            if args.csv and rows:
                with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
                print(f"wrote {args.csv}")

        elif args.command == "stats":
            rows = closed_deals(args.days, args.magic)
            stats = compute_stats(rows)
            if args.json:
                print(json.dumps(stats, indent=2))
            else:
                print(f"--- last {args.days} days"
                      + (f", magic {args.magic}" if args.magic else "") + " ---")
                width = max(len(k) for k in stats)
                for key, value in stats.items():
                    print(f"{key.ljust(width)} : {value}")
                if stats.get("trades", 0) < 30:
                    print("\nNote: fewer than 30 trades. These numbers are not yet"
                          " statistically meaningful.")

        elif args.command == "guard":
            run_guard(args.max_drawdown, args.daily_loss, args.interval,
                      args.enforce, args.magic, args.state)

    finally:
        disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
