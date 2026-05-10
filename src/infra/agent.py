import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
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

# (chat_id, phase, tool_name, payload) -> awaitable. `phase` is "pre" or
# "post"; payload is the raw `tool_input` for pre and a dict with at least
# a `tool_response` field for post.
ToolEventCallback = Callable[
    [int, str, str, dict[str, Any]],
    Awaitable[None],
]

# Tools that get a `post`-phase preview in the chat (their output is what
# the user is interested in). All other tools only get a one-line `pre`
# announcement so the user sees that the agent is working.
_TOOL_POST_PREVIEW_NAMES = ("Monitor", "TaskOutput")


class AgentSessionManager:
    def __init__(
        self,
        on_permission: PermissionCallback | None = None,
        system_prompt: str = "You are a friendly Telegram assistant. Reply concisely.",
        cwd: str | None = None,
        idle_ttl_sec: int = 86400,
        add_dirs: list[str] | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> None:
        self._on_permission = on_permission
        self._system_prompt = system_prompt
        self._cwd = cwd
        self._idle_ttl = idle_ttl_sec
        self._add_dirs = list(add_dirs) if add_dirs else []
        self._on_tool_event = on_tool_event
        # chat_id -> (client, last_used_monotonic_ts)
        self._clients: dict[int, tuple[ClaudeSDKClient, float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._gc_task: asyncio.Task[None] | None = None
        # Last permission-mode value set via `set_permission_mode`. The SDK
        # has no public getter, so we mirror it here for `/whoami`.
        self._modes: dict[int, str] = {}
        # Last model id set via `set_model` (None == SDK default).
        self._models: dict[int, str | None] = {}

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
            self._modes.pop(chat_id, None)
            self._models.pop(chat_id, None)
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
        can_use_tool: (
            Callable[
                [str, dict[str, Any], ToolPermissionContext],
                Awaitable[PermissionResultAllow | PermissionResultDeny],
            ]
            | None
        ) = None
        if self._on_permission is not None:
            on_perm = self._on_permission

            async def _can_use_tool(
                tool_name: str,
                tool_input: dict[str, Any],
                ctx: ToolPermissionContext,
            ) -> PermissionResultAllow | PermissionResultDeny:
                return await on_perm(chat_id, tool_name, tool_input, ctx)

            can_use_tool = _can_use_tool

        hooks: dict[str, list[HookMatcher]] | None = None
        if self._on_tool_event is not None:
            on_evt = self._on_tool_event
            post_matcher = "|".join(_TOOL_POST_PREVIEW_NAMES)

            def _hook_field(input: Any, name: str, default: Any) -> Any:
                # SDK hook inputs are TypedDicts (plain dicts at runtime),
                # so attribute access via getattr always returns the
                # default. Fall back to dict lookup.
                if isinstance(input, dict):
                    return input.get(name, default)
                return getattr(input, name, default)

            async def pre_hook(
                input: Any, _tool_use_id: Any, _context: Any
            ) -> dict[str, Any]:
                try:
                    await on_evt(
                        chat_id,
                        "pre",
                        _hook_field(input, "tool_name", ""),
                        dict(_hook_field(input, "tool_input", {}) or {}),
                    )
                except Exception:
                    log.exception("pre-tool hook failed")
                return {}

            async def post_hook(
                input: Any, _tool_use_id: Any, _context: Any
            ) -> dict[str, Any]:
                try:
                    payload = {
                        "tool_input": dict(
                            _hook_field(input, "tool_input", {}) or {}
                        ),
                        "tool_response": _hook_field(
                            input, "tool_response", None
                        ),
                    }
                    await on_evt(
                        chat_id,
                        "post",
                        _hook_field(input, "tool_name", ""),
                        payload,
                    )
                except Exception:
                    log.exception("post-tool hook failed")
                return {}

            hooks = {
                # Pre-fire announcement for every tool — the bot.py callback
                # filters out tools we already render via the interaction
                # gate (AskUserQuestion / ExitPlanMode / PushNotification).
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[cast(Any, pre_hook)])
                ],
                # Post-fire previews only for tools whose output the user
                # actually wants to see.
                "PostToolUse": [
                    HookMatcher(matcher=post_matcher, hooks=[cast(Any, post_hook)])
                ],
            }

        return ClaudeAgentOptions(
            system_prompt=self._system_prompt,
            include_partial_messages=True,
            can_use_tool=can_use_tool,
            cwd=self._cwd,
            add_dirs=list(self._add_dirs),
            setting_sources=["user", "project", "local"],
            # SDK declares `hooks` as a dict keyed by hook-name Literals; we
            # build it dynamically with plain str keys, so cast to Any here.
            hooks=cast(Any, hooks),
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

    async def get_context_usage(self, chat_id: int) -> dict[str, Any]:
        """Wraps ClaudeSDKClient.get_context_usage(); spins up a client if needed."""
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            result = await client.get_context_usage()
            self._clients[chat_id] = (client, time.monotonic())
            return dict(result)

    async def set_permission_mode(self, chat_id: int, mode: str) -> None:
        """Switch the per-chat client's permission mode at runtime.

        Preserves session context (no `__aexit__` / re-init). The SDK
        forwards the request to the running CLI, which applies the new
        mode for subsequent tool calls in the same conversation.
        """
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            # SDK accepts a Literal here; the caller validates `mode` against
            # the same allowlist (see handlers/selectors.py:_MODE_VALUES).
            await client.set_permission_mode(cast(Any, mode))
            self._clients[chat_id] = (client, time.monotonic())
            self._modes[chat_id] = mode

    async def set_model(self, chat_id: int, model: str | None) -> None:
        """Switch the per-chat client's model at runtime. Pass None for default."""
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            await client.set_model(model)
            self._clients[chat_id] = (client, time.monotonic())
            self._models[chat_id] = model

    async def interrupt(self, chat_id: int) -> bool:
        """Send an interrupt to the live client. Returns False if no client.

        Does NOT acquire the per-chat lock — the lock is held by the running
        `query` / `receive_response`, which is what we want to interrupt.
        """
        entry = self._clients.get(chat_id)
        if entry is None:
            return False
        client, _ = entry
        await client.interrupt()
        return True

    async def get_mcp_status(self, chat_id: int) -> dict[str, Any]:
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            result = await client.get_mcp_status()
            self._clients[chat_id] = (client, time.monotonic())
            return dict(result)

    async def get_server_info(self, chat_id: int) -> dict[str, Any] | None:
        async with self._lock(chat_id):
            client = await self._get_client(chat_id)
            result = await client.get_server_info()
            self._clients[chat_id] = (client, time.monotonic())
            return dict(result) if result else None

    def current_mode(self, chat_id: int) -> str:
        return self._modes.get(chat_id, "default")

    def current_model(self, chat_id: int) -> str | None:
        return self._models.get(chat_id)

    def has_session(self, chat_id: int) -> bool:
        return chat_id in self._clients

    async def reset(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            self._modes.pop(chat_id, None)
            self._models.pop(chat_id, None)
            entry = self._clients.pop(chat_id, None)
            if entry is not None:
                client, _ = entry
                await client.__aexit__(None, None, None)
        # Drop the per-chat lock outside its own critical section so we don't
        # leak one Lock per unique chat_id across the process lifetime.
        self._locks.pop(chat_id, None)

    async def close_all(self) -> None:
        if self._gc_task is not None and not self._gc_task.done():
            self._gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gc_task
            self._gc_task = None
        for chat_id in list(self._clients):
            await self.reset(chat_id)
