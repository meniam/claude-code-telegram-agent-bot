# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / develop

```bash
# venv + deps (pyproject.toml is the source of truth; requirements.txt mirrors deps)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# run all bots from src/config/config.json
python -m src.bot
# or use the console script declared in pyproject.toml:
agent-bot

# tests (76 pure unit tests; no live aiogram or SDK calls)
pytest -q

# lint + type + security (all expected green)
ruff check src/ tests/
mypy src/ tests/ --strict
pyright src/ tests/
bandit -r src/ -q
pip-audit --strict
```

Validate behavioral changes by running the bot and watching `logs/<internal_name>/bot.log` (and the per-chat `<chat_id>.log`). Tests cover pure modules (config, commands, i18n, uploads, markdown, reactions, sdk_views, plan_router, streaming redact, logs LRU, ACL factory) — handler wiring, live SDK calls, and Telegram Bot API integration are exercised manually.

## Big picture

Multi-bot Telegram agent. One process can run several bots in parallel (`asyncio.gather` over `run_bot(cfg)` per entry in `config.json`). Each bot owns:

- a long-polling `aiogram` dispatcher,
- an `AgentSessionManager` ([src/infra/agent.py](src/infra/agent.py)) that keeps a live `ClaudeSDKClient` per `chat_id` (multi-turn context survives across messages until `/new`, the idle GC closes the session after `session_idle_ttl_sec` of inactivity, or process restart),
- a `DraftStreamer` ([src/infra/streaming.py](src/infra/streaming.py)) that animates Claude's reply via the recent `sendMessageDraft` Bot API method while tokens stream in (`include_partial_messages=True` → `text_delta` events),
- a `TelegramInteractionGate` ([src/infra/interactions/](src/infra/interactions/) — package; `gate.py` is the entry, per-flow logic lives in `permission_prompt.py`, `ask_user_question.py`, `plan_mode.py`, `push_notification.py`) that turns Claude's `can_use_tool` into inline-button prompts (Allow / Deny / Always-allow-this-session) and resolves the `asyncio.Future` from a `callback_query` handler. The gate also intercepts the built-in `AskUserQuestion` tool: each question is rendered as its own inline keyboard (single- or multi-select + a "Skip" button), answers are collected sequentially, then returned to Claude as a plain-text summary via `PermissionResultDeny.message` (the SDK feeds the message to the model as the tool's response). Any incoming user message — text, voice, photo, document, sticker — calls `gate.cancel_active_aq(chat_id)` to auto-skip a hanging prompt so the new message is not deadlocked behind an unanswered quiz.
- a `BotLogs` ([src/infra/logs.py](src/infra/logs.py)) that writes general `bot.log` and per-chat `<chat_id>.log` under `<logs_dir>/<internal_name>/`,
- a `Translator` from `src/i18n/<lang>.json`,
- an optional command loader ([src/infra/commands.py](src/infra/commands.py)) that scans `commands_dir` for `*.md` files. Each file is one Telegram slash command with `--- name / description ---` frontmatter and a body. Body is sent to Claude as the user prompt when the chat types `/<name>`. The literal token `$ARGUMENTS` in the body is replaced with whatever the user typed after the command (empty string when no arguments). Disabled when `commands_dir` is unset. Full reference: [COMMANDS.md](COMMANDS.md).
- an optional `GroqTranscriber` ([src/services/transcribe.py](src/services/transcribe.py)) that handles `voice` / `audio` messages: downloads the file via `bot.download`, posts it to Groq's `audio/transcriptions` endpoint, echoes the transcript as a Markdown blockquote, then feeds it into `agent.ask_stream` like any text message. Disabled when `groq_api_key` is unset.
- an optional `UploadStore` ([src/services/upload_store.py](src/services/upload_store.py)) that handles `photo` / `document` / `sticker` messages: saves them under `<uploads_dir>/<chat_id>/<ts>_<file_id>_<name>` and fires the agent immediately. A subsequent text or voice message also drains anything still in the queue. Albums (`media_group_id`) are debounced ~1.5 s so a single agent turn handles all items at once; the caption from any album item is preserved. Prompt to Claude is built by `format_attachment_prompt(...)` — Claude gets the absolute paths and is told to use the `Read` tool. The configured `uploads_dir` is forwarded to `ClaudeAgentOptions(add_dirs=[...])`, so Claude's `Read` works without a permission prompt on paths outside `working_dir`. Stickers branch by type — static WebP → `kind="image"` (Claude reads it as a regular image), animated `.tgs` (Lottie) and video `.webm` are saved with `kind="binary (...)"` so Claude knows it cannot decode them visually. Disabled when `uploads_dir` is unset.

Entry point [src/bot.py](src/bot.py) wires those together and delegates handler registration to `register_all(dp, commands)` in [src/handlers/__init__.py](src/handlers/__init__.py). The final response is converted from Markdown to Telegram MarkdownV2 via `telegramify-markdown` (see [src/ui/markdown.py](src/ui/markdown.py)), with a small pre-pass that inserts a blank line after closing fenced code blocks. Long replies are split into ≤4000-char chunks; a chunk that fails MarkdownV2 parsing falls back to plain text. SDK-response renderers (`format_context_usage`, `format_mcp_status`, `format_server_info`) live in [src/ui/sdk_views.py](src/ui/sdk_views.py).

## Source layout

```
src/
  bot.py                  entry: run_bot + per-bot _supervise (exp-backoff) + main; small _make_* factories
  config/                 BotConfig (pydantic) + config.json loader
  i18n/                   Translator + <lang>.json files
  infra/
    agent.py              AgentSessionManager — per-chat ClaudeSDKClient cache + idle GC + hooks setup
    commands.py           load_commands(commands_dir) — frontmatter parser for `*.md` slash-command files
    logs.py               BotLogs — general + per-chat rotating loggers with LRU eviction
    streaming.py          DraftStreamer — sendMessageDraft animation while tokens stream
    interactions/         TelegramInteractionGate (package, see below)
      __init__.py         re-exports TelegramInteractionGate
      gate.py             class + shared helpers + dispatcher + thin callback wrappers
      permission_prompt.py generic Allow / Deny / Always inline-button flow
      ask_user_question.py AskUserQuestion tool flow (multi-question, multi-select)
      plan_mode.py        ExitPlanMode tool flow (approve / reject + text feedback)
      push_notification.py PushNotification tool flow (one-shot 🔔)
  services/               external API clients
    transcribe.py         GroqTranscriber — POST audio to Groq's /audio/transcriptions
    upload_store.py       UploadStore — saves photo/doc/sticker to disk; pending-attachments queue
  ui/                     telegram-facing helpers + middleware
    agent_reply.py        react_to + reply_with_agent pipeline (draining pending uploads)
    album.py              AlbumDebouncer — coalesce media_group_id arrivals
    markdown.py           md → MarkdownV2, chunked send, audio_filename, format_quote, TG_LIMIT
    middleware.py         AclMiddleware (fail-closed)
    plan_router.py        PlanRouter — per-chat /plan arming + agent fire
    reactions.py          ReactionPicker — first-matching regex → emoji (rules from i18n)
    sdk_views.py          format_context_usage / format_mcp_status / format_server_info
    tool_status.py        ToolStatusMirror — SDK pre/post hooks → chat preview
  handlers/               aiogram handlers (each module has top-level fns + register(dp))
    __init__.py           register_all(dp, custom_commands) — orders the registrations
    context.py            BotContext dataclass — wiring aggregate consumed by every handler
    basic.py              /start, /new, /cancel, /context, /stop, /mcp, /info, /whoami, /help
    custom.py             user-defined slash commands loaded from `commands_dir`
    plan.py               /plan + perm:/aq:/plan: callback handlers
    selectors.py          /mode + /model (inline keyboards)
    text.py               F.text catch-all (plan-feedback consume + agent turn)
    uploads.py            F.photo / F.document / F.sticker
    voice.py              F.voice / F.audio → Groq transcript → agent
tests/                    pytest unit tests (76 — pure modules only)
pyproject.toml            build, deps, ruff, mypy, pytest config; [project.scripts] agent-bot
```

`ui/` holds Telegram-side UX building blocks (no aiogram handlers). `handlers/` registers aiogram message and callback handlers, one module per feature. `register_all` wires them in the order required by aiogram (exact `Command(...)` filters → user-defined commands → `F.text` catch-all → `F.voice|F.audio` → `F.photo`/`F.document`/`F.sticker`).

Access control is enforced by `AclMiddleware` ([src/ui/middleware.py](src/ui/middleware.py)) registered as `outer_middleware` on both `dp.message` and `dp.callback_query`. It injects `chat_id`, `cl` (per-chat logger), and `ctx` (`BotContext` in [src/handlers/context.py](src/handlers/context.py)) into the handler kwargs. Gate-managed callbacks (`perm:`, `aq:`, `plan:`) bypass the ACL — `TelegramInteractionGate` validates ownership itself.

## Configuration model

`src/config/config.json` is a top-level dict `<internal_name>: BotConfig`. Loader [src/config/__init__.py](src/config/__init__.py) accepts the legacy flat format too — it gets wrapped under name `default`. Token can be overridden via env `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>`.

**Per-field reference: [CONFIG.md](CONFIG.md)** — types, defaults, semantics, validation rules, env overrides, examples for every `BotConfig` field. The summary below covers only the load-bearing invariants.

Access control is **fail-closed**. Three fields drive it; `is_allowed` evaluates them in this order:

1. `blacklist_chat_ids: tuple[int, ...]` (default `()`). If the sender is here, deny outright. Beats `allowed_for_all` and the whitelist.
2. `allowed_for_all: bool` (default `false`). When `true` every non-blacklisted chat passes. Logged as a warning on startup. Only enable for genuinely public bots.
3. `allowed_chat_ids: tuple[int, ...]` (default `()`). When `allowed_for_all` is `false`, only chats in this list are accepted. `null`, missing field and `[]` all mean **nobody is allowed** — every message gets the refusal containing the sender's `chat_id`.

`system_prompt: null` falls back to translation key `default_system_prompt` for the configured `lang`.

`logs_dir: null` → console-only logging; otherwise rotating file handlers (10 MB × 5) under `<logs_dir>/<internal_name>/`.

Other tunables on `BotConfig` ([src/config/__init__.py](src/config/__init__.py)): `draft_interval_sec` (0.2), `approval_timeout_sec` (300), `agent_timeout_sec` (600, hard cap per Claude turn — bumped from 180 because plan-mode generation + user click time can easily exceed the older default), `session_idle_ttl_sec` (86400 = 24h, idle GC; `0` disables), `chat_logger_capacity` (256, LRU cap on per-chat file loggers).

Voice transcription (Groq): `groq_api_key` (env override `GROQ_API_KEY_<INTERNAL_NAME>`, then `GROQ_API_KEY`; `null` disables the voice handler), `groq_model` (default `whisper-large-v3-turbo`), `groq_timeout_sec` (60), `voice_max_duration_sec` (600, `0` disables the cap).

File uploads: `uploads_dir` (`null` disables `photo` / `document` / `sticker` handlers — the bot replies with `upload_disabled`), `upload_max_bytes` (20 MB default; `0` disables the cap; the upstream Bot API caps downloads at 20 MB without a self-hosted Bot API server anyway). When set, the directory is also passed to the SDK as `add_dirs`, so files saved there are reachable by `Read` without permission prompts.

User-defined slash commands: `commands_dir` (absolute path; missing directory raises a startup error; `null` / missing → no extra commands). Each `*.md` file inside it is a single command. Frontmatter is a tiny YAML subset (`key: value` lines, no nesting) — only `name` and `description` are read; `name` defaults to the file stem. The literal token `$ARGUMENTS` in the body is replaced with the text the user typed after the command (`/foo bar baz` → `$ARGUMENTS = "bar baz"`; empty when no args). Files with invalid names, names colliding with built-ins (`start`, `new`, `context`, `plan`, `cancel`, `stop`, `mode`, `model`, `mcp`, `info`, `whoami`, `help`), duplicate names, or empty bodies are skipped with a warning. Commands are loaded once at startup; restart to pick up changes. Handlers are registered before the generic `F.text` handler so `/<name>` is routed correctly. Full reference: [COMMANDS.md](COMMANDS.md).

## Permissions

`AgentSessionManager._make_options` passes `setting_sources=["user", "project", "local"]`, so rules from `.claude/settings.json`, `.claude/settings.local.json` (in `working_dir`) and the user's global settings are honored — those tools never reach the gate.

The "Always allow this session" button issues `PermissionUpdate(type="addRules", behavior="allow", destination="session")` for the specific tool. It dies with the live `ClaudeSDKClient` (i.e. on `/new` or restart). For persistent rules edit `<working_dir>/.claude/settings.local.json`.

`AskUserQuestion` is special-cased in [src/infra/interactions/ask_user_question.py](src/infra/interactions/ask_user_question.py): the standard Allow/Deny/Always prompt is **not** shown for it. Instead the gate iterates over the `questions` array, posting each as an inline keyboard. Single-select question fires on the first tap; multi-select toggles between `▫️` and `☑️` until the user taps `✅ Done`. Every question also has a `⏭ Skip` button. Per-question timeout uses `approval_timeout_sec`. Result format is a plain-text block (`User responded to AskUserQuestion via Telegram inline buttons: …`) returned through `PermissionResultDeny.message` — Claude reads it as the tool result. `gate.cancel_active_aq(chat_id)` is invoked from every input handler so a fresh user message auto-skips the hanging quiz; remaining questions are tagged `(skipped)` in the summary.

Three more built-in tools are special-cased the same way:

- `ExitPlanMode` ([src/infra/interactions/plan_mode.py](src/infra/interactions/plan_mode.py)) — the `plan` markdown is sent to the chat (rendered via the shared `send_md_to_chat` helper) followed by a separate two-button message (Approve / Reject). Approve → `PermissionResultAllow()`. Reject → `PermissionResultDeny(message=…)` with either an empty default or whatever feedback the user typed: `gate.consume_plan_text(chat_id, text)` is called from the `F.text` handler before the regular agent flow and resolves the pending plan future as `("reject", feedback)`. Timeout reuses `approval_timeout_sec`. State lives in `_plan_pending: dict[chat_id, (Future, request_id, prompt_msg_id)]`.
- `PushNotification` ([src/infra/interactions/push_notification.py](src/infra/interactions/push_notification.py)) — the `message` field is forwarded to Telegram as `🔔 …` (plain text). The gate then returns `PermissionResultDeny(message="Notification delivered to user via Telegram.")` so Claude sees the call as successful and does not retry.
- `Monitor` and `TaskOutput` — kept on the standard tool path (they must actually run); status is mirrored via SDK `PreToolUse` / `PostToolUse` hooks. Hooks are wired in [src/infra/agent.py](src/infra/agent.py) (`AgentSessionManager(on_tool_event=…)` builds them per chat) and the callback is the `ToolStatusMirror.handle` method from [src/ui/tool_status.py](src/ui/tool_status.py) — pre sends `🔧 {tool}: {description}`, post sends `✅ {tool} done` plus a 6-line / 600-char preview of the response. i18n keys: `tool_status_pre`, `tool_status_pre_no_desc`, `tool_status_post`, `tool_status_post_with_preview`.

## i18n

User-facing strings live in `src/i18n/<lang>.json`. Bundled top-20 languages: `ar bn de en es fr hi id ja ko mr pt ru sw ta te tr ur vi zh`. Add a new language by dropping another JSON file with the same keys; `Translator` falls back to the default language file if the requested key is missing. The `system_prompt` field stays separate — it controls Claude's reply language directly, while `lang` only affects bot-rendered UI strings.

## Testing

`tests/` has 76 unit tests covering pure modules (no live aiogram or SDK). Run with `pytest -q`.

| File | What |
|---|---|
| `test_config.py` | `BotConfig` validation, flat/multi format, env override, ACL defaults |
| `test_commands.py` | Frontmatter parser, name validation, dedup, size cap |
| `test_uploads.py` | `_safe_filename` (path-traversal), pending queue, `format_attachment_prompt` |
| `test_i18n.py` | Translator lookup, default-lang fallback, format args |
| `test_markdown.py` | `_pad_after_code_blocks`, `to_mdv2`, `format_quote`, `audio_filename` |
| `test_reactions.py` | `ReactionPicker.pick` + Translator-driven factory |
| `test_sdk_views.py` | `format_context_usage` / `format_mcp_status` / `format_server_info` |
| `test_plan_router.py` | `arm` / `is_armed` / `disarm` per-chat state |
| `test_streaming.py` | `DraftStreamer._redact` masks the bot token |
| `test_logs.py` | `BotLogs` LRU eviction closes file handlers |
| `test_bot_factories.py` | `_make_acl` (fail-closed semantics), `_build_bot_command_list` |

Not covered (integration-heavy, would need aiogram/SDK mocks): handlers, agent session, gate flows, transcriber, album debouncer, ACL middleware, tool-status mirror.

## Tooling

`pyproject.toml` declares everything. Console script: `agent-bot = "src.bot:_cli"`.

| Tool | Config | Expected |
|---|---|---|
| `ruff check` | `[tool.ruff]` — E, F, W, I, B, UP, ASYNC, S, RUF, SIM, PTH, ANN, RET, ARG, TC | clean |
| `ruff format` | matches `ruff check` | use to format |
| `mypy --strict` | `[tool.mypy]` python_version=3.11, ignore_missing_imports | 0 errors |
| `pyright` | default | 0 errors |
| `bandit -r src/` | — | 0 issues (one `# nosec B101` on internal assert) |
| `pip-audit --strict` | — | no known vulns |
| `pytest -q` | `[tool.pytest.ini_options]` asyncio_mode=auto | 76 passed |

Ignored ruff codes (`pyproject.toml`): `S101`/`S110` (assert/try-pass intentional), `ANN401` (Any at SDK / JSON boundaries), `TC006` (cast() quoting style), `RET504` (assign-then-return clarity), `E501` (length handled by formatter).

## Conventions

- All code comments and docstrings are in English.
- User-facing strings (greetings, buttons, errors) must go through `Translator.t(key, **kwargs)` — don't hardcode them in Python.
- The default `python -m src.bot` writes nothing to project root; if you see a stray `bot.log` there it's from a `tee`-based ad-hoc launch, not the app itself.
- `src/config/config.json` and `logs/` are gitignored; `src/config/config.example.json` is the template.
- Per-field config reference: [CONFIG.md](CONFIG.md). Full project overview for LLMs: [AGENTS.md](AGENTS.md).
