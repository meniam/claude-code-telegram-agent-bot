"""BotLogs: per-chat LRU eviction closes file handlers."""

import logging
from pathlib import Path

from src.infra.logs import BotLogs


def test_for_chat_creates_logger_with_file_handler(tmp_path: Path) -> None:
    logs = BotLogs(name="bot1", base_dir=tmp_path)
    log = logs.for_chat(42)
    log.info("hello")
    for h in log.handlers:
        h.flush()
    assert (tmp_path / "42.log").exists()


def test_for_chat_returns_same_logger_on_second_call(tmp_path: Path) -> None:
    logs = BotLogs(name="bot1", base_dir=tmp_path)
    a = logs.for_chat(1)
    b = logs.for_chat(1)
    assert a is b


def test_no_base_dir_returns_noop_logger() -> None:
    logs = BotLogs(name="bot2", base_dir=None)
    log = logs.for_chat(1)
    # Should be silent (NullHandler) and shared across calls.
    assert log is logs.for_chat(2)


def test_lru_evicts_oldest_when_capacity_exceeded(tmp_path: Path) -> None:
    logs = BotLogs(name="bot3", base_dir=tmp_path, capacity=2)
    log1 = logs.for_chat(1)
    log2 = logs.for_chat(2)
    # Touching chat 1 moves it to the end of the LRU.
    logs.for_chat(1)
    # Adding chat 3 should evict chat 2 (least recently used).
    logs.for_chat(3)
    # Evicted logger's handlers should be closed.
    assert log2.handlers == []
    # log1 must still have its handler.
    assert log1.handlers


def test_general_logger_writes_bot_log(tmp_path: Path) -> None:
    logs = BotLogs(name="bot4", base_dir=tmp_path)
    logs.general.info("startup")
    for h in logs.general.handlers:
        h.flush()
    assert (tmp_path / "bot.log").exists()


def _cleanup_loggers(prefix: str) -> None:
    """Remove dynamically-created loggers so tests don't leak across runs."""
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(prefix):
            logging.Logger.manager.loggerDict.pop(name, None)


def test_module_cleanup_after_run() -> None:
    _cleanup_loggers("bot.")
