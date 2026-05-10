"""Config loader: multi-bot dict, flat-legacy wrap, ACL evaluation, env fallback."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.config import BotConfig, load


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_multi_bot_dict(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "alpha": {"telegram_bot_token": "1:abc"},
            "beta": {"telegram_bot_token": "2:def"},
        },
    )
    bots = load(p)
    assert set(bots) == {"alpha", "beta"}
    assert bots["alpha"].telegram_bot_token.get_secret_value() == "1:abc"


def test_flat_legacy_format_wraps_as_default(tmp_path: Path) -> None:
    p = _write(tmp_path, {"telegram_bot_token": "1:abc"})
    bots = load(p)
    assert list(bots) == ["default"]


def test_placeholder_token_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, {"alpha": {"telegram_bot_token": "put-it-here"}})
    with pytest.raises(ValueError, match="telegram_bot_token"):
        load(p)


def test_env_fallback_for_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_ALPHA", "9:zzz")
    p = _write(tmp_path, {"alpha": {}})
    bots = load(p)
    assert bots["alpha"].telegram_bot_token.get_secret_value() == "9:zzz"


def test_acl_defaults_are_fail_closed() -> None:
    cfg = BotConfig.model_validate(
        {"name": "x", "telegram_bot_token": "1:abc"}
    )
    assert cfg.allowed_for_all is False
    assert cfg.allowed_chat_ids == ()
    assert cfg.blacklist_chat_ids == ()


def test_allowed_chat_ids_must_be_list(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {"alpha": {"telegram_bot_token": "1:abc", "allowed_chat_ids": "not-list"}},
    )
    with pytest.raises(ValueError, match="allowed_chat_ids"):
        load(p)


def test_blacklist_parses_integers(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "alpha": {
                "telegram_bot_token": "1:abc",
                "blacklist_chat_ids": [1, "2", 3],
            }
        },
    )
    bots = load(p)
    assert bots["alpha"].blacklist_chat_ids == (1, 2, 3)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        BotConfig.model_validate(
            {"name": "x", "telegram_bot_token": "1:abc", "garbage": True}
        )
