"""Telegram interaction gate package.

`TelegramInteractionGate` (see `gate.py`) is the entry point. Per-flow logic
lives in:

- `permission_prompt.py` — generic Allow / Deny / Always inline buttons.
- `ask_user_question.py` — the `AskUserQuestion` SDK tool flow.
- `plan_mode.py` — the `ExitPlanMode` SDK tool flow.
- `push_notification.py` — the `PushNotification` SDK tool flow.

Each per-flow module exposes free functions that take the gate as their
first argument, so the shared state on the gate is reachable without
inheritance or mixin gymnastics.
"""

from .gate import TelegramInteractionGate

__all__ = ["TelegramInteractionGate"]
