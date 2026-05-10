import asyncio
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
    # None → open to everyone; () → closed; (..) → whitelist.
    allowed_set: set[int] | None
    allowed_set = None if cfg.allowed_chat_ids is None else set(cfg.allowed_chat_ids)

    def is_allowed(chat_id: int) -> bool:
        return allowed_set is None or chat_id in allowed_set

    if allowed_set is not None:
        glog.info(
            "[%s] access restricted to %d chat_id(s)",
            cfg.name,
            len(allowed_set),
        )

    dp = Dispatcher()
    streamer = DraftStreamer(
        cfg.telegram_bot_token.get_secret_value(),
        http,
        interval_sec=cfg.draft_interval_sec,
    )
    gate = TelegramPermissionGate(
        bot, translator=tr, approval_timeout_sec=cfg.approval_timeout_sec
    )
    agent = AgentSessionManager(
        on_permission=gate.can_use_tool,
        system_prompt=system_prompt,
        cwd=cfg.working_dir,
        idle_ttl_sec=cfg.session_idle_ttl_sec,
    )

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

    @dp.message(F.text)
    async def handle(message: Message) -> None:
        if not is_allowed(message.chat.id):
            await deny_access(message)
            return
        cl = bot_logs.for_chat(message.chat.id)
        cl.info("user: %s", message.text)
        emoji = reaction_picker.pick(message.text or "")
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception:
            glog.exception("[%s] reaction failed", cfg.name)

        await bot.send_chat_action(message.chat.id, "typing")
        try:
            chunks = agent.ask_stream(message.chat.id, message.text)
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
