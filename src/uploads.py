"""Per-chat upload store + pending-attachments queue.

Telegram photos and documents are saved to disk under
`<uploads_dir>/<chat_id>/<timestamp>_<file_id>_<name>` and queued until the
user sends a text/voice prompt that explains what to do with them. The
queue is drained the next time `reply_with_agent` runs; the prompt sent to
Claude includes the absolute paths so the agent can `Read` them itself.
"""

import re
import time
from dataclasses import dataclass
from pathlib import Path

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name)
    return cleaned or "file"


@dataclass(frozen=True)
class PendingFile:
    path: Path
    kind: str  # "image" | "document"
    name: str  # original filename or photo.jpg


class UploadStore:
    def __init__(self, base_dir: Path):
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        self._pending: dict[int, list[PendingFile]] = {}

    @property
    def base_dir(self) -> Path:
        return self._base

    def chat_dir(self, chat_id: int) -> Path:
        d = self._base / str(chat_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def build_path(self, chat_id: int, file_id: str, name: str) -> Path:
        ts = int(time.time())
        return self.chat_dir(chat_id) / f"{ts}_{file_id[:12]}_{_safe_filename(name)}"

    def add_pending(self, chat_id: int, item: PendingFile) -> None:
        self._pending.setdefault(chat_id, []).append(item)

    def pop_pending(self, chat_id: int) -> list[PendingFile]:
        return self._pending.pop(chat_id, [])

    def has_pending(self, chat_id: int) -> bool:
        return bool(self._pending.get(chat_id))


def format_attachment_prompt(items: list[PendingFile], user_text: str) -> str:
    """Wrap pending attachments into a prompt block Claude can act on."""
    lines = [
        "The user attached the following files (use the Read tool to inspect them):"
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it.path} ({it.kind}, original name: {it.name})")
    body = "\n".join(lines)
    user_text = (user_text or "").strip()
    if user_text:
        return f"{body}\n\nUser message:\n{user_text}"
    return body
