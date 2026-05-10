# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / develop

```bash
# venv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run all bots from src/config/config.json
python -m src.bot

# syntax check after edits
python -m py_compile src/*.py src/config/__init__.py src/i18n/__init__.py
```

There is no test suite, no linter config, no build step. Validate changes by running the bot and watching `logs/<internal_name>/bot.log` (and the per-chat `<chat_id>.log`).

## Big picture

Multi-bot Telegram agent. One process can run several bots in parallel (`asyncio.gather` over `run_bot(cfg)` per entry in `config.json`). Each bot owns:

- a long-polling `aiogram` dispatcher,
- an `AgentSessionManager` that keeps a live `ClaudeSDKClient` per `chat_id` (multi-turn context survives across messages until `/new`, the idle GC closes the session after `session_idle_ttl_sec` of inactivity, or process restart),
- a `DraftStreamer` that animates Claude's reply via the recent `sendMessageDraft` Bot API method while tokens stream in (`include_partial_messages=True` → `text_delta` events),
- a `TelegramPermissionGate` that turns Claude's `can_use_tool` into inline-button prompts (Allow / Deny / Always-allow-this-session) and resolves the `asyncio.Future` from a `callback_query` handler,
- a `BotLogs` that writes general `bot.log` and per-chat `<chat_id>.log` under `<logs_dir>/<internal_name>/`,
- a `Translator` from `src/i18n/<lang>.json`,
- an optional `GroqTranscriber` ([src/transcribe.py](src/transcribe.py)) that handles `voice` / `audio` messages: downloads the file via `bot.download`, posts it to Groq's `audio/transcriptions` endpoint, echoes the transcript as a Markdown blockquote, then feeds it into `agent.ask_stream` like any text message. Disabled when `groq_api_key` is unset.

Entry point [src/bot.py](src/bot.py) wires those together and registers handlers. The final response is converted from Markdown to Telegram MarkdownV2 via `telegramify-markdown`, with a small pre-pass that inserts a blank line after closing fenced code blocks. Long replies are split into ≤4000-char chunks; a chunk that fails MarkdownV2 parsing falls back to plain text.

## Configuration model

`src/config/config.json` is a top-level dict `<internal_name>: BotConfig`. Loader [src/config/__init__.py](src/config/__init__.py) accepts the legacy flat format too — it gets wrapped under name `default`. Token can be overridden via env `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>`.

`allowed_chat_ids` semantics — keep them straight when editing:

- `null` / missing → open to everyone
- `[]` → closed to everyone (every chat gets the refusal containing its own `chat_id`)
- `[id, ...]` → whitelist

`system_prompt: null` falls back to translation key `default_system_prompt` for the configured `lang`.

`logs_dir: null` → console-only logging; otherwise rotating file handlers (10 MB × 5) under `<logs_dir>/<internal_name>/`.

Other tunables on `BotConfig` ([src/config/__init__.py](src/config/__init__.py)): `draft_interval_sec` (0.2), `approval_timeout_sec` (300), `agent_timeout_sec` (180, hard cap per Claude turn), `session_idle_ttl_sec` (3600, idle GC; `0` disables), `chat_logger_capacity` (256, LRU cap on per-chat file loggers).

Voice transcription (Groq): `groq_api_key` (env override `GROQ_API_KEY_<INTERNAL_NAME>`, then `GROQ_API_KEY`; `null` disables the voice handler), `groq_model` (default `whisper-large-v3-turbo`), `groq_timeout_sec` (60), `voice_max_duration_sec` (600, `0` disables the cap).

## Permissions

`AgentSessionManager._make_options` passes `setting_sources=["user", "project", "local"]`, so rules from `.claude/settings.json`, `.claude/settings.local.json` (in `working_dir`) and the user's global settings are honored — those tools never reach the gate.

The "Always allow this session" button issues `PermissionUpdate(type="addRules", behavior="allow", destination="session")` for the specific tool. It dies with the live `ClaudeSDKClient` (i.e. on `/new` or restart). For persistent rules edit `<working_dir>/.claude/settings.local.json`.

## i18n

User-facing strings live in `src/i18n/<lang>.json` (`ru`, `en` shipped). Add a new language by dropping another JSON file with the same keys; `Translator` falls back to the default language if the requested file is missing. The `system_prompt` field stays separate — it controls Claude's reply language directly, while `lang` only affects bot-rendered UI strings.

## Conventions

- All code comments and docstrings are in English.
- User-facing strings (greetings, buttons, errors) must go through `Translator.t(key, **kwargs)` — don't hardcode them in Python.
- The default `python -m src.bot` writes nothing to project root; if you see a stray `bot.log` there it's from a `tee`-based ad-hoc launch, not the app itself.
- `src/config/config.json` and `logs/` are gitignored; `src/config/config.example.json` is the template.
