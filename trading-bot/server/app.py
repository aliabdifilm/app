"""ApexAlgo control server.

Sits between the Expert Advisor (which runs on a Windows PC or VPS) and the
people who want to watch or steer it from a phone or a browser.

    MT5 + EA  --POST /api/heartbeat-->  this server  <--HTTPS--  phone/browser
              <--commands in reply---                <--------   Telegram

The EA only ever makes OUTBOUND requests, so the machine holding the money
never needs an open inbound port.

Security model
    * EA  -> server : shared bearer token in the X-Apex-Token header.
    * user-> server : password login, signed session cookie.
    * Both are useless without TLS. Put this behind a reverse proxy with a
      real certificate (or a Cloudflare / Tailscale tunnel) before exposing
      it to the internet. See docs/03_INSTALL_VPS.md.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from config import Config
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))

# Commands the panel and Telegram are allowed to issue. Anything else is
# rejected before it reaches the database, so a malformed client cannot
# smuggle an unknown instruction through to the EA.
ALLOWED_COMMANDS = {
    "pause",
    "resume",
    "close_all",
    "close_symbol",
    "flatten",
    "set_risk",
    "kill",
    "release_kill",
    "ping",
}

# Commands that can lose money or stop the bot need a confirmation flag from
# the client. This is what stops a mis-tap on a phone from flattening a book.
DESTRUCTIVE_COMMANDS = {"close_all", "close_symbol", "flatten", "kill"}

cfg = Config.load()
store = Store(cfg.db_path)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = cfg.secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set APEX_INSECURE_COOKIES=1 only for local http:// testing.
    SESSION_COOKIE_SECURE=os.environ.get("APEX_INSECURE_COOKIES") != "1",
    PERMANENT_SESSION_LIFETIME=7 * 24 * 3600,
    MAX_CONTENT_LENGTH=512 * 1024,
)

# Optional hook installed by telegram_bot.py
notifier: Optional[Callable[[str, str, str, str], None]] = None

# Naive in-process login throttle. Good enough for a single-operator panel.
_login_attempts: Dict[str, List[float]] = {}
_LOGIN_WINDOW = 300.0
_LOGIN_MAX = 8


# ----------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------
def _client_ip() -> str:
    # Trust X-Forwarded-For only when explicitly told we sit behind a proxy.
    if os.environ.get("APEX_BEHIND_PROXY") == "1":
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _login_throttled(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= _LOGIN_MAX


def _record_login_failure(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def require_bot_token(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Apex-Token", "")
        if not token or not secrets.compare_digest(token, cfg.bot_token):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def require_panel(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def _resolve_bot_id() -> str:
    """Pick which bot the panel is looking at."""
    requested = request.args.get("bot") or request.form.get("bot")
    if requested:
        return requested
    bots = store.list_bots()
    return bots[0]["bot_id"] if bots else "apex-1"


# ----------------------------------------------------------------------
# EA-facing endpoints
# ----------------------------------------------------------------------
@app.post("/api/heartbeat")
@require_bot_token
def heartbeat():
    """Receive the EA's state, return any commands waiting for it."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    bot_id = str(payload.get("bot_id") or request.headers.get("X-Apex-Bot") or "apex-1")
    if not cfg.bot_allowed(bot_id):
        return jsonify({"ok": False, "error": "unknown bot_id"}), 403

    # The EA reports which command ids it has already executed.
    ack = payload.get("ack") or []
    if isinstance(ack, list) and ack:
        store.ack_commands(bot_id, [int(a) for a in ack if isinstance(a, (int, float))])

    store.update_bot_state(bot_id, payload)

    try:
        store.record_equity(
            bot_id,
            float(payload.get("balance", 0.0)),
            float(payload.get("equity", 0.0)),
            float(payload.get("drawdown_pct", 0.0)),
        )
    except (TypeError, ValueError):
        pass  # a malformed sample must never break the heartbeat

    bot = store.get_bot(bot_id) or {}
    commands = store.take_pending_commands(bot_id)

    return jsonify(
        {
            "ok": True,
            "server_time": int(time.time()),
            "paused": bool(bot.get("paused", False)),
            "commands": commands,
        }
    )


@app.post("/api/event")
@require_bot_token
def event():
    """One-way notification from the EA (entry, exit, limit breached)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid json"}), 400

    bot_id = str(payload.get("bot_id") or "apex-1")
    if not cfg.bot_allowed(bot_id):
        return jsonify({"ok": False, "error": "unknown bot_id"}), 403

    level = str(payload.get("level", "info"))[:16]
    title = str(payload.get("title", ""))[:120]
    message = str(payload.get("message", ""))[:600]

    store.add_event(bot_id, level, title, message, int(payload.get("ts", 0) or 0))

    if notifier is not None:
        try:
            notifier(bot_id, level, title, message)
        except Exception as exc:  # a broken notifier must not break the bot
            app.logger.warning("notifier failed: %s", exc)

    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# Panel endpoints
# ----------------------------------------------------------------------
@app.get("/login")
def login():
    return render_template("login.html", error=request.args.get("error"))


@app.post("/login")
def do_login():
    ip = _client_ip()
    if _login_throttled(ip):
        return render_template("login.html", error="too_many"), 429

    password = request.form.get("password", "")
    if secrets.compare_digest(password, cfg.panel_password):
        session.permanent = True
        session["auth"] = True
        nxt = request.args.get("next") or request.form.get("next") or "/"
        # never redirect off-site
        if not nxt.startswith("/"):
            nxt = "/"
        return redirect(nxt)

    _record_login_failure(ip)
    return render_template("login.html", error="bad"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@require_panel
def dashboard():
    return render_template("dashboard.html", max_risk=cfg.max_risk_percent)


@app.get("/api/state")
@require_panel
def api_state():
    bots = store.list_bots()
    now = int(time.time())

    for bot in bots:
        bot["online"] = (now - bot["last_seen"]) <= cfg.offline_after_seconds
        bot["seconds_since_seen"] = max(0, now - bot["last_seen"])

    bot_id = _resolve_bot_id()
    current = next((b for b in bots if b["bot_id"] == bot_id), None)
    if current is None and bots:
        current = bots[0]
        bot_id = current["bot_id"]

    return jsonify(
        {
            "ok": True,
            "server_time": now,
            "bot_id": bot_id,
            "bots": [
                {
                    "bot_id": b["bot_id"],
                    "online": b["online"],
                    "paused": b["paused"],
                }
                for b in bots
            ],
            "current": current,
            "events": store.recent_events(bot_id, 40) if current else [],
            "commands": store.recent_commands(bot_id, 12) if current else [],
            "max_risk_percent": cfg.max_risk_percent,
            "offline_after_seconds": cfg.offline_after_seconds,
        }
    )


@app.get("/api/history")
@require_panel
def api_history():
    bot_id = _resolve_bot_id()
    try:
        hours = max(1, min(24 * 30, int(request.args.get("hours", 48))))
    except ValueError:
        hours = 48
    return jsonify({"ok": True, "bot_id": bot_id, "points": store.equity_series(bot_id, hours)})


@app.post("/api/command")
@require_panel
def api_command():
    body = request.get_json(silent=True) or {}
    bot_id = str(body.get("bot") or _resolve_bot_id())
    type_ = str(body.get("type", "")).lower().strip()
    symbol = str(body.get("symbol", "")).strip()[:32]
    value = body.get("value", 0.0)

    ok, error, value = validate_command(type_, symbol, value, bool(body.get("confirm")))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    command_id = store.enqueue_command(bot_id, type_, symbol, value, source="panel")
    store.add_event(
        bot_id,
        "warn" if type_ in DESTRUCTIVE_COMMANDS else "info",
        "Command queued",
        f"{type_} {symbol} {value}".strip(),
    )
    return jsonify({"ok": True, "id": command_id})


def validate_command(type_: str, symbol: str, value: Any, confirmed: bool):
    """Shared by the HTTP panel and the Telegram bot."""
    if type_ not in ALLOWED_COMMANDS:
        return False, "unknown command", 0.0

    if type_ in DESTRUCTIVE_COMMANDS and not confirmed:
        return False, "confirmation required", 0.0

    if type_ == "close_symbol" and not symbol:
        return False, "symbol required", 0.0

    if type_ == "set_risk":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, "risk must be a number", 0.0
        if value <= 0:
            return False, "risk must be greater than zero", 0.0
        if value > cfg.max_risk_percent:
            return False, f"risk above the {cfg.max_risk_percent}% server ceiling", 0.0
        return True, "", value

    return True, "", 0.0


# ----------------------------------------------------------------------
# PWA plumbing and health
# ----------------------------------------------------------------------
@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(
        os.path.join(HERE, "static"), "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    # A service worker may only control paths at or below its own URL, so it
    # has to be served from the site root rather than from /static/.
    response = send_from_directory(os.path.join(HERE, "static", "js"), "sw.js",
                                   mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": int(time.time())})


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'",
    )
    return response


def create_app() -> Flask:
    """Entry point for WSGI servers (gunicorn / waitress)."""
    return app


def main() -> None:
    if cfg.telegram.enabled and cfg.telegram.bot_token:
        try:
            import telegram_bot

            telegram_bot.start(cfg, store, validate_command)
            globals()["notifier"] = telegram_bot.notify
            print("[apex] telegram bridge started")
        except Exception as exc:
            print(f"[apex] telegram bridge disabled: {exc}")

    print(f"[apex] control server on http://{cfg.host}:{cfg.port}")
    print(f"[apex] database: {cfg.db_path}")
    if cfg.host not in ("127.0.0.1", "localhost"):
        print("[apex] WARNING: binding to a public interface. Put TLS in front of this.")

    app.run(host=cfg.host, port=cfg.port, threaded=True)


if __name__ == "__main__":
    main()
