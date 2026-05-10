# Claude Code Telegram Agent Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Python Telegram bot that receives messages and replies through the **Claude Agent SDK**. Built on [aiogram v3](https://docs.aiogram.dev/) + [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python).

Each chat gets its own live `ClaudeSDKClient` session, so Claude remembers context across messages. The `/new` command starts a fresh session.

## Features

- **Multi-turn dialog per chat_id** — every chat owns a live Claude session; context persists between messages.
- **Streaming response** via the recent `sendMessageDraft` Bot API method — while Claude is generating, the user sees an animated draft that grows token by token (`include_partial_messages=True`, parsed `text_delta` events).
- **Final reply in MarkdownV2** — converted via `telegramify-markdown`, split into chunks ≤ 4000 chars, falls back to plain text when the parser chokes. A blank line is automatically inserted after fenced code blocks.
- **Link previews disabled** globally (`link_preview_is_disabled=True`).
- **Permission gate** — when Claude tries to use a tool that requires asking, the bot sends a message with inline buttons: _Allow / Deny / Always allow this session_. "Always" injects an `addRules` rule with `destination="session"` via `PermissionUpdate`.
- **Emoji reactions on incoming messages** — picked from a keyword map (thanks → ❤, error → 🤯, joke → 😁, code → 👨‍💻, question → 🤔, etc.), default — 👀.
- **Working directory from config** — `working_dir` is forwarded to `ClaudeAgentOptions(cwd=...)`, so Claude operates in the desired folder.
- **Settings sources** — the SDK loads permissions from `user`/`project`/`local` Claude Code settings (e.g. `.claude/settings.local.json` inside `cwd`), so tools whitelisted there never reach the gate.
- **Bot commands** registered in the Telegram menu (`/start`, `/new`).
- **i18n** — UI strings (buttons, greetings, permission prompts, default `system_prompt`) live in `src/i18n/<lang>.json`. Per-bot `lang` field in config picks a translation. Bundled (top-20 world languages): `en`, `zh`, `hi`, `es`, `ar`, `fr`, `bn`, `pt`, `ru`, `ur`, `id`, `de`, `ja`, `sw`, `mr`, `te`, `tr`, `ta`, `vi`, `ko`. Custom `system_prompt` overrides the default and controls Claude's reply language directly.
- **Access control** — `allowed_chat_ids` per bot. `null`/missing = open to everyone, `[]` = closed to everyone, list of IDs = whitelist. Outsiders get their own `chat_id` plus instructions to forward it to the admin.
- **Multiple bots in one process** — `config.json` accepts a `<internal_name>: <bot_config>` map; every entry runs concurrently via `asyncio.gather` with its own token / `working_dir` / `system_prompt` / `logs_dir`.
- **Logs** — per-bot `bot.log` and per-chat `<chat_id>.log` under `<logs_dir>/<internal_name>/`, with rotation.

## Prerequisites

- **Python 3.10+**
- **Claude Code CLI** on the system — required once for `claude login` (or export `ANTHROPIC_API_KEY`). The SDK ships a bundled copy of `claude` for runtime use, but logging in via subscription requires the system CLI: `npm install -g @anthropic-ai/claude-code` (needs Node.js 18+).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Claude Code authentication — pick one:
    - `claude login` (Claude / Max / Team subscription) — **recommended**, no API key needed.
    - or set `ANTHROPIC_API_KEY` env var — billed via [console.anthropic.com](https://console.anthropic.com/).

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Authenticate Claude Code once:

```bash
claude login
```

## Configuration

```bash
cp src/config/config.example.json src/config/config.json
```

Format: top-level is a `<internal_name>: <bot_config>` map. Every key is launched as an independent bot in the same process.

```json
{
  "brain": {
    "telegram_bot_token": "123456:ABC...",
    "system_prompt": "You are a friendly assistant. Be concise.",
    "lang": "en",
    "allowed_chat_ids": [123456789, 987654321],
    "working_dir": "/Users/me/Projects/brain",
    "logs_dir": "/Users/me/agent-bot-logs",
    "draft_interval_sec": 0.2,
    "approval_timeout_sec": 300
  },
  "research": {
    "telegram_bot_token": "789012:DEF...",
    "system_prompt": "Ты ресёрчер. Отвечай развёрнуто, со ссылками.",
    "lang": "ru",
    "allowed_chat_ids": [],
    "working_dir": "/Users/me/Projects/research",
    "logs_dir": "/Users/me/agent-bot-logs"
  }
}
```

`internal_name` is a technical identifier used for log directories and console messages — not the same as the bot's `@username` from BotFather.

| Field                  | Purpose                                                                                                                                                                                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `telegram_bot_token`   | Token from @BotFather. **Required.** Can be overridden by env var `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>` (uppercase).                                                                                                                                                       |
| `system_prompt`        | System prompt for Claude. `null` → fallback to translation key `default_system_prompt` for the chosen `lang`.                                                                                                                                                              |
| `lang`                 | UI language for bot-facing strings (greetings, buttons, permission prompts). Pick a code matching a JSON file in `src/i18n/`. Bundled: `en`, `zh`, `hi`, `es`, `ar`, `fr`, `bn`, `pt`, `ru`, `ur`, `id`, `de`, `ja`, `sw`, `mr`, `te`, `tr`, `ta`, `vi`, `ko`. Default: `ru`. Doesn't affect Claude's replies — those follow `system_prompt`. |
| `allowed_chat_ids`     | Whitelist of Telegram chat IDs that may talk to this bot. `null` or missing → open to everyone. `[]` → closed to everyone (use this to lock the bot down before adding IDs). `[id, id, ...]` → only listed chats may use the bot; anyone else gets a polite refusal that includes their chat_id and instructions for the admin. |
| `working_dir`          | Working directory for Claude Code. `null` / missing → process cwd. Absolute path or `~/...` accepted. A non-existent directory triggers a startup error.                                                                                                                  |
| `logs_dir`             | Root log directory. `null` → console only. Otherwise creates `<logs_dir>/<internal_name>/bot.log` (general per-bot log) + `<logs_dir>/<internal_name>/<chat_id>.log` (per-chat). Rotated at 10 MB × 5 files. Directory is created automatically. |
| `draft_interval_sec`   | Minimum seconds between draft-message updates while streaming. Lower = smoother animation, more API calls. Default `0.2`.                                                                                                                                                 |
| `approval_timeout_sec` | How long to wait for the user's reply to a permission prompt before auto-deny. Default `300`.                                                                                                                                                                             |
| `agent_timeout_sec`    | Hard timeout per Claude turn (seconds). Default `180`.                                                                                                                                                                                                                    |
| `session_idle_ttl_sec` | Idle TTL for a per-chat `ClaudeSDKClient`. A background GC closes clients with no traffic for that long; the next message opens a fresh session (context lost). Default `3600`. Set `0` to disable.                                                                       |
| `chat_logger_capacity` | Max number of per-chat file loggers cached in memory (LRU). Default `256`.                                                                                                                                                                                                |

### `allowed_chat_ids` semantics

| Value | Meaning |
|---|---|
| `null` / field missing | Open to everyone — no access check. |
| `[]` | Closed to everyone — every message returns the refusal with the sender's `chat_id`. |
| `[id, id, ...]` | Whitelist — only listed chats may use the bot. Outsiders get the refusal. |

Bootstrap flow when you don't yet know your `chat_id`:

1. Set `"allowed_chat_ids": []` in `config.json`.
2. Start the bot, message it from Telegram — the refusal contains your `chat_id`.
3. Replace `[]` with `[<your_chat_id>]` and restart.

The legacy flat format (with `telegram_bot_token` at the top level) still works — it gets wrapped under the name `default`.

`src/config/config.json` and `.env` are in `.gitignore`.

## Run

```bash
source .venv/bin/activate
python -m src.bot
```

You should see `Run polling for bot @your_bot_name` in the logs. Open the bot in Telegram → `/start`.

## Bot commands

- `/start` — greeting.
- `/new` — start a fresh Claude Code session for the current chat (the live `ClaudeSDKClient` is closed, context and session-level allow rules are dropped).
- any text — a question for Claude. The bot reacts to your message, streams the response via draft, then sends the final MarkdownV2 message.

## Layout

- [src/bot.py](src/bot.py) — entry point: aiogram `Dispatcher`, long polling, command/text/callback handlers, Markdown → MarkdownV2 conversion, `asyncio.gather` for running multiple bots.
- [src/agent.py](src/agent.py) — `AgentSessionManager`: `chat_id → ClaudeSDKClient` map + per-chat `asyncio.Lock`, SDK options (including `setting_sources`, `cwd`, `can_use_tool`).
- [src/streaming.py](src/streaming.py) — `DraftStreamer`: throttled `sendMessageDraft` via direct HTTP POST to the Bot API.
- [src/permissions.py](src/permissions.py) — `TelegramPermissionGate`: inline Allow/Deny/Always buttons, `asyncio.Future` per request_id, timeout.
- [src/reactions.py](src/reactions.py) — keyword → emoji rules for reactions on incoming messages.
- [src/config/__init__.py](src/config/__init__.py) — `BotConfig` dataclass, `src/config/config.json` loader (multi-bot format with backward compatibility).
- [src/config/config.example.json](src/config/config.example.json) — config template.
- [src/logs.py](src/logs.py) — `BotLogs`: general `bot.log` + per-chat files.
- [src/i18n/](src/i18n/) — `Translator` + `<lang>.json` files. Add a new translation by dropping another JSON file with the same keys.
- [requirements.txt](requirements.txt) — dependencies (`claude-agent-sdk`, `aiogram>=3.13`, `aiohttp`, `telegramify-markdown`, `pydantic>=2`).

## Notes

- **State is not persisted** — it is lost on process restart. For durability, add storage of `session_id` from `ResultMessage` and `resume` via `ClaudeAgentOptions(resume=...)`.
- **Idle session GC** — a per-chat `ClaudeSDKClient` is closed automatically after `session_idle_ttl_sec` of inactivity (default 1 hour). The next message opens a fresh session, so context from before the timeout is gone. Set `session_idle_ttl_sec: 0` in config to disable.
- **Long replies** are automatically split into chunks of 4000 chars (Telegram limit is 4096).
- **Permission mode** is not set — defaults apply. Tools not covered by `setting_sources` go through the gate. For fully autonomous mode you can set `permission_mode="bypassPermissions"` in `_make_options` (but it disables every check).
- **"Always allow"** is session-scoped (until `/new`, idle GC, or restart). Persistent rules belong in `.claude/settings.local.json` inside `working_dir`.

## License

Released under the [MIT License](LICENSE) — © 2026 Eugene Myazin.
