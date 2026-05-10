"""Mirror Claude SDK tool lifecycle events into the Telegram chat.

`pre` fires for every tool — we skip the ones whose UX is already provided by
the interaction gate. `post` fires only for the small set of tools whose tail
output is actually useful (Monitor, TaskOutput).
"""

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from ..i18n import Translator
from ..infra.logs import BotLogs
from .markdown import TG_LIMIT

log = logging.getLogger(__name__)

# Tools whose UX is already provided by the interaction gate — skip the
# generic pre-tool announcement so the chat does not see two notices.
_GATE_HANDLED_TOOLS = frozenset({
    "AskUserQuestion",
    "ExitPlanMode",
    "PushNotification",
})

# Per-tool preferred input field for the brief status line. Tools not
# listed fall back to the first scalar value in `tool_input`.
_TOOL_PRIMARY_FIELD: dict[str, str] = {
    "Bash": "command",
    "BashOutput": "bash_id",
    "KillShell": "shell_id",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Task": "description",
    "Skill": "skill",
    "Monitor": "description",
    "TaskOutput": "task_id",
    "ToolSearch": "query",
}


def _tool_brief(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos") or []
        return f"{len(todos)} todo(s)"
    field = _TOOL_PRIMARY_FIELD.get(tool_name)
    if field and field in tool_input:
        return str(tool_input[field])[:300]
    for k, v in tool_input.items():
        if k in {"content", "new_string", "old_string"}:
            continue
        if isinstance(v, (str, int, float, bool)):
            return f"{k}={str(v)[:200]}"
    return ""


class ToolStatusMirror:
    def __init__(
        self,
        bot: Bot,
        tr: Translator,
        bot_logs: BotLogs,
        glog: logging.Logger,
        bot_name: str,
    ) -> None:
        self._bot = bot
        self._tr = tr
        self._bot_logs = bot_logs
        self._glog = glog
        self._bot_name = bot_name

    async def handle(
        self,
        chat_id: int,
        phase: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> None:
        cl = self._bot_logs.for_chat(chat_id)
        try:
            if phase == "pre":
                if tool_name in _GATE_HANDLED_TOOLS:
                    return
                desc = _tool_brief(tool_name, payload)
                if desc:
                    body = self._tr.t(
                        "tool_status_pre", tool=tool_name, desc=desc
                    )
                else:
                    body = self._tr.t(
                        "tool_status_pre_no_desc", tool=tool_name
                    )
                cl.info("hook %s: %s", phase, body.replace("\n", " ⏎ "))
                # Plain text — no MarkdownV2 escaping artefacts, lighter
                # visual weight than `send_md_to_chat`.
                try:
                    await self._bot.send_message(
                        chat_id, body[:TG_LIMIT], parse_mode=None,
                        disable_notification=True,
                    )
                except TelegramBadRequest:
                    log.exception("tool status pre send failed")
            elif phase == "post":
                response = payload.get("tool_response")
                # Tool response shapes vary across tools; best-effort extract.
                preview = ""
                if isinstance(response, dict):
                    preview = str(
                        response.get("output")
                        or response.get("stdout")
                        or response.get("text")
                        or response.get("result")
                        or ""
                    )
                elif isinstance(response, str):
                    preview = response
                preview_lines = preview.strip().splitlines()[:6]
                preview = "\n".join(preview_lines)[:600]
                if preview:
                    body = self._tr.t(
                        "tool_status_post_with_preview",
                        tool=tool_name,
                        preview=preview,
                    )
                else:
                    body = self._tr.t("tool_status_post", tool=tool_name)
                cl.info("hook %s: %s", phase, body.replace("\n", " ⏎ "))
                try:
                    await self._bot.send_message(
                        chat_id, body[:TG_LIMIT], parse_mode=None,
                        disable_notification=True,
                    )
                except TelegramBadRequest:
                    log.exception("tool status post send failed")
        except Exception:
            self._glog.exception(
                "[%s] tool-event delivery failed", self._bot_name
            )
