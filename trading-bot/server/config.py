"""Configuration loading for the ApexAlgo control server.

Precedence, lowest to highest:
    built-in defaults  ->  config.json  ->  environment variables

Secrets are never written back to disk by the app. On first run, if no
config.json exists, one is generated with strong random values so the
operator never accidentally runs with a default password.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, asdict
from typing import List

DEFAULT_CONFIG_PATH = os.environ.get(
    "APEX_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    # Only these chat ids may issue commands. Anyone else is ignored entirely.
    allowed_chat_ids: List[int] = field(default_factory=list)
    # Which event levels trigger a push message: info / warn / error
    notify_levels: List[str] = field(default_factory=lambda: ["warn", "error"])
    notify_trades: bool = True


@dataclass
class Config:
    # Shared secret the EA sends in the X-Apex-Token header.
    bot_token: str = ""
    # Password for the browser panel.
    panel_password: str = ""
    # Flask session signing key.
    secret_key: str = ""

    host: str = "127.0.0.1"
    port: int = 8800
    db_path: str = "apex.db"

    # Bot ids allowed to report in. Empty list = accept any id (single-user setups).
    allowed_bot_ids: List[str] = field(default_factory=list)

    # Hard ceiling the panel will not let you exceed, mirroring the EA's own
    # InpMaxRiskPercent. Defence in depth: a compromised panel still cannot
    # set a catastrophic risk level.
    max_risk_percent: float = 2.0

    # A bot that has not reported in for this long is shown as offline.
    offline_after_seconds: int = 90

    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    # ------------------------------------------------------------------
    @staticmethod
    def load(path: str = DEFAULT_CONFIG_PATH) -> "Config":
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

        tg_data = data.pop("telegram", {}) or {}
        cfg = Config(**{k: v for k, v in data.items() if k in Config.__annotations__})
        cfg.telegram = TelegramConfig(
            **{k: v for k, v in tg_data.items() if k in TelegramConfig.__annotations__}
        )

        # --- environment overrides (useful for containers / systemd) ----
        cfg.bot_token = os.environ.get("APEX_BOT_TOKEN", cfg.bot_token)
        cfg.panel_password = os.environ.get("APEX_PANEL_PASSWORD", cfg.panel_password)
        cfg.secret_key = os.environ.get("APEX_SECRET_KEY", cfg.secret_key)
        cfg.host = os.environ.get("APEX_HOST", cfg.host)
        cfg.port = int(os.environ.get("APEX_PORT", cfg.port))
        cfg.db_path = os.environ.get("APEX_DB", cfg.db_path)

        if os.environ.get("APEX_TELEGRAM_TOKEN"):
            cfg.telegram.bot_token = os.environ["APEX_TELEGRAM_TOKEN"]
            cfg.telegram.enabled = True
        if os.environ.get("APEX_TELEGRAM_CHATS"):
            cfg.telegram.allowed_chat_ids = [
                int(x.strip())
                for x in os.environ["APEX_TELEGRAM_CHATS"].split(",")
                if x.strip()
            ]

        # --- generate anything still missing ---------------------------
        generated = False
        if not cfg.bot_token:
            cfg.bot_token = secrets.token_urlsafe(32)
            generated = True
        if not cfg.secret_key:
            cfg.secret_key = secrets.token_urlsafe(32)
            generated = True
        if not cfg.panel_password:
            cfg.panel_password = secrets.token_urlsafe(12)
            generated = True

        if generated and not os.path.exists(path):
            cfg.save(path)
            print("=" * 68)
            print(f"ApexAlgo: created {path} with fresh secrets.")
            print(f"  Panel password : {cfg.panel_password}")
            print(f"  EA token       : {cfg.bot_token}")
            print("  Put the EA token into the ApexAlgo EA's InpRemoteToken input.")
            print("=" * 68)

        if not os.path.isabs(cfg.db_path):
            cfg.db_path = os.path.join(os.path.dirname(os.path.abspath(path)), cfg.db_path)

        return cfg

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        data = asdict(self)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        try:
            os.chmod(path, 0o600)  # secrets live here
        except OSError:
            pass

    def bot_allowed(self, bot_id: str) -> bool:
        return (not self.allowed_bot_ids) or (bot_id in self.allowed_bot_ids)
