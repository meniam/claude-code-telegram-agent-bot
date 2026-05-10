import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
)

log = logging.getLogger(__name__)

PermissionCallback = Callable[
    [int, str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


class AgentSessionManager:
    def __init__(
        self,
        on_permission: PermissionCallback | None = None,
        system_prompt: str = "You are a friendly Telegram assistant. Reply concisely.",
        cwd: str | None = None,
        idle_ttl_sec: int = 86400,
        add_dirs: list[str] | None = None,
    ):
        self._on_permission = on_permission
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._idle_ttl = idle_ttl_sec
        self._add_dirs = list(add_dirs) if add_dirs else []
        # chat_id -> (client, last_used_monotonic_ts)
        self._clients: dict[int, tuple[ClaudeSDKClient, float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._gc_task: asyncio.Task[None] | None = None

    def _ensure_gc_running(self) -> None:
        if self._idle_ttl <= 0:
            return
        if self._gc_task is None or self._gc_task.done():
            self._gc_task = asyncio.create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        interval = max(min(self._idle_ttl / 4, 60.0), 5.0)
        try:
            while True:
                await asyncio.sleep(interval)
                await self._gc_idle()
        except asyncio.CancelledError:
            raise

    async def _gc_idle(self) -> None:
        now = time.monotonic()
        stale = [
            chat_id
            for chat_id, (_client, last_used) in self._clients.items()
            if now - last_used > self._idle_ttl
        ]
        for chat_id in stale:
            lock = self._locks.get(chat_id)
            if lock is not None and lock.locked():
                # Session in use right now; skip and try next round.
                continue
            entry = self._clients.pop(chat_id, None)
            self._locks.pop(chat_id, None)
            if entry is None:
                continue
            client, _ = entry
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                log.exception("idle gc: failed to close client for chat_id=%s", chat_id)
            else:
                log.info("idle gc: closed client for chat_id=%s", chat_id)

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    def _make_options(self, chat_id: int) -> ClaudeAgentOptions:
        can_use_tool = None
        if self._on_permission is not None:
            on_perm = self._on_permission

            async def can_use_tool(  # type: ignore[no-redef]
                tool_name: str,
                tool_input: dict[str, Any],
                ctx: ToolPermissionContext,
            ) -> PermissionResultAllow | PermissionResultDeny:
                return await on_perm(chat_id, tool_name, tool_input, ctx)

        return ClaudeAgentOptions(
            system_prompt=self._system_prompt,
            include_partial_messages=True,
            can_use_tool=can_use_tool,
            cwd=self._cwd,
            add_dirs=list(self._add_dirs),
            setting_sources=["user", "project", "local"],
        )

    async def _get_client(self, chat_id: int) -> ClaudeSDKClient:
        self._ensure_gc_running()
        entry = self._clients.get(chat_id)
        if entry is None:
            client = ClaudeSDKClient(options=self._make_options(chat_id))
            await client.__aenter__()
        else:
            client, _ = entry
        self._clients[chat_id] = (client, time.monotonic())
        return client

    async def ask(self, chat_id: int, prompt: str) -> str:
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            await client.query(prompt)
            chunks: list[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
            self._clients[chat_id] = (client, time.monotonic())
            return "".join(chunks).strip() or "(empty response)"

    async def ask_stream(
        self, chat_id: int, prompt: str
    ) -> AsyncIterator[str]:
        """Yield text deltas as tokens arrive from Claude.

        Each yield is a *new chunk* (not the accumulated string) — the consumer
        is responsible for accumulation. This keeps the streaming pipeline O(N)
        in the response length instead of O(N²).

        Uses StreamEvent (text_delta) when include_partial_messages=True.
        The final AssistantMessage acts as a fallback if no delta stream
        came through (e.g. the option is disabled); it carries the full text,
        so it is yielded only when no deltas were observed.
        """
        # The lock is held for the whole generator: it serializes concurrent
        # requests from the same chat_id against a single ClaudeSDKClient.
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            await client.query(prompt)
            saw_delta = False
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    if (
                        event.get("type") == "content_block_delta"
                        and event.get("delta", {}).get("type") == "text_delta"
                    ):
                        saw_delta = True
                        yield event["delta"]["text"]
                elif isinstance(msg, AssistantMessage) and not saw_delta:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield block.text

    async def reset(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            entry = self._clients.pop(chat_id, None)
            if entry is not None:
                client, _ = entry
                await client.__aexit__(None, None, None)

    async def close_all(self) -> None:
        if self._gc_task is not None and not self._gc_task.done():
            self._gc_task.cancel()
            try:
                await self._gc_task
            except (asyncio.CancelledError, Exception):
                pass
            self._gc_task = None
        for chat_id in list(self._clients):
            await self.reset(chat_id)
