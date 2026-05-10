"""Permission gate: Claude Code → Telegram inline buttons.

When Claude tries to use a tool that requires asking, the SDK invokes
`can_use_tool`. We send the user a Telegram message with Allow/Deny/Always
buttons, await the answer, return the result back to the SDK.
"""

import asyncio
import logging
import secrets
from typing import Any

from aiogram import Bot
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from .i18n import Translator

log = logging.getLogger(__name__)

DEFAULT_APPROVAL_TIMEOUT_SEC = 300


class TelegramPermissionGate:
    def __init__(
        self,
        bot: Bot,
        translator: Translator,
        approval_timeout_sec: int = DEFAULT_APPROVAL_TIMEOUT_SEC,
    ):
        self._bot = bot
        self._t = translator
        self._timeout = approval_timeout_sec
        # request_id -> (Future with decision, tool_name, expected_chat_id, prompt_message_id)
        # prompt_message_id is None until the inline-button message is sent
        # successfully; we use it to delete the prompt on click / timeout so
        # the chat does not pile up with stale permission requests.
        self._pending: dict[
            str, tuple[asyncio.Future[str], str, int, int | None]
        ] = {}

    async def can_use_tool(
        self,
        chat_id: int,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        t = self._t
        request_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[request_id] = (fut, tool_name, chat_id, None)

        text = self._format_request(tool_name, tool_input, ctx)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t.t("btn_allow"),
                        callback_data=f"perm:{request_id}:allow",
                    ),
                    InlineKeyboardButton(
                        text=t.t("btn_deny"),
                        callback_data=f"perm:{request_id}:deny",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=t.t("btn_always"),
                        callback_data=f"perm:{request_id}:always",
                    ),
                ],
            ]
        )

        try:
            sent = await self._bot.send_message(
                chat_id, text, reply_markup=kb, parse_mode=None
            )
        except Exception:
            log.exception("permission prompt failed")
            self._pending.pop(request_id, None)
            return PermissionResultDeny(message=t.t("permission_failed_prompt"))
        self._pending[request_id] = (fut, tool_name, chat_id, sent.message_id)

        decision = "deny"
        try:
            decision = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            # Drop the stale prompt before announcing the timeout so the
            # chat does not keep an orphaned set of buttons around.
            await self._delete_prompt(chat_id, sent.message_id)
            try:
                await self._bot.send_message(
                    chat_id, t.t("approval_timeout"), parse_mode=None
                )
            except Exception:
                pass
        finally:
            self._pending.pop(request_id, None)

        if decision == "always":
            return PermissionResultAllow(
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        behavior="allow",
                        rules=[PermissionRuleValue(tool_name=tool_name)],
                        destination="session",
                    )
                ]
            )
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message=t.t("permission_denied_via_telegram"))

    async def handle_callback(self, callback: CallbackQuery) -> None:
        t = self._t
        data = callback.data or ""
        if not data.startswith("perm:"):
            return
        try:
            _, request_id, decision = data.split(":", 2)
        except ValueError:
            await callback.answer()
            return

        msg = callback.message if isinstance(callback.message, Message) else None

        entry = self._pending.get(request_id)
        if entry is None or entry[0].done():
            await callback.answer(t.t("callback_outdated"), show_alert=False)
            # Outdated prompt — delete it so the chat does not accumulate
            # orphaned button rows.
            if msg is not None:
                try:
                    await msg.delete()
                except Exception:
                    pass
            return

        fut, _tool_name, expected_chat_id, _prompt_msg_id = entry

        # Authz: only callbacks from the chat the request was issued to are honored.
        actual_chat_id = msg.chat.id if msg is not None else None
        if actual_chat_id != expected_chat_id:
            await callback.answer(t.t("unauthorized_callback"), show_alert=True)
            return

        if decision not in {"allow", "deny", "always"}:
            await callback.answer()
            return

        fut.set_result(decision)

        verdict = t.t(f"verdict_{decision}")
        # Delete the prompt message itself — verdict goes in the callback
        # toast, so there is no need to keep the bubble.
        if msg is not None:
            try:
                await msg.delete()
            except Exception:
                log.debug("could not delete permission prompt", exc_info=True)
        await callback.answer(verdict)

    async def _delete_prompt(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id, message_id)
        except Exception:
            log.debug("could not delete permission prompt", exc_info=True)

    def _format_request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> str:
        t = self._t
        head = ctx.title or t.t("permission_request_default_title", tool=tool_name)
        parts: list[str] = [head]
        if ctx.description and ctx.description != head:
            parts.append(ctx.description)
        if ctx.decision_reason:
            parts.append(t.t("permission_request_reason", reason=ctx.decision_reason))
        if ctx.blocked_path:
            parts.append(t.t("permission_request_path", path=ctx.blocked_path))
        if tool_input:
            try:
                preview = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
            except Exception:
                preview = str(tool_input)
            if len(preview) > 800:
                preview = preview[:800] + "…"
            parts.append(t.t("permission_request_params", params=preview))
        return "\n\n".join(parts)
