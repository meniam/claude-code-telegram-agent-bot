"""Loads settings from config.json.

Format: top-level is a `<internal_bot_name>: BotConfig` dict.
Every bot can have its own token / working_dir / logs_dir.
"""

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class BotConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # internal name taken from the key in config.json
    telegram_bot_token: SecretStr
    # If None, bot.py falls back to translation key `default_system_prompt`.
    system_prompt: str | None = None
    draft_interval_sec: float = 0.2
    approval_timeout_sec: int = 300
    agent_timeout_sec: int = 180
    session_idle_ttl_sec: int = 3600
    chat_logger_capacity: int = 256
    working_dir: str | None = None
    logs_dir: str | None = None
    lang: str = "ru"
    # Whitelist of Telegram chat IDs allowed to talk to the bot.
    # None / missing  → no restriction (open to everyone).
    # ()              → closed to everyone (whitelist explicitly empty).
    # (id, id, ...)   → only listed chats are allowed.
    allowed_chat_ids: tuple[int, ...] | None = None

    @field_validator("lang", mode="before")
    @classmethod
    def _lang_lower(cls, v: Any) -> Any:
        return str(v).lower() if v is not None else v


def _build(name: str, data: dict) -> BotConfig:
    raw_token = data.get("telegram_bot_token") or os.environ.get(
        f"TELEGRAM_BOT_TOKEN_{name.upper()}", ""
    )
    if not raw_token or raw_token.startswith("put-"):
        raise ValueError(
            f"[{name}] telegram_bot_token is missing (or still a placeholder)."
        )

    working_dir = data.get("working_dir")
    if working_dir:
        wd = Path(working_dir).expanduser()
        if not wd.is_dir():
            raise ValueError(
                f"[{name}] working_dir does not exist or is not a directory: {wd}"
            )
        working_dir = str(wd.resolve())

    logs_dir = data.get("logs_dir")
    if logs_dir:
        ld = Path(logs_dir).expanduser()
        ld.mkdir(parents=True, exist_ok=True)
        logs_dir = str(ld.resolve())

    raw_allowed = data.get("allowed_chat_ids", None)
    allowed_chat_ids: tuple[int, ...] | None
    if raw_allowed is None:
        allowed_chat_ids = None
    elif isinstance(raw_allowed, list):
        try:
            allowed_chat_ids = tuple(int(x) for x in raw_allowed)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"[{name}] allowed_chat_ids must contain integer chat IDs"
            ) from e
    else:
        raise ValueError(
            f"[{name}] allowed_chat_ids must be null or a list of integers"
        )

    payload: dict[str, Any] = {
        "name": name,
        "telegram_bot_token": raw_token,
        "system_prompt": data.get("system_prompt"),
        "working_dir": working_dir,
        "logs_dir": logs_dir,
        "allowed_chat_ids": allowed_chat_ids,
    }
    for key in (
        "draft_interval_sec",
        "approval_timeout_sec",
        "agent_timeout_sec",
        "session_idle_ttl_sec",
        "chat_logger_capacity",
        "lang",
    ):
        if key in data:
            payload[key] = data[key]

    return BotConfig.model_validate(payload)


def load(path: Path | str = CONFIG_PATH) -> dict[str, BotConfig]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Copy config.example.json → config.json and fill in telegram_bot_token."
        )
    with p.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or not data:
        raise ValueError("config.json is empty or not an object")

    # Backward compat: if the top level has telegram_bot_token directly,
    # this is the flat (single-bot) format — wrap it under the name "default".
    if "telegram_bot_token" in data:
        return {"default": _build("default", data)}

    bots = {name: _build(name, cfg) for name, cfg in data.items()}
    if not bots:
        raise ValueError("config.json has no bot entries")
    return bots
