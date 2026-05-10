import itertools
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_DRAFT_INTERVAL = 0.2  # min seconds between draft updates
DRAFT_TEXT_LIMIT = 4000  # max draft message length

_TELEGRAM_API_BASE = "https://api.telegram.org"
_draft_seq = itertools.count(1)


class DraftStreamer:
    """Streams accumulated text to Telegram via `sendMessageDraft`.

    The final message is sent separately as a regular sendMessage by the caller —
    drafts are ephemeral and not persisted to chat history.

    The streamer accepts an iterator of *delta* chunks (new text only) and
    accumulates them locally; this avoids the O(N²) behavior of re-sending the
    full text on every tick.
    """

    def __init__(
        self,
        token: str,
        http: aiohttp.ClientSession,
        interval_sec: float = DEFAULT_DRAFT_INTERVAL,
    ) -> None:
        self._token = token
        self._http = http
        self._interval = interval_sec

    def __repr__(self) -> str:
        return f"DraftStreamer(interval={self._interval})"

    async def stream(
        self, chat_id: int, chunks: AsyncIterator[str]
    ) -> str:
        draft_id = next(_draft_seq) % 2_147_483_647
        last_sent = 0.0
        last_text = ""
        accumulated = ""

        async for chunk in chunks:
            if not chunk:
                continue
            accumulated += chunk
            now = time.monotonic()
            preview = accumulated[-DRAFT_TEXT_LIMIT:]
            if now - last_sent >= self._interval and preview != last_text:
                await self._call(
                    "sendMessageDraft",
                    {
                        "chat_id": chat_id,
                        "draft_id": draft_id,
                        "text": preview,
                    },
                )
                last_sent = now
                last_text = preview

        return accumulated

    def _redact(self, text: str) -> str:
        # The bot token is embedded in the request URL and may surface in
        # aiohttp error messages / tracebacks. Strip it before logging.
        return text.replace(self._token, "***") if self._token else text

    async def _call(self, method: str, payload: dict[str, Any]) -> None:
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/{method}"
        try:
            async with self._http.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "draft call %s -> %s: %s",
                        method,
                        resp.status,
                        self._redact(body[:200]),
                    )
        except Exception as e:
            log.warning("draft call failed: %s: %s", method, self._redact(repr(e)))
