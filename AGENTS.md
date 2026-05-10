# AGENTS.md

Guide for LLM agents working with this codebase. Architecture, slash-command
system, plan-mode lifecycle, special-cased SDK tools, logs, conventions.

Companion docs:
- [README.md](README.md) — user-facing feature list and install.
- [INSTALLATION.md](INSTALLATION.md) — production install.
- [CONFIG.md](CONFIG.md) — per-field config reference.
- [COMMANDS.md](COMMANDS.md) — custom slash-command reference.
- [CLAUDE.md](CLAUDE.md) — short orientation pinned to the repo.

If anything here contradicts the code, **the code wins**. Re-read the
relevant module before relying on a claim in this file.

---

## 1. What this project is

Multi-bot Telegram → Claude Agent SDK bridge. One Python process runs N
Telegram bots concurrently (one per entry in `src/config/config.json`).
Each bot owns:

- a long-polling `aiogram` `Dispatcher`,
- a per-chat `AgentSessionManager` holding live `ClaudeSDKClient` sessions
  (multi-turn context preserved across messages until `/new`, idle GC, or
  process restart),
- a `DraftStreamer` animating Claude's reply via Bot API `sendMessageDraft`
  while tokens stream in,
- a `TelegramInteractionGate` turning `can_use_tool` into inline-button
  prompts; intercepts `AskUserQuestion`, `ExitPlanMode`, `PushNotification`,
- a `BotLogs` writing general `bot.log` and per-chat `<chat_id>.log`,
- a `Translator` from `src/i18n/<lang>.json`,
- optional `GroqTranscriber` (voice → text), `UploadStore` (files), and a
  custom slash-command loader.

The bot accepts text, voice, audio, photo, document, sticker, albums.
Replies in MarkdownV2 (chunked ≤4000 chars, falls back to plain text on
parser failure).

---

## 2. Project layout

```
src/
  bot.py                  entry: run_bot + _supervise + main + small _make_* factories
  config/                 BotConfig (pydantic) + config.json loader
  i18n/                   Translator + <lang>.json files
  infra/
    agent.py              AgentSessionManager — per-chat ClaudeSDKClient cache + idle GC + hooks
    commands.py           load_commands(commands_dir) — frontmatter parser for `*.md`
    logs.py               BotLogs — general + per-chat rotating loggers (LRU)
    streaming.py          DraftStreamer — sendMessageDraft animation
    interactions/         TelegramInteractionGate package (gate.py + 4 per-flow modules)
  services/               external API clients
    transcribe.py         GroqTranscriber
    upload_store.py       UploadStore — file storage + pending queue
  ui/                     telegram-facing helpers + middleware
    agent_reply.py        react_to + reply_with_agent pipeline
    album.py              AlbumDebouncer
    markdown.py           md → MarkdownV2 + chunked send + audio_filename
    middleware.py         AclMiddleware (fail-closed)
    plan_router.py        PlanRouter
    reactions.py          ReactionPicker
    sdk_views.py          format_context_usage / format_mcp_status / format_server_info
    tool_status.py        ToolStatusMirror
  handlers/               aiogram handlers (top-level fns + register(dp))
    context.py            BotContext dataclass
    basic.py              /start, /new, /cancel, /context, /stop, /mcp, /info, /whoami, /help
    custom.py             user-defined slash commands
    plan.py               /plan + perm:/aq:/plan: callbacks
    selectors.py          /mode + /model
    text.py               F.text catch-all
    uploads.py            F.photo / F.document / F.sticker
    voice.py              F.voice / F.audio → Groq → agent
tests/                    76 pytest unit tests
pyproject.toml            build, deps, ruff/mypy/pytest config; [project.scripts] agent-bot
commands/, logs/, uploads/   created at runtime (gitignored)
```

Module-level docstring on every file describes its scope. `bot.py` is
wiring + supervision only — all feature logic lives in `ui/` and
`handlers/`. Full tree with one-line per module: see
[INSTALLATION.md §11](INSTALLATION.md#11-directory-layout).

---

## 3. Run / develop

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"           # pyproject is source of truth; [dev] pulls ruff, mypy, pytest, bandit, pip-audit

# run all bots from src/config/config.json
python -m src.bot                 # or `agent-bot` (console script)

# checks (all expected green)
ruff check src/ tests/
mypy src/ tests/ --strict
pyright src/ tests/
bandit -r src/ -q
pip-audit --strict
pytest -q                          # 76 tests
```

`pyproject.toml` declares deps, ruff rules, mypy strict mode, and the
`agent-bot` console script. Behavior beyond the unit tests is validated
by running the bot and watching `logs/<internal_name>/bot.log` + per-chat
`<chat_id>.log`.

When iterating on a running bot from inside Claude Code: use background
processes; restart via `TaskStop` of the prior task.

---

## 4. Configuration

`src/config/config.json` is a top-level dict `<internal_name>: BotConfig`.
The loader (`src/config/__init__.py`) also accepts the legacy flat format
(single bot at top level, wrapped under name `default`).

`<internal_name>` is a technical identifier for log directories and console
prefixes. It is **not** the Telegram `@username`.

**Per-field reference: [CONFIG.md](CONFIG.md)** — types, defaults, semantics,
validation rules, env overrides, examples for every `BotConfig` field.

### Access control

Fail-closed. Evaluated in this order by `is_allowed` in `bot.run_bot`:

1. `blacklist_chat_ids` — sender here → deny.
2. `allowed_for_all=true` → allow.
3. `allowed_chat_ids` — sender must be on the list.

Bootstrap when chat_id is unknown: leave `allowed_chat_ids=[]` and
`allowed_for_all=false`; message the bot — refusal text shows your
`chat_id`; add it to `allowed_chat_ids` and restart.

ACL is enforced by `ui/middleware.py:AclMiddleware`, registered as
`outer_middleware` on both `dp.message` and `dp.callback_query`. It injects
`chat_id`, `cl` (per-chat logger), and `ctx` (BotContext) into handler
kwargs — every handler signature declares these.

Gate-managed callbacks (`perm:`, `aq:`, `plan:`) **bypass** the ACL —
`TelegramInteractionGate` validates ownership itself.

### Env-var overrides

- `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>` — overrides `telegram_bot_token`.
- `GROQ_API_KEY_<INTERNAL_NAME>` — overrides `groq_api_key`.
- `GROQ_API_KEY` — fallback if the per-bot env is unset.

---

## 5. Built-in slash commands

Registered in `handlers/basic.py`, `plan.py`, `selectors.py`. Telegram menu
list is built in `bot._build_bot_command_list`.

| Command | Effect |
|---|---|
| `/start` | Greeting (`start_greeting` i18n key). |
| `/new` | Reset Claude session: close client, drop session-level allow rules, disarm `/plan`, cancel hanging AskUserQuestion. |
| `/cancel` | Clear armed `/plan` wait. Does NOT reset session, does NOT touch AskUserQuestion. Replies `nothing_to_cancel` otherwise. |
| `/context` | Show context-window usage: percentage, used / max tokens, model, top categories. |
| `/stop` | `agent.interrupt(chat_id)`. Returns `stop_ok` if a turn was running, `stop_idle` otherwise. |
| `/mode` | No arg → keyboard `default` / `acceptEdits` / `plan`. With arg → set directly. Wraps `agent.set_permission_mode`. |
| `/model` | No arg → keyboard Opus 4.7 / Sonnet 4.6 / Haiku 4.5 / default. With arg → set directly. Wraps `agent.set_model`. |
| `/plan <task>` | Engage `permission_mode="plan"` and run the turn. See `§8`. |
| `/plan` (no arg) | Arm plan mode; next text/voice becomes the prompt. |
| `/mcp` | List MCP servers grouped by status. |
| `/info` | Show output style + slash commands the SDK exposes. |
| `/whoami` | chat_id, access type, current mode, whether a session is live. |
| `/help` | List every command + example line (i18n `help_example_<cmd>`). |

Selector keyboards (`/mode`, `/model`) embed chat_id in `callback_data`
(`mode:<chat_id>:<value>`) and verify it matches the incoming chat —
guard against forged callback_data. ACL middleware does NOT check that;
the dispatcher does.

---

## 6. Input handlers (non-command)

Registration order in `handlers/__init__.py:register_all`:

1. `selectors.register` — `Command("mode"|"model")`.
2. `basic.register` — other `Command(...)` handlers.
3. `plan.register` — `Command("plan")` + three gate callbacks.
4. `custom.register(dp, commands)` — exact `Command(<name>)` per `.md`.
5. `text.register` — `F.text` catch-all.
6. `voice.register` — `F.voice | F.audio`.
7. `uploads.register` — `F.photo`, `F.document`, `F.sticker`.

`F.text` is greedy — custom commands MUST be registered before it (step
4 → 5).

### Text (`F.text`)
1. Pending `ExitPlanMode` prompt → `gate.consume_plan_text` consumes as
   rejection-with-feedback. No new agent turn.
2. `plan_router.is_armed(chat_id)` → text becomes the plan prompt.
3. Otherwise: `gate.cancel_active_aq`, react, run agent turn.

### Voice (`F.voice | F.audio`)
1. `gate.cancel_active_aq`.
2. Null `groq_api_key` → `voice_disabled`.
3. Duration > `voice_max_duration_sec` (if > 0) → `voice_too_long`.
4. Download into `SpooledTemporaryFile` (4 MB in-RAM, spill to disk).
5. POST to Groq `audio/transcriptions`; echo transcript as blockquote.
6. If plan-armed → fire plan with the transcript. Otherwise react + run.

`audio_filename(message)` picks a Groq-friendly extension. Voice →
`voice.ogg`; audio MIME → `.mp3 / .m4a / .ogg / .wav / .webm / .flac`.

### Uploads (`F.photo` / `F.document` / `F.sticker`)
1. `gate.cancel_active_aq`.
2. Null `uploads_dir` → `upload_disabled`.
3. Size check against `upload_max_bytes` (if > 0).
4. Download into `<uploads_dir>/<chat_id>/<ts>_<file_id>_<name>`.
5. `uploads.add_pending(chat_id, PendingFile)`.
6. `album.schedule(...)` — single file fires immediately; album
   (`media_group_id` set) debounced ~1.5 s. Caption from any item is
   preserved.
7. The fired turn drains `uploads.pop_pending(chat_id)` and prepends
   absolute paths via `format_attachment_prompt`.

Sticker kinds: static `.webp` → `kind="image"` (Claude reads as image);
animated `.tgs` → `kind="binary (animated sticker, Lottie JSON)"`; video
`.webm` → `kind="binary (video sticker)"`. Animated and video saved but
Claude only sees the path.

---

## 7. Custom slash commands

Drop `*.md` files into `cfg.commands_dir`. Each file = one command, named
after the file stem (or `name:` in frontmatter). `$ARGUMENTS` in the body
is replaced with whatever follows the command. Loaded once at startup;
restart to pick up changes.

Built-in names that cannot be overridden: `start, new, context, plan,
cancel, stop, mode, model, mcp, info, whoami, help`. Telegram name regex:
`^[a-z][a-z0-9_]{0,31}$`. Description trimmed to 256 chars.

**Full reference: [COMMANDS.md](COMMANDS.md).**

---

## 8. Plan mode

Two paths to engage plan mode:

1. `/plan <prompt>` — `PlanRouter.fire` sets `permission_mode="plan"` and
   runs the turn.
2. `/plan` (no args) — `PlanRouter.arm(chat_id)`. The next `F.text` or
   transcribed voice message is consumed by `PlanRouter.fire`. `/cancel`
   and `/new` both disarm.

After Claude finishes planning it calls `ExitPlanMode(plan=…)`. The gate
intercepts (NOT shown as regular Allow/Deny):

- Plan body rendered as MarkdownV2 (via `send_md_callback`).
- Separate two-button keyboard `▶️ Approve` / `❌ Reject`.
- Approve → `PermissionResultAllow()`; agent leaves plan mode.
- Reject (button) → `PermissionResultDeny` with default message; Claude
  stays in plan mode.
- Freeform text reply while buttons on screen → `consume_plan_text`
  resolves the future with typed text as feedback. Gate returns
  `PermissionResultDeny(message="User rejected the plan and provided the
  following feedback. … User feedback: <text>")`. Claude is instructed to
  revise and call `ExitPlanMode` again.

Timeout uses `approval_timeout_sec`. State in `gate._plan_pending:
dict[chat_id, (Future, request_id, prompt_msg_id)]`.

---

## 9. Permission gate

`infra/interactions.py:TelegramInteractionGate` fronts every Claude tool
call. Entry: `can_use_tool(chat_id, tool_name, tool_input, ctx)`.

Default flow (non-special-cased tool):

- Builds description from `ctx.title / description / decision_reason /
  blocked_path` + truncated `tool_input` preview.
- Sends three-button message: `Allow / Deny / Always allow this session`.
- Returns `PermissionResultAllow()` / `PermissionResultAllow(...rules)` /
  `PermissionResultDeny(message="Denied via Telegram")`.

The prompt message is **deleted** on click or timeout — no stale buttons.
Ownership check: only callbacks from the original chat are honored.

Permissions in `.claude/settings.json`, `.claude/settings.local.json`
(inside `working_dir`), and the user's global Claude Code settings are
honored (`setting_sources=["user", "project", "local"]`). Tools
whitelisted there NEVER reach the gate.

"Always allow this session" rule dies with the live `ClaudeSDKClient` (on
`/new` or restart). For persistent rules edit
`<working_dir>/.claude/settings.local.json`.

### Special-cased tools

Gate intercepts five tools before the generic Allow/Deny:

| Tool | Behavior |
|---|---|
| `AskUserQuestion` | Each question = inline keyboard. Single-select fires on tap; multi-select toggles `▫️ ↔ ☑️`, submits on `✅ Done`. `⏭ Skip` per question. Result returned as plain-text summary via `PermissionResultDeny.message`. New user message → `gate.cancel_active_aq` auto-skips remaining; tagged `(skipped)`. |
| `ExitPlanMode` | See `§8`. |
| `PushNotification` | `message` forwarded as `🔔 …` (plain, truncated to 4000 chars). Returns `PermissionResultDeny(message="Notification delivered…")` — Claude treats as success. |
| `Monitor` | Standard tool path (must run). Status mirrored via SDK Pre/Post hooks (`§10`). |
| `TaskOutput` | Same as `Monitor`. |

---

## 10. Tool status mirror

`ui/tool_status.py:ToolStatusMirror` wires SDK Pre/Post tool hooks.
`PreToolUse` (matcher=None — every tool) fires `tool_status_pre` /
`tool_status_pre_no_desc` as plain text with `disable_notification=True`;
tools in `_GATE_HANDLED_TOOLS` skipped (gate renders them).
`PostToolUse` (matcher = `"Monitor|TaskOutput"`) fires
`tool_status_post_with_preview` (6-line / 600-char tail) or
`tool_status_post`.

Per-tool brief: `TodoWrite` → `<N> todo(s)`; otherwise
`_TOOL_PRIMARY_FIELD` picks the most descriptive input field (Bash →
`command`, Read/Write/Edit → `file_path`, Grep/Glob → `pattern`,
WebFetch → `url`, …). Fallback: first scalar value excluding `content /
new_string / old_string`.

The mirror is **not** routed through aiogram middleware — fetches the
per-chat logger itself. Event comes from the SDK hook, not a dispatcher
event.

---

## 11. Streaming & MarkdownV2

`infra/streaming.py:DraftStreamer` calls `sendMessageDraft` over raw HTTP
on a `draft_interval_sec` throttle. While `text_delta` events arrive (via
`include_partial_messages=True`), the user sees an animated draft.

Final reply: `telegramify_markdown.markdownify` + `_pad_after_code_blocks`
pre-pass. Sent in chunks ≤4000 chars. On MarkdownV2 parser failure,
`send_md_to_chat` falls back to plain text using the **original** body
(not the escape-laden converted string).

`send_md(message, text)` — message-bound twin used by handlers.
`send_md_to_chat(bot, chat_id, text)` — chat_id-bound, used by the gate.

---

## 12. Sessions, modes, models

`infra/agent.py:AgentSessionManager` owns:

- `_clients: dict[chat_id, (ClaudeSDKClient, last_used_monotonic_ts)]`,
- `_locks: dict[chat_id, asyncio.Lock]` — per-chat serialization,
- `_modes: dict[chat_id, str]`, `_models: dict[chat_id, str | None]` —
  mirrors of last set values (SDK has no public getter),
- background GC closing idle clients (> `session_idle_ttl_sec`).

Key methods:
- `ask_stream(chat_id, prompt)` — yields text deltas under lock. Falls
  back to final `AssistantMessage` text blocks if no deltas.
- `set_permission_mode`, `set_model` — apply to live client without
  context loss.
- `interrupt(chat_id)` — no lock; running query holds the lock.
- `reset(chat_id)` — close client + clear mode/model mirrors.
- `close_all()` — shutdown helper.

`/new` calls `reset` and the next message opens a fresh client.
`session_idle_ttl_sec=0` disables GC entirely.

---

## 13. Logs

`infra/logs.py:BotLogs`:

- `general` — per-bot general logger. Writes
  `<logs_dir>/<internal_name>/bot.log` (rotating, 10 MB × 5), propagates
  to the root console handler.
- `for_chat(chat_id)` — lazily creates a logger writing
  `<logs_dir>/<internal_name>/<chat_id>.log` (rotating, 10 MB × 5).
  LRU-bounded by `chat_logger_capacity`.
- `_NOOP` logger returned when `logs_dir` is unset — handlers can always
  call `cl.info(...)`.

Console init: `setup_console()` in `main()`. Formats: general
`"%(asctime)s %(levelname)s %(name)s: %(message)s"`; per-chat
`"%(asctime)s %(levelname)s: %(message)s"`.

`<chat_id>.log` content: user messages (`user: …`), voice transcripts,
agent replies (`bot: …`), permission verdicts, AskUserQuestion picks, plan
approve/reject events, tool hooks (`hook pre|post: …`), mode/model
changes, upload saves, album fires, errors. Complete audit trail of one
conversation.

---

## 14. i18n

`src/i18n/<lang>.json`. `Translator(lang).t(key, **kwargs)` formats with
`str.format`. Missing keys return the key itself, so callers can detect
"unknown key" (used by `format_mcp_status` and `show_help`).

Bundled top-20: `ar bn de en es fr hi id ja ko mr pt ru sw ta te tr ur vi
zh`. Add a new language by dropping a JSON file with the same keys; falls
back to the default file if a key is missing.

`system_prompt` is **separate** from `lang` — `lang` only controls
bot-rendered strings; what language Claude responds in is governed by
`system_prompt`. The default `system_prompt` in each language file tells
Claude to reply in that language.

---

## 15. Multi-bot concurrency

`main()` loads `config.json`, runs every bot under `asyncio.gather` over
`_supervise(cfg, http)` (exponential-backoff loop, 1s → 60s).
`CancelledError` propagates so Ctrl-C works. Shared `aiohttp.ClientSession`
across all bots; each bot owns its own `Dispatcher`, `BotContext`, agent
manager, gate, streamer, logs.

---

## 16. Conventions for contributors

- All code comments and docstrings in **English**.
- User-facing strings must go through `Translator.t(key, **kwargs)` — do
  not hardcode them.
- `src/config/config.json`, `logs/`, `uploads/`, `commands/` are
  gitignored. `src/config/config.example.json` is the template.
- The bot is **fail-closed**: any code path letting a message reach the
  agent without `is_allowed` is a bug. New handlers declare `ctx:
  BotContext, cl: logging.Logger, chat_id: int` and rely on injection.
- Custom permission rules belong in
  `<working_dir>/.claude/settings.local.json`. "Always allow this session"
  only lives until the next `/new` or restart.
- After significant changes: `find src -name '*.py' -not -path
  '*/__pycache__/*' -print0 | xargs -0 python -m py_compile` and `pytest
  -q`. Then start the bot and exercise the affected feature in Telegram.

---

## 17. Tests

`tests/` covers pure modules: `test_commands.py` (loader parser),
`test_config.py` (`BotConfig` multi-bot / flat-format / env overrides /
access eval), `test_i18n.py` (`Translator` fallbacks), `test_uploads.py`
(`UploadStore` layout + queue + prompt formatting).

`pytest -q` from repo root. No external services. Not unit-tested:
aiogram handler wiring, live Claude SDK, streaming, Bot API integration —
validated by running the bot.

---

## 18. Glossary

- **chat_id** — Telegram chat id (int). Private = user_id; group = negative; channel = below `-1e12`.
- **internal_name** — top-level key in `config.json`. Used for log dirs, console prefixes, env-var lookups, supervisor identity.
- **plan mode** — `permission_mode="plan"`. Claude generates a plan, calls `ExitPlanMode(plan=…)` for approval.
- **gate** — `TelegramInteractionGate`. Boundary between `can_use_tool` and the Telegram chat.
- **draft** — Telegram message via `sendMessageDraft`. Used by `DraftStreamer` to animate replies.
- **album** — Telegram media group sharing `media_group_id`. Debounced ~1.5 s so the agent fires once.
- **BotContext** — frozen dataclass in `ui/context.py` holding every per-bot dependency. Injected by the ACL middleware.
