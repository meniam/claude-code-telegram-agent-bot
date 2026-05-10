"""Markdown → Telegram MarkdownV2 conversion, chunked send, audio filename.

Pure helpers — no I/O state, no closures over per-bot config. Reusable
across bots.
"""

import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from telegramify_markdown import markdownify

log = logging.getLogger(__name__)

TG_LIMIT = 4000


def _pad_after_code_blocks(text: str) -> str:
    """Ensures a blank line after the closing ``` fence."""
    lines = text.split("\n")
    out: list[str] = []
    in_block = False
    for i, line in enumerate(lines):
        out.append(line)
        if line.lstrip().startswith("```"):
            if in_block:
                in_block = False
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                if next_line.strip():
                    out.append("")
            else:
                in_block = True
    return "\n".join(out)


def to_mdv2(text: str) -> str:
    out: str = markdownify(_pad_after_code_blocks(text))
    return out


async def send_md(message: Message, text: str) -> None:
    """Sends Markdown as Telegram MarkdownV2 in chunks of ≤ TG_LIMIT.

    Falls back to plain text for any chunk that the parser rejects.
    """
    bot = message.bot
    if bot is None:
        # Should not happen — aiogram always binds a Bot to inbound messages.
        return
    await send_md_to_chat(bot, message.chat.id, text)


async def send_md_to_chat(bot: Bot, chat_id: int, text: str) -> None:
    """Chat-id-bound twin of `send_md` — used for bot-initiated messages
    (hooks, notifications) that have no inbound `Message` to reply to.

    Tries MarkdownV2 first; on the first parse failure dumps the *original*
    body in plain text. Falling back to the converted (escape-laden)
    string would surface backslashes and `\\.` artifacts to the user.
    """
    converted = to_mdv2(text)
    sent_any = False
    for i in range(0, len(converted), TG_LIMIT):
        chunk = converted[i : i + TG_LIMIT]
        try:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.MARKDOWN_V2)
            sent_any = True
        except TelegramBadRequest:
            log.warning(
                "send_md_to_chat: MarkdownV2 rejected for chat_id=%s — "
                "falling back to plain text (sent_any=%s)",
                chat_id,
                sent_any,
            )
            for j in range(0, len(text), TG_LIMIT):
                await bot.send_message(
                    chat_id, text[j : j + TG_LIMIT], parse_mode=None
                )
            return


_AUDIO_EXT_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}


def audio_filename(message: Message) -> str:
    """Pick a filename with an extension Groq can dispatch by."""
    if message.voice is not None:
        return "voice.ogg"
    audio = message.audio
    if audio is not None:
        if audio.file_name:
            return audio.file_name
        ext = _AUDIO_EXT_BY_MIME.get((audio.mime_type or "").lower(), ".ogg")
        return f"audio{ext}"
    return "audio.ogg"


def format_quote(text: str) -> str:
    """Wrap each line with a Markdown blockquote prefix."""
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)
