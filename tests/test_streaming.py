"""DraftStreamer._redact: mask the bot token before logging."""

from unittest.mock import MagicMock

from src.infra.streaming import DraftStreamer


def _streamer(token: str) -> DraftStreamer:
    return DraftStreamer(token=token, http=MagicMock())


def test_redact_replaces_token_with_stars() -> None:
    s = _streamer("123:SECRET-abc")
    out = s._redact("Connection failed for https://api.tg/bot123:SECRET-abc/x")
    assert "SECRET" not in out
    assert "***" in out


def test_redact_passthrough_when_token_absent_in_text() -> None:
    s = _streamer("123:SECRET")
    assert s._redact("nothing to mask here") == "nothing to mask here"


def test_redact_handles_empty_token() -> None:
    s = _streamer("")
    assert s._redact("body with anything") == "body with anything"


def test_redact_replaces_all_occurrences() -> None:
    s = _streamer("AAA")
    out = s._redact("AAA and AAA again AAA")
    assert "AAA" not in out
    assert out.count("***") == 3
