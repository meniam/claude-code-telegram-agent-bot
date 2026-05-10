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
    session_idle_ttl_sec: int = 86400
    chat_logger_capacity: int = 256
    working_dir: str | None = None
    logs_dir: str | None = None
    lang: str = "ru"
    # Voice/audio transcription via Groq. None disables the feature; the
    # voice handler then replies with `voice_disabled`.
    groq_api_key: SecretStr | None = None
    groq_model: str = "whisper-large-v3-turbo"
    groq_timeout_sec: float = 60.0
    # Reject audio longer than this (seconds). 0 disables the check.
    voice_max_duration_sec: int = 600
    # File / photo upload storage. None disables both handlers — the bot
    # responds with `upload_disabled` and the file is dropped.
    uploads_dir: str | None = None
    # Reject uploads larger than this (bytes). 0 disables the check.
    # Telegram Bot API caps downloads at 20 MB without a local Bot API server.
    upload_max_bytes: int = 20 * 1024 * 1024
    # Directory with user-defined slash commands (`*.md`). Each file becomes
    # a Telegram bot command whose body is sent to Claude as the prompt.
    # `null` / missing → no extra commands.
    commands_dir: str | None = None
    # Fail-closed access control. Evaluation order in `is_allowed`:
    #   1. `blacklist_chat_ids` — if the sender is here, deny outright.
    #      Takes priority over `allowed_for_all` and `allowed_chat_ids`.
    #   2. `allowed_for_all=True` — every non-blacklisted chat is allowed.
    #   3. otherwise — accept only chats listed in `allowed_chat_ids`.
    #      Default is the empty tuple, i.e. nobody is allowed.
    allowed_for_all: bool = False
    allowed_chat_ids: tuple[int, ...] = ()
    blacklist_chat_ids: tuple[int, ...] = ()

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

    raw_groq = (
        data.get("groq_api_key")
        or os.environ.get(f"GROQ_API_KEY_{name.upper()}")
        or os.environ.get("GROQ_API_KEY")
    )
    if raw_groq and str(raw_groq).startswith("put-"):
        raw_groq = None

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

    uploads_dir = data.get("uploads_dir")
    if uploads_dir:
        ud = Path(uploads_dir).expanduser()
        ud.mkdir(parents=True, exist_ok=True)
        uploads_dir = str(ud.resolve())

    commands_dir = data.get("commands_dir")
    if commands_dir:
        cd = Path(commands_dir).expanduser()
        if not cd.is_dir():
            raise ValueError(
                f"[{name}] commands_dir does not exist or is not a directory: {cd}"
            )
        commands_dir = str(cd.resolve())

    def _parse_chat_id_list(field: str) -> tuple[int, ...]:
        raw = data.get(field, None)
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ValueError(
                f"[{name}] {field} must be null or a list of integers"
            )
        try:
            return tuple(int(x) for x in raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"[{name}] {field} must contain integer chat IDs"
            ) from e

    allowed_chat_ids = _parse_chat_id_list("allowed_chat_ids")
    blacklist_chat_ids = _parse_chat_id_list("blacklist_chat_ids")

    raw_for_all = data.get("allowed_for_all", False)
    if not isinstance(raw_for_all, bool):
        raise ValueError(
            f"[{name}] allowed_for_all must be a boolean"
        )

    payload: dict[str, Any] = {
        "name": name,
        "telegram_bot_token": raw_token,
        "system_prompt": data.get("system_prompt"),
        "working_dir": working_dir,
        "logs_dir": logs_dir,
        "uploads_dir": uploads_dir,
        "commands_dir": commands_dir,
        "allowed_chat_ids": allowed_chat_ids,
        "blacklist_chat_ids": blacklist_chat_ids,
        "allowed_for_all": raw_for_all,
    }
    if raw_groq:
        payload["groq_api_key"] = raw_groq
    for key in (
        "draft_interval_sec",
        "approval_timeout_sec",
        "agent_timeout_sec",
        "session_idle_ttl_sec",
        "chat_logger_capacity",
        "lang",
        "groq_model",
        "groq_timeout_sec",
        "voice_max_duration_sec",
        "upload_max_bytes",
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
