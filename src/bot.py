import asyncio
import io
import logging
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message, ReactionTypeEmoji
from telegramify_markdown import markdownify

from .agent import AgentSessionManager
from .config import BotConfig, load as load_config
from .i18n import Translator
from .logs import BotLogs, setup_console
from .permissions import TelegramPermissionGate
from .reactions import ReactionPicker
from .streaming import DraftStreamer
from .transcribe import GroqTranscriber, TranscriptionError
from .uploads import PendingFile, UploadStore, format_attachment_prompt

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
    return markdownify(_pad_after_code_blocks(text))


async def send_md(message: Message, text: str) -> None:
    """Sends Markdown as Telegram MarkdownV2 in chunks of ≤ TG_LIMIT.

    Falls back to plain text for any chunk that the parser rejects.
    """
    converted = to_mdv2(text)
    for i in range(0, len(converted), TG_LIMIT):
        chunk = converted[i : i + TG_LIMIT]
        try:
            await message.answer(chunk, parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None)


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


def _audio_filename(message: Message) -> str:
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


def _format_quote(text: str) -> str:
    """Wrap each line with a Markdown blockquote prefix."""
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


async def run_bot(cfg: BotConfig, http: aiohttp.ClientSession) -> None:
    bot = Bot(
        token=cfg.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.MARKDOWN_V2,
            link_preview_is_disabled=True,
        ),
    )
    me = await bot.get_me()
    bot_username = me.username or f"bot_{me.id}"

    bot_log_dir: Path | None = None
    if cfg.logs_dir:
        bot_log_dir = Path(cfg.logs_dir) / cfg.name
    bot_logs = BotLogs(
        name=cfg.name,
        base_dir=bot_log_dir,
        capacity=cfg.chat_logger_capacity,
    )
    glog = bot_logs.general

    glog.info("[%s] starting as @%s", cfg.name, bot_username)
    glog.info("[%s] lang: %s", cfg.name, cfg.lang)
    if cfg.working_dir:
        glog.info("[%s] working_dir: %s", cfg.name, cfg.working_dir)
    if bot_log_dir:
        glog.info("[%s] logs: %s", cfg.name, bot_log_dir)

    tr = Translator(cfg.lang)
    system_prompt = cfg.system_prompt or tr.t("default_system_prompt")
    reaction_picker = ReactionPicker.from_translator(tr)
    # Fail-closed gate. Order: blacklist → allowed_for_all → whitelist.
    allowed_set: set[int] = set(cfg.allowed_chat_ids)
    blacklist_set: set[int] = set(cfg.blacklist_chat_ids)

    def is_allowed(chat_id: int) -> bool:
        if chat_id in blacklist_set:
            return False
        if cfg.allowed_for_all:
            return True
        return chat_id in allowed_set

    if cfg.allowed_for_all:
        glog.warning("[%s] access: OPEN TO EVERYONE (allowed_for_all=true)", cfg.name)
    else:
        glog.info(
            "[%s] access restricted to %d chat_id(s)",
            cfg.name,
            len(allowed_set),
        )
    if blacklist_set:
        glog.info("[%s] blacklist: %d chat_id(s)", cfg.name, len(blacklist_set))

    dp = Dispatcher()
    streamer = DraftStreamer(
        cfg.telegram_bot_token.get_secret_value(),
        http,
        interval_sec=cfg.draft_interval_sec,
    )
    gate = TelegramPermissionGate(
        bot, translator=tr, approval_timeout_sec=cfg.approval_timeout_sec
    )
    add_dirs: list[str] = []
    if cfg.uploads_dir:
        add_dirs.append(cfg.uploads_dir)
    agent = AgentSessionManager(
        on_permission=gate.can_use_tool,
        system_prompt=system_prompt,
        cwd=cfg.working_dir,
        idle_ttl_sec=cfg.session_idle_ttl_sec,
        add_dirs=add_dirs,
    )

    transcriber: GroqTranscriber | None = None
    if cfg.groq_api_key is not None:
        transcriber = GroqTranscriber(
            http,
            api_key=cfg.groq_api_key.get_secret_value(),
            model=cfg.groq_model,
            timeout_sec=cfg.groq_timeout_sec,
        )
        glog.info("[%s] groq transcription enabled (model=%s)", cfg.name, cfg.groq_model)

    uploads: UploadStore | None = None
    if cfg.uploads_dir:
        uploads = UploadStore(Path(cfg.uploads_dir))
        glog.info("[%s] uploads enabled at %s", cfg.name, uploads.base_dir)

    # Per-album debounce: a single Telegram album arrives as N separate
    # `photo` / `document` updates with the same `media_group_id`. We hold a
    # short timer per media_group_id, restarting it on every arrival, so the
    # agent fires once after the album is fully delivered. Captions can land
    # on any one of the album's items — collect them here.
    album_timers: dict[str, asyncio.Task[None]] = {}
    album_captions: dict[str, str] = {}
    ALBUM_DEBOUNCE_SEC = 1.5

    async def deny_access(message: Message) -> None:
        bot_logs.for_chat(message.chat.id).warning(
            "access denied for chat_id=%s user=%s",
            message.chat.id,
            message.from_user.id if message.from_user else None,
        )
        await send_md(message, tr.t("access_denied", chat_id=message.chat.id))

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        await send_md(message, tr.t("start_greeting"))

    @dp.message(Command("new"))
    async def new_session(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        await agent.reset(message.chat.id)
        bot_logs.for_chat(message.chat.id).info("session reset by /new")
        await send_md(message, tr.t("new_session_confirmation"))

    @dp.callback_query(F.data.startswith("perm:"))
    async def permission_callback(callback: CallbackQuery) -> None:
        await gate.handle_callback(callback)

    async def react_to(message: Message, text: str) -> None:
        emoji = reaction_picker.pick(text or "")
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception:
            glog.exception("[%s] reaction failed", cfg.name)

    async def reply_with_agent(
        message: Message, prompt: str, cl: logging.Logger
    ) -> None:
        if uploads is not None:
            pending = uploads.pop_pending(message.chat.id)
            if pending:
                cl.info(
                    "draining %d pending upload(s): %s",
                    len(pending),
                    ", ".join(str(p.path) for p in pending),
                )
                prompt = format_attachment_prompt(pending, prompt)
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            chunks = agent.ask_stream(message.chat.id, prompt)
            answer = await asyncio.wait_for(
                streamer.stream(message.chat.id, chunks),
                timeout=cfg.agent_timeout_sec,
            )
        except asyncio.TimeoutError:
            glog.warning("[%s] agent timeout (chat_id=%s)", cfg.name, message.chat.id)
            cl.warning("agent timeout after %ss", cfg.agent_timeout_sec)
            await send_md(
                message, tr.t("agent_timeout", seconds=cfg.agent_timeout_sec)
            )
            return
        except Exception as e:
            glog.exception("[%s] agent error", cfg.name)
            cl.exception("agent error: %s", e)
            await send_md(
                message, tr.t("error_internal", error=type(e).__name__)
            )
            return
        final = answer.strip() or tr.t("empty_answer")
        cl.info("bot: %s", final)
        await send_md(message, final)

    @dp.message(F.text)
    async def handle(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        cl.info("user: %s", message.text)
        await react_to(message, message.text)
        await reply_with_agent(message, message.text, cl)

    @dp.message(F.voice | F.audio)
    async def handle_voice(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        media = message.voice or message.audio
        if media is None:
            return
        cl.info(
            "voice: file_id=%s duration=%ss mime=%s",
            media.file_id,
            getattr(media, "duration", None),
            getattr(media, "mime_type", None),
        )
        if transcriber is None:
            await send_md(message, tr.t("voice_disabled"))
            return
        duration = getattr(media, "duration", 0) or 0
        if cfg.voice_max_duration_sec > 0 and duration > cfg.voice_max_duration_sec:
            await send_md(
                message, tr.t("voice_too_long", seconds=cfg.voice_max_duration_sec)
            )
            return

        await bot.send_chat_action(message.chat.id, "typing")
        try:
            buf = io.BytesIO()
            await bot.download(media.file_id, destination=buf)
            audio_bytes = buf.getvalue()
            transcript = await transcriber.transcribe(
                audio_bytes,
                filename=_audio_filename(message),
            )
        except (TranscriptionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            glog.warning("[%s] transcription failed: %s", cfg.name, e)
            cl.warning("transcription failed: %s", e)
            await send_md(message, tr.t("voice_error", error=str(e)[:200]))
            return
        except Exception as e:
            glog.exception("[%s] transcription error", cfg.name)
            cl.exception("transcription error: %s", e)
            await send_md(message, tr.t("voice_error", error=type(e).__name__))
            return

        if not transcript:
            await send_md(message, tr.t("voice_empty"))
            return

        cl.info("transcript: %s", transcript)
        await send_md(
            message,
            f"{tr.t('voice_recognized')}:\n{_format_quote(transcript)}",
        )
        await react_to(message, transcript)
        await reply_with_agent(message, transcript, cl)

    async def _save_upload(
        message: Message,
        file_id: str,
        original_name: str,
        kind: str,
        cl: logging.Logger,
        size_hint: int | None,
    ) -> PendingFile | None:
        """Download a Telegram file into the per-chat uploads dir.

        Returns the resulting PendingFile or None on a handled error
        (caller has already replied to the user)."""
        assert uploads is not None  # caller checked
        if (
            cfg.upload_max_bytes > 0
            and size_hint is not None
            and size_hint > cfg.upload_max_bytes
        ):
            await send_md(
                message,
                tr.t(
                    "upload_too_large",
                    size_mb=size_hint / 1024 / 1024,
                    limit_mb=cfg.upload_max_bytes / 1024 / 1024,
                ),
            )
            return None
        path = uploads.build_path(message.chat.id, file_id, original_name)
        try:
            with path.open("wb") as f:
                await bot.download(file_id, destination=f)
        except Exception as e:
            glog.exception("[%s] upload download failed", cfg.name)
            cl.exception("upload download failed: %s", e)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            await send_md(message, tr.t("upload_error", error=type(e).__name__))
            return None
        cl.info(
            "upload saved: kind=%s name=%s path=%s size=%s",
            kind,
            original_name,
            path,
            path.stat().st_size,
        )
        return PendingFile(path=path, kind=kind, name=original_name)

    async def _fire_for_upload(message: Message, cl: logging.Logger) -> None:
        """Fire the agent on the just-saved upload.

        Single message → fire immediately. Album (`media_group_id` set) →
        debounce: every additional album item resets the timer, so the
        agent runs once after the album is fully delivered. Caption from
        any item in the album is preserved.
        """
        caption = (message.caption or "").strip()
        mg = message.media_group_id
        if not mg:
            await react_to(message, caption)
            await reply_with_agent(message, caption, cl)
            return
        if caption:
            album_captions[mg] = caption
        existing = album_timers.get(mg)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _delayed() -> None:
            try:
                await asyncio.sleep(ALBUM_DEBOUNCE_SEC)
            except asyncio.CancelledError:
                return
            album_timers.pop(mg, None)
            cap = album_captions.pop(mg, "")
            cl.info("album %s: firing with caption=%r", mg, cap)
            try:
                await react_to(message, cap)
                await reply_with_agent(message, cap, cl)
            except Exception:
                glog.exception("[%s] album fire failed", cfg.name)

        album_timers[mg] = asyncio.create_task(_delayed())

    @dp.message(F.photo)
    async def handle_photo(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        if uploads is None:
            await send_md(message, tr.t("upload_disabled"))
            return
        photo = message.photo[-1]  # largest available size
        cl.info(
            "photo: file_id=%s size=%sx%s bytes=%s media_group=%s",
            photo.file_id,
            photo.width,
            photo.height,
            photo.file_size,
            message.media_group_id,
        )
        item = await _save_upload(
            message,
            photo.file_id,
            "photo.jpg",
            "image",
            cl,
            photo.file_size,
        )
        if item is None:
            return
        uploads.add_pending(message.chat.id, item)
        await _fire_for_upload(message, cl)

    @dp.message(F.document)
    async def handle_document(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        if uploads is None:
            await send_md(message, tr.t("upload_disabled"))
            return
        doc = message.document
        if doc is None:
            return
        original_name = doc.file_name or "document"
        cl.info(
            "document: file_id=%s name=%s mime=%s size=%s media_group=%s",
            doc.file_id,
            original_name,
            doc.mime_type,
            doc.file_size,
            message.media_group_id,
        )
        item = await _save_upload(
            message,
            doc.file_id,
            original_name,
            "document",
            cl,
            doc.file_size,
        )
        if item is None:
            return
        uploads.add_pending(message.chat.id, item)
        await _fire_for_upload(message, cl)

    @dp.message(F.sticker)
    async def handle_sticker(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        if uploads is None:
            await send_md(message, tr.t("upload_disabled"))
            return
        sticker = message.sticker
        if sticker is None:
            return
        if sticker.is_animated:
            ext, kind = ".tgs", "binary (animated sticker, Lottie JSON)"
        elif sticker.is_video:
            ext, kind = ".webm", "binary (video sticker)"
        else:
            # Static stickers are plain WebP images — Claude can Read them.
            ext, kind = ".webp", "image"
        name = f"sticker_{sticker.set_name or 'unknown'}{ext}"
        cl.info(
            "sticker: file_id=%s set=%s emoji=%s kind=%s size=%s",
            sticker.file_id,
            sticker.set_name,
            sticker.emoji,
            kind,
            sticker.file_size,
        )
        item = await _save_upload(
            message,
            sticker.file_id,
            name,
            kind,
            cl,
            sticker.file_size,
        )
        if item is None:
            return
        uploads.add_pending(message.chat.id, item)
        await _fire_for_upload(message, cl)

    await bot.set_my_commands([
        BotCommand(command="start", description=tr.t("bot_command_start")),
        BotCommand(command="new", description=tr.t("bot_command_new")),
    ])

    try:
        await dp.start_polling(bot)
    finally:
        glog.info("[%s] shutting down", cfg.name)
        await agent.close_all()
        await bot.session.close()


async def _supervise(cfg: BotConfig, http: aiohttp.ClientSession) -> None:
    """Run a bot with exponential backoff on crashes (1s -> 60s)."""
    backoff = 1.0
    while True:
        try:
            await run_bot(cfg, http)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception(
                "[%s] crashed, restarting in %.1fs", cfg.name, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def main() -> None:
    setup_console()
    bots = load_config()
    logging.info("loaded %d bot(s): %s", len(bots), ", ".join(bots.keys()))

    http = aiohttp.ClientSession()
    try:
        await asyncio.gather(
            *(_supervise(cfg, http) for cfg in bots.values()),
            return_exceptions=True,
        )
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
