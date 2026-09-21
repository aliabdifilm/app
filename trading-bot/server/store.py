"""SQLite persistence for the ApexAlgo control server.

Everything the panel shows and every command it issues lives here. SQLite is
deliberate: the whole control plane has to survive a VPS reboot without any
external service, and a single file is trivially easy to back up.

Threading note: Flask serves requests on several threads and the Telegram bot
runs on its own, so every call opens a short-lived connection instead of
sharing one. WAL mode keeps concurrent readers from blocking the writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

# Commands that were handed to a bot but never acknowledged are re-sent after
# this many seconds. A dropped HTTP response must not lose a "close all".
REDELIVER_AFTER_SECONDS = 30

# How long an equity sample is kept, and the minimum gap between samples.
EQUITY_RETENTION_DAYS = 90
EQUITY_MIN_INTERVAL_SECONDS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    bot_id      TEXT PRIMARY KEY,
    last_seen   INTEGER NOT NULL DEFAULT 0,
    paused      INTEGER NOT NULL DEFAULT 0,
    state_json  TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id     TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    symbol     TEXT    NOT NULL DEFAULT '',
    value      REAL    NOT NULL DEFAULT 0,
    created    INTEGER NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0,
    acked      INTEGER NOT NULL DEFAULT 0,
    source     TEXT    NOT NULL DEFAULT 'panel'
);
CREATE INDEX IF NOT EXISTS idx_commands_pending ON commands(bot_id, acked);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    level   TEXT    NOT NULL,
    title   TEXT    NOT NULL,
    message TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(bot_id, ts DESC);

CREATE TABLE IF NOT EXISTS equity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id   TEXT    NOT NULL,
    ts       INTEGER NOT NULL,
    balance  REAL    NOT NULL,
    equity   REAL    NOT NULL,
    drawdown REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(bot_id, ts);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Bot state
    # ------------------------------------------------------------------
    def update_bot_state(self, bot_id: str, state: Dict[str, Any]) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bots (bot_id, last_seen, paused, state_json)
                VALUES (?, ?, COALESCE((SELECT paused FROM bots WHERE bot_id = ?), 0), ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    state_json = excluded.state_json
                """,
                (bot_id, now, bot_id, json.dumps(state, ensure_ascii=False)),
            )

    def get_bot(self, bot_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT bot_id, last_seen, paused, state_json FROM bots WHERE bot_id = ?",
                (bot_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "bot_id": row["bot_id"],
            "last_seen": row["last_seen"],
            "paused": bool(row["paused"]),
            "state": json.loads(row["state_json"] or "{}"),
        }

    def list_bots(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bot_id, last_seen, paused, state_json FROM bots ORDER BY bot_id"
            ).fetchall()
        return [
            {
                "bot_id": r["bot_id"],
                "last_seen": r["last_seen"],
                "paused": bool(r["paused"]),
                "state": json.loads(r["state_json"] or "{}"),
            }
            for r in rows
        ]

    def set_paused(self, bot_id: str, paused: bool) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bots (bot_id, last_seen, paused, state_json)
                VALUES (?, ?, ?, '{}')
                ON CONFLICT(bot_id) DO UPDATE SET paused = excluded.paused
                """,
                (bot_id, now, 1 if paused else 0),
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def enqueue_command(
        self,
        bot_id: str,
        type_: str,
        symbol: str = "",
        value: float = 0.0,
        source: str = "panel",
    ) -> int:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO commands (bot_id, type, symbol, value, created, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (bot_id, type_, symbol, float(value), now, source),
            )
            command_id = int(cur.lastrowid)

        # Pause/resume also flips the authoritative flag the EA re-syncs from,
        # so the state survives an EA restart between heartbeats.
        if type_ == "pause":
            self.set_paused(bot_id, True)
        elif type_ in ("resume", "release_kill"):
            self.set_paused(bot_id, False)
        elif type_ in ("flatten", "kill"):
            self.set_paused(bot_id, True)

        return command_id

    def take_pending_commands(self, bot_id: str) -> List[Dict[str, Any]]:
        """Return commands to send, marking them delivered.

        Commands already delivered but not yet acknowledged are returned again
        once REDELIVER_AFTER_SECONDS has passed; the EA is idempotent for every
        command type, so a duplicate is harmless while a lost one is not.
        """
        now = int(time.time())
        cutoff = now - REDELIVER_AFTER_SECONDS
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, type, symbol, value FROM commands
                WHERE bot_id = ? AND acked = 0 AND (delivered = 0 OR delivered < ?)
                ORDER BY id
                LIMIT 20
                """,
                (bot_id, cutoff),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                conn.execute(
                    "UPDATE commands SET delivered = ? WHERE id IN (%s)"
                    % ",".join("?" * len(ids)),
                    [now] + ids,
                )
        return [
            {"id": int(r["id"]), "type": r["type"], "symbol": r["symbol"], "value": r["value"]}
            for r in rows
        ]

    def ack_commands(self, bot_id: str, ids: Iterable[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE commands SET acked = ? WHERE bot_id = ? AND id IN (%s)"
                % ",".join("?" * len(ids)),
                [now, bot_id] + ids,
            )
            return cur.rowcount

    def recent_commands(self, bot_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, type, symbol, value, created, delivered, acked, source
                FROM commands WHERE bot_id = ? ORDER BY id DESC LIMIT ?
                """,
                (bot_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def add_event(self, bot_id: str, level: str, title: str, message: str, ts: int = 0) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (bot_id, ts, level, title, message) VALUES (?, ?, ?, ?, ?)",
                (bot_id, ts or int(time.time()), level, title, message),
            )

    def recent_events(self, bot_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, level, title, message FROM events
                WHERE bot_id = ? ORDER BY id DESC LIMIT ?
                """,
                (bot_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------
    def record_equity(self, bot_id: str, balance: float, equity: float, drawdown: float) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            last = conn.execute(
                "SELECT ts FROM equity WHERE bot_id = ? ORDER BY ts DESC LIMIT 1", (bot_id,)
            ).fetchone()
            if last and now - int(last["ts"]) < EQUITY_MIN_INTERVAL_SECONDS:
                return
            conn.execute(
                "INSERT INTO equity (bot_id, ts, balance, equity, drawdown) VALUES (?, ?, ?, ?, ?)",
                (bot_id, now, float(balance), float(equity), float(drawdown)),
            )
            conn.execute(
                "DELETE FROM equity WHERE bot_id = ? AND ts < ?",
                (bot_id, now - EQUITY_RETENTION_DAYS * 86400),
            )

    def equity_series(self, bot_id: str, hours: int = 48, max_points: int = 400):
        since = int(time.time()) - hours * 3600
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, balance, equity, drawdown FROM equity "
                "WHERE bot_id = ? AND ts >= ? ORDER BY ts",
                (bot_id, since),
            ).fetchall()

        points = [dict(r) for r in rows]
        if len(points) <= max_points:
            return points
        # Even decimation keeps the shape of the curve without shipping
        # thousands of points to a phone on mobile data.
        step = len(points) / max_points
        return [points[int(i * step)] for i in range(max_points)]
