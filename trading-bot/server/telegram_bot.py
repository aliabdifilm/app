"""Telegram bridge for the ApexAlgo control server.

Gives you the whole panel from a phone without opening a browser: status,
pause/resume, close positions, change risk, and the emergency kill switch.

Safety rules baked in:
    * Only chat ids in `telegram.allowed_chat_ids` are listened to at all.
      An unknown chat gets a single reply telling the operator what id to
      whitelist, and nothing else ever happens for it.
    * Destructive commands (close all, flatten, kill) require tapping a
      confirm button. A typo cannot flatten a book.
    * The bridge is optional. If it crashes, trading is unaffected: the EA
      never talks to Telegram, only to the control server.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

API = "https://api.telegram.org/bot{token}/{method}"

_cfg = None
_store = None
_validate: Optional[Callable[..., Tuple[bool, str, float]]] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

# Confirmation tokens handed out with inline buttons, so a stale button from
# yesterday cannot fire today.
_pending: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL = 120.0

HELP = (
    "*ApexAlgo control*\n\n"
    "/status - account, risk and guard state\n"
    "/positions - open positions\n"
    "/events - recent bot events\n"
    "/pause - stop opening new trades\n"
    "/resume - resume trading\n"
    "/risk 0.5 - set risk per trade (%)\n"
    "/closeall - close every position (confirm)\n"
    "/close EURUSD - close one symbol (confirm)\n"
    "/flatten - close everything and pause (confirm)\n"
    "/kill - emergency stop (confirm)\n"
    "/release - release the kill switch\n"
)


# ----------------------------------------------------------------------
# Telegram transport
# ----------------------------------------------------------------------
def _call(method: str, **params) -> Optional[dict]:
    if _cfg is None or not _cfg.telegram.bot_token:
        return None
    try:
        response = requests.post(
            API.format(token=_cfg.telegram.bot_token, method=method),
            json=params,
            timeout=35,
        )
        data = response.json()
        if not data.get("ok"):
            print(f"[telegram] {method} failed: {data.get('description')}")
            return None
        return data.get("result")
    except requests.RequestException as exc:
        print(f"[telegram] {method} transport error: {exc}")
        return None


def _send(chat_id: int, text: str, keyboard: Optional[list] = None) -> None:
    params: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    _call("sendMessage", **params)


def notify(bot_id: str, level: str, title: str, message: str) -> None:
    """Called by app.py whenever the EA posts an event."""
    if _cfg is None or not _cfg.telegram.enabled:
        return
    if level not in _cfg.telegram.notify_levels:
        # trade notifications are opt-in separately from warnings/errors
        is_trade = title.lower().startswith(("trade", "entry"))
        if not (is_trade and _cfg.telegram.notify_trades):
            return

    icon = {"error": "🛑", "warn": "⚠️", "info": "ℹ️"}.get(level, "•")
    text = f"{icon} *{_escape(title)}*\n{_escape(message)}\n`{_escape(bot_id)}`"
    for chat_id in _cfg.telegram.allowed_chat_ids:
        _send(chat_id, text)


def _escape(text: str) -> str:
    # Markdown (legacy) only needs these four neutralised.
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------
def _bot_id() -> str:
    bots = _store.list_bots()
    return bots[0]["bot_id"] if bots else "apex-1"


def _status_text(bot_id: str) -> str:
    bot = _store.get_bot(bot_id)
    if not bot:
        return "No bot has reported in yet."

    state = bot.get("state", {})
    age = int(time.time()) - int(bot.get("last_seen", 0))
    online = age <= _cfg.offline_after_seconds

    halt = state.get("halt", "NONE")
    guard = "clear" if halt == "NONE" else halt
    if bot.get("paused"):
        guard = "PAUSED"

    return (
        f"*{_escape(bot_id)}* {'🟢 online' if online else '🔴 offline'} ({age}s ago)\n"
        f"Account `{state.get('login', '?')}` @ {_escape(str(state.get('server', '?')))}\n\n"
        f"Balance  `{state.get('balance', 0):,.2f}` {state.get('currency', '')}\n"
        f"Equity   `{state.get('equity', 0):,.2f}`\n"
        f"Day P/L  `{state.get('day_pnl', 0):,.2f}` ({state.get('day_pnl_pct', 0):+.2f}%)\n"
        f"Drawdown `{state.get('drawdown_pct', 0):.2f}%`\n\n"
        f"Risk/trade `{state.get('risk_percent', 0):.2f}%`\n"
        f"Positions `{state.get('open_positions', 0)}`  "
        f"Trades today `{state.get('trades_today', 0)}`\n"
        f"Loss streak `{state.get('loss_streak', 0)}`\n"
        f"Guard: *{_escape(guard)}*\n"
        f"{_escape(str(state.get('halt_reason', '')))}"
    )


def _positions_text(bot_id: str) -> str:
    bot = _store.get_bot(bot_id)
    if not bot:
        return "No bot has reported in yet."
    positions = bot.get("state", {}).get("positions", []) or []
    if not positions:
        return "No open positions."

    lines = ["*Open positions*"]
    total = 0.0
    for p in positions:
        profit = float(p.get("profit", 0))
        total += profit
        lines.append(
            f"`{p.get('symbol','?'):<10}` {p.get('side','?'):<4} "
            f"{float(p.get('volume',0)):.2f}  {profit:+.2f}"
        )
    lines.append(f"\nTotal: *{total:+.2f}*")
    return "\n".join(lines)


def _events_text(bot_id: str) -> str:
    events = _store.recent_events(bot_id, 10)
    if not events:
        return "No events recorded."
    lines = ["*Recent events*"]
    for e in events:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
        lines.append(f"`{stamp}` {_escape(e['title'])} - {_escape(e['message'])[:80]}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Command handling
# ----------------------------------------------------------------------
def _issue(bot_id: str, type_: str, symbol: str = "", value: float = 0.0) -> str:
    ok, error, value = _validate(type_, symbol, value, confirmed=True)
    if not ok:
        return f"Rejected: {error}"
    _store.enqueue_command(bot_id, type_, symbol, value, source="telegram")
    return f"✅ Queued *{_escape(type_)}* {_escape(symbol)}".strip()


def _confirm_keyboard(action: str, symbol: str = "", value: float = 0.0) -> list:
    token = f"{int(time.time()*1000)}{len(_pending)}"
    _pending[token] = {
        "action": action,
        "symbol": symbol,
        "value": value,
        "expires": time.time() + _PENDING_TTL,
    }
    return [
        [
            {"text": "✅ Confirm", "callback_data": f"y:{token}"},
            {"text": "✖ Cancel", "callback_data": f"n:{token}"},
        ]
    ]


def _prune_pending() -> None:
    now = time.time()
    for token in [k for k, v in _pending.items() if v["expires"] < now]:
        _pending.pop(token, None)


def _handle_message(chat_id: int, text: str) -> None:
    bot_id = _bot_id()
    parts = text.strip().split()
    if not parts:
        return
    command = parts[0].lower().split("@")[0]
    args = parts[1:]

    if command in ("/start", "/help"):
        _send(chat_id, HELP)
    elif command == "/status":
        _send(chat_id, _status_text(bot_id))
    elif command == "/positions":
        _send(chat_id, _positions_text(bot_id))
    elif command == "/events":
        _send(chat_id, _events_text(bot_id))
    elif command == "/pause":
        _send(chat_id, _issue(bot_id, "pause"))
    elif command == "/resume":
        _send(chat_id, _issue(bot_id, "resume"))
    elif command == "/release":
        _send(chat_id, _issue(bot_id, "release_kill"))
    elif command == "/risk":
        if not args:
            _send(chat_id, "Usage: `/risk 0.5`")
            return
        try:
            value = float(args[0])
        except ValueError:
            _send(chat_id, "Risk must be a number, for example `/risk 0.5`")
            return
        _send(chat_id, _issue(bot_id, "set_risk", "", value))
    elif command == "/closeall":
        _send(chat_id, "Close *every* open position?", _confirm_keyboard("close_all"))
    elif command == "/flatten":
        _send(chat_id, "Close everything *and pause* the bot?", _confirm_keyboard("flatten"))
    elif command == "/kill":
        _send(
            chat_id,
            "🛑 Engage the *kill switch*? This closes everything and stops the bot "
            "until you send /release.",
            _confirm_keyboard("kill"),
        )
    elif command == "/close":
        if not args:
            _send(chat_id, "Usage: `/close EURUSD`")
            return
        symbol = args[0].upper()
        _send(chat_id, f"Close all *{_escape(symbol)}* positions?",
              _confirm_keyboard("close_symbol", symbol))
    else:
        _send(chat_id, "Unknown command. Send /help.")


def _handle_callback(callback: dict) -> None:
    data = callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    callback_id = callback.get("id")

    _prune_pending()
    decision, _, token = data.partition(":")
    entry = _pending.pop(token, None)

    if entry is None:
        _call("answerCallbackQuery", callback_query_id=callback_id,
              text="This confirmation expired.")
        return

    if decision != "y":
        _call("answerCallbackQuery", callback_query_id=callback_id, text="Cancelled.")
        _send(chat_id, "Cancelled.")
        return

    _call("answerCallbackQuery", callback_query_id=callback_id, text="Sent.")
    result = _issue(_bot_id(), entry["action"], entry["symbol"], entry["value"])
    _send(chat_id, result)


# ----------------------------------------------------------------------
# Polling loop
# ----------------------------------------------------------------------
def _loop() -> None:
    offset = 0
    warned_chats: set = set()
    print("[telegram] polling started")

    while not _stop.is_set():
        try:
            updates = _call(
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as exc:
            print(f"[telegram] poll error: {exc}")
            time.sleep(5)
            continue

        if updates is None:
            time.sleep(5)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)

            callback = update.get("callback_query")
            message = update.get("message")

            if callback:
                chat_id = callback.get("message", {}).get("chat", {}).get("id")
                if chat_id in _cfg.telegram.allowed_chat_ids:
                    _handle_callback(callback)
                continue

            if not message:
                continue

            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")

            if chat_id not in _cfg.telegram.allowed_chat_ids:
                # Tell the operator once, then stay silent for this chat.
                if chat_id not in warned_chats:
                    warned_chats.add(chat_id)
                    print(f"[telegram] ignoring unauthorised chat id {chat_id}")
                    _send(
                        chat_id,
                        "This bot is private. If it is yours, add this chat id to "
                        f"`telegram.allowed_chat_ids` in config.json:\n`{chat_id}`",
                    )
                continue

            if text:
                try:
                    _handle_message(chat_id, text)
                except Exception as exc:
                    print(f"[telegram] handler error: {exc}")
                    _send(chat_id, "Something went wrong handling that command.")

    print("[telegram] polling stopped")


def start(cfg, store, validate_command) -> None:
    global _cfg, _store, _validate, _thread
    _cfg, _store, _validate = cfg, store, validate_command

    if not cfg.telegram.allowed_chat_ids:
        print(
            "[telegram] no allowed_chat_ids configured - the bot will reply once "
            "with your chat id so you can whitelist it."
        )

    _stop.clear()
    _thread = threading.Thread(target=_loop, name="apex-telegram", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
