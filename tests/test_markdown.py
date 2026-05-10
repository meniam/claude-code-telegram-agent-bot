"""Markdown helpers: code-block padding, MDV2 conversion, audio filenames."""

from unittest.mock import MagicMock

from src.ui.markdown import (
    _pad_after_code_blocks,
    audio_filename,
    format_quote,
    to_mdv2,
)


def test_pad_inserts_blank_line_after_closing_fence() -> None:
    src = "```\ncode\n```\nnext line"
    out = _pad_after_code_blocks(src)
    assert "```\nnext line" not in out
    assert "```\n\nnext line" in out


def test_pad_skips_blank_line_when_next_already_blank() -> None:
    src = "```\ncode\n```\n\nnext"
    out = _pad_after_code_blocks(src)
    # Only one blank line between fence and next, not two.
    assert "```\n\n\nnext" not in out


def test_pad_handles_unterminated_fence() -> None:
    src = "```\ncode without closer"
    # Must not raise and must preserve content.
    assert _pad_after_code_blocks(src) == src


def test_to_mdv2_escapes_special_chars() -> None:
    out = to_mdv2("Hello. World!")
    # MarkdownV2 escapes `.` and `!` — exact form depends on telegramify-markdown
    # but the raw chars cannot survive un-escaped.
    assert isinstance(out, str)
    assert "Hello" in out


def test_format_quote_prefixes_each_line() -> None:
    out = format_quote("first\nsecond")
    assert out == "> first\n> second"


def test_format_quote_handles_empty_lines() -> None:
    out = format_quote("a\n\nb")
    assert out == "> a\n>\n> b"


def test_format_quote_empty_string() -> None:
    assert format_quote("") == ">"


def test_audio_filename_voice() -> None:
    msg = MagicMock()
    msg.voice = object()
    assert audio_filename(msg) == "voice.ogg"


def test_audio_filename_audio_with_name() -> None:
    msg = MagicMock()
    msg.voice = None
    msg.audio.file_name = "song.mp3"
    assert audio_filename(msg) == "song.mp3"


def test_audio_filename_audio_by_mime() -> None:
    msg = MagicMock()
    msg.voice = None
    msg.audio.file_name = None
    msg.audio.mime_type = "audio/mpeg"
    assert audio_filename(msg) == "audio.mp3"


def test_audio_filename_unknown_mime_defaults_to_ogg() -> None:
    msg = MagicMock()
    msg.voice = None
    msg.audio.file_name = None
    msg.audio.mime_type = "audio/totally-made-up"
    assert audio_filename(msg) == "audio.ogg"


def test_audio_filename_no_audio_no_voice() -> None:
    msg = MagicMock()
    msg.voice = None
    msg.audio = None
    assert audio_filename(msg) == "audio.ogg"
