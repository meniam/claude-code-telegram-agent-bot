# CONFIG.md

Complete reference for `src/config/config.json` fields. Schema is enforced by
`BotConfig` (pydantic v2) in [src/config/__init__.py](src/config/__init__.py)
with `extra="forbid"` — unknown keys cause a startup error.

For an end-to-end project tour see [AGENTS.md](AGENTS.md). This file is
field-by-field reference only.

---

## 1. File format

`config.json` is a JSON object mapping `<internal_name>` → bot config:

```json
{
    "brain": {
        "telegram_bot_token": "123456:ABC...",
        "lang": "ru",
        "allowed_chat_ids": [123456789]
    },
    "research": {
        "telegram_bot_token": "789012:DEF...",
        "lang": "en",
        "allowed_for_all": true,
        "blacklist_chat_ids": [555]
    }
}
```

Every top-level key launches one bot in the same process via
`asyncio.gather`. `<internal_name>` is used for log directories
(`<logs_dir>/<internal_name>/`), console message prefixes, and env-var
overrides (uppercased). It is **not** the Telegram `@username`.

### Legacy flat format

If the top-level has `telegram_bot_token` directly:

```json
{
    "telegram_bot_token": "123456:ABC...",
    "lang": "ru"
}
```

it is wrapped under the name `default`. New configs should use the
multi-bot form even with a single bot.

### File location

Default path: `src/config/config.json`. The loader fails with
`FileNotFoundError` if the file does not exist. Copy
`src/config/config.example.json` → `src/config/config.json` to start.

`.gitignore` excludes `config.json` — keep tokens out of the repo.

---

## 2. Environment overrides

Two fields can be overridden at runtime via env vars:

| Env var | Overrides | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>` | `telegram_bot_token` | `<INTERNAL_NAME>` is uppercased. Empty / placeholder values are rejected at startup. |
| `GROQ_API_KEY_<INTERNAL_NAME>` | `groq_api_key` | Same uppercase convention. |
| `GROQ_API_KEY` | `groq_api_key` (fallback) | Used only when the per-bot var is unset. |

Tokens starting with `put-` are treated as placeholders. A placeholder
`telegram_bot_token` raises a startup error; a placeholder `groq_api_key`
silently disables voice transcription for that bot.

No other fields support env overrides — change them in `config.json`.

---

## 3. Field reference

Each section: type, default, semantics, valid values, related fields.

### `telegram_bot_token` — required

- **Type:** `SecretStr` (kept opaque in logs, accessed via
  `.get_secret_value()`).
- **Default:** none. Missing or empty → startup error.
- **Source:** [@BotFather](https://t.me/BotFather) — `/newbot` → token.
- **Env override:** `TELEGRAM_BOT_TOKEN_<INTERNAL_NAME>`.
- **Format:** `<id>:<secret>`, e.g. `123456:AABBCCDD…`.
- **Placeholder check:** values starting with `put-` are rejected with
  `[<name>] telegram_bot_token is missing (or still a placeholder).`

### `system_prompt`

- **Type:** `str | null`.
- **Default:** `null` → falls back to translation key
  `default_system_prompt` for the chosen `lang`.
- **Semantics:** Claude's system prompt. Controls reply language and
  personality. Overrides the i18n default when set.
- **Tip:** Use this to set a per-bot personality / role. UI strings are
  controlled separately by `lang`.

### `lang`

- **Type:** `str`.
- **Default:** `"ru"`.
- **Valid values:** any filename without the `.json` suffix in
  `src/i18n/`. Bundled: `ar bn de en es fr hi id ja ko mr pt ru sw ta te
  tr ur vi zh`.
- **Semantics:** UI language for bot-facing strings (greetings, button
  labels, permission prompts, error messages). Does **not** affect Claude's
  reply language — that is governed by `system_prompt`.
- **Normalization:** lowercased before lookup.
- **Missing language fallback:** `Translator` returns the key string when a
  message is missing.

### `working_dir`

- **Type:** `str | null`.
- **Default:** `null` → Claude uses the process cwd.
- **Semantics:** forwarded to `ClaudeAgentOptions(cwd=...)`. Claude reads /
  writes files relative to this directory and picks up
  `.claude/settings.json` and `.claude/settings.local.json` from here.
- **Validation:** `~/...` is expanded. The directory must exist (a missing
  path raises a startup error: `[<name>] working_dir does not exist or is
  not a directory: <path>`).
- **Resolution:** stored as an absolute path (`Path.resolve()`).

### `logs_dir`

- **Type:** `str | null`.
- **Default:** `null` → console-only logging.
- **Semantics:** when set, the bot writes:
  - `<logs_dir>/<internal_name>/bot.log` — general bot log, 10 MB × 5
    rotation.
  - `<logs_dir>/<internal_name>/<chat_id>.log` — per-chat audit log, 10 MB
    × 5 rotation.
- **Validation:** `~/...` expanded; directory is created automatically
  (`mkdir(parents=True, exist_ok=True)`).
- **Console:** general events also reach the shared console handler — the
  file is additive.
- **See also:** `chat_logger_capacity` (cap on cached per-chat loggers).

### `draft_interval_sec`

- **Type:** `float`.
- **Default:** `0.2`.
- **Semantics:** minimum seconds between `sendMessageDraft` updates while
  Claude streams tokens. Lower = smoother animation, more Bot API calls.
- **Valid range:** any positive float. `0` is allowed but burns API budget.
- **Throttle implementation:** [src/infra/streaming.py](src/infra/streaming.py).

### `approval_timeout_sec`

- **Type:** `int`.
- **Default:** `300`.
- **Semantics:** how long the gate waits for the user's click on:
  - permission prompts (Allow / Deny / Always),
  - `ExitPlanMode` Approve / Reject buttons,
  - each `AskUserQuestion` keyboard.
- **On timeout:** treated as deny (permission), reject (plan), or "no
  selection" (AQ). The prompt message is deleted and a `*_timeout` i18n
  message is sent.

### `agent_timeout_sec`

- **Type:** `int`.
- **Default:** `600`.
- **Semantics:** hard upper bound on one Claude turn from query to final
  response. Wraps `streamer.stream(...)` inside `asyncio.wait_for`.
- **Includes:** generation time + tool calls + user reaction time on
  permission prompts (since they pause the turn).
- **On timeout:** bot sends `agent_timeout` i18n message and abandons the
  turn. The next user message starts a new turn (the live session
  continues — no implicit `/new`).
- **Tip:** raise above the slowest expected scenario. Plan mode + slow
  clicks easily exceed the previous 180s default; the bumped default is
  now 600s.

### `session_idle_ttl_sec`

- **Type:** `int`.
- **Default:** `86400` (24 hours).
- **Semantics:** idle TTL for the per-chat `ClaudeSDKClient`. A background
  GC task runs every `max(min(ttl/4, 60), 5)` seconds and closes clients
  whose last activity is older than the TTL.
- **`0`:** disables the GC entirely. Live clients survive until process
  restart, `/new`, or explicit `agent.reset(chat_id)`.
- **On eviction:** the client's `__aexit__` is called; mode/model mirrors
  are dropped. The next message opens a fresh client with no context.
- **Race:** if a chat acquired the lock right before GC, it is skipped and
  retried on the next pass.

### `chat_logger_capacity`

- **Type:** `int`.
- **Default:** `256`.
- **Semantics:** LRU cap on cached per-chat file loggers (`BotLogs`). When
  exceeded, the least-recently-used logger's handler is closed (releasing
  the file descriptor) and removed from the global logger registry. The
  log file on disk is untouched and reopens lazily on the next message
  from that chat.
- **Tune up** when running with many concurrent chats and a generous OS fd
  limit.

### `groq_api_key`

- **Type:** `SecretStr | null`.
- **Default:** `null` → voice / audio handler replies with
  `voice_disabled`.
- **Env override:** `GROQ_API_KEY_<INTERNAL_NAME>`, fallback
  `GROQ_API_KEY`.
- **Source:** https://console.groq.com.
- **Placeholder check:** values starting with `put-` are treated as
  `null`.

### `groq_model`

- **Type:** `str`.
- **Default:** `"whisper-large-v3-turbo"`.
- **Valid values:** any model id Groq accepts on `audio/transcriptions`.
  Practical picks:
  - `whisper-large-v3-turbo` — faster / cheaper (default).
  - `whisper-large-v3` — higher quality, slower.

### `groq_timeout_sec`

- **Type:** `float`.
- **Default:** `60.0`.
- **Semantics:** HTTP timeout for the transcription POST.
- **On timeout:** voice handler replies with `voice_error` i18n.

### `voice_max_duration_sec`

- **Type:** `int`.
- **Default:** `600` (10 minutes).
- **Semantics:** voice / audio longer than this is rejected with
  `voice_too_long` i18n. The cap is checked against
  `message.voice.duration` / `message.audio.duration` (Telegram-reported
  duration in seconds).
- **`0`:** disables the check.
- **Tip:** Groq enforces its own per-request limits (and you pay per
  minute) — keep this lower than Groq's hard cap to fail fast.

### `uploads_dir`

- **Type:** `str | null`.
- **Default:** `null` → photo / document / sticker handlers reply with
  `upload_disabled`.
- **Semantics:** directory where incoming files are saved. Layout:
  `<uploads_dir>/<chat_id>/<timestamp>_<file_id>_<safe_name>`.
- **Validation:** `~/...` expanded; directory is created automatically.
- **Bonus effect:** the absolute path is passed to
  `ClaudeAgentOptions(add_dirs=[...])`, so Claude's `Read` tool can open
  files under this dir without the permission gate firing.
- **File handling:**
  - photos: largest available size, saved as `<…>_photo.jpg`,
    `kind="image"`.
  - documents: original filename preserved (filtered to
    `[A-Za-z0-9._-]`), `kind="document"`.
  - static stickers (`.webp`): saved as image (`kind="image"`).
  - animated stickers (`.tgs`): `kind="binary (animated sticker, Lottie
    JSON)"` — Claude only sees the path.
  - video stickers (`.webm`): `kind="binary (video sticker)"` — same.

### `upload_max_bytes`

- **Type:** `int`.
- **Default:** `20971520` (20 MiB — Telegram Bot API cap without a
  self-hosted Bot API server).
- **Semantics:** reject uploads larger than this. Size is taken from
  `file_size` on the inbound message (Telegram-reported; not always
  exact). On rejection: `upload_too_large` i18n.
- **`0`:** disables the local check. Telegram's 20 MB cap still applies
  unless you run a self-hosted Bot API server.

### `commands_dir`

- **Type:** `str | null`.
- **Default:** `null` → no extra commands beyond the built-ins.
- **Semantics:** directory of `*.md` files. Each file becomes one
  Telegram slash command. Body is sent to Claude as the prompt when the
  user types `/<name>`.
- **Validation:** `~/...` expanded; the directory must exist (a missing
  path raises a startup error: `[<name>] commands_dir does not exist or
  is not a directory: <path>`).
- **Format:** see [COMMANDS.md](COMMANDS.md).
- **Loaded once at startup.** Restart to pick up edits / new files.

### `allowed_for_all`

- **Type:** `bool`.
- **Default:** `false`.
- **Semantics:** when `true`, every chat that is not in
  `blacklist_chat_ids` is allowed. `allowed_chat_ids` is ignored.
- **Side effect:** startup log emits a `WARNING`:
  `[<name>] access: OPEN TO EVERYONE (allowed_for_all=true)`.
- **Only set for genuinely public bots.** Combine with a real blacklist
  for incident response.

### `allowed_chat_ids`

- **Type:** `tuple[int, ...]` (parsed from a JSON array of ints).
- **Default:** `()` — empty.
- **Semantics:** whitelist of allowed Telegram chat IDs. Ignored when
  `allowed_for_all=true`.
- **Fail-closed:** missing key, `null`, and `[]` all mean **nobody is
  allowed**. Outsiders get a refusal containing their `chat_id` so they
  can forward it to the admin.
- **Validation:** must be a JSON array of integers. Anything else raises
  `[<name>] allowed_chat_ids must be null or a list of integers`.

### `blacklist_chat_ids`

- **Type:** `tuple[int, ...]`.
- **Default:** `()` — empty.
- **Semantics:** always-denied chat IDs. **Beats both `allowed_for_all`
  and the whitelist.**
- **Use case:** emergency kill-switch on a public bot, or per-user revoke
  without flipping the whole config.
- **Validation:** same as `allowed_chat_ids`.

---

## 4. Access control matrix

`is_allowed(chat_id)` returns `True` iff:
1. `chat_id NOT in blacklist_chat_ids`, AND
2. `allowed_for_all=true` OR `chat_id in allowed_chat_ids`.

| `allowed_for_all` | `allowed_chat_ids` | `blacklist_chat_ids` | Effect for sender X |
|---|---|---|---|
| `false` | `null` / missing / `[]` | any | Closed to everyone. Every message returns the refusal. |
| `false` | `[id, ...]` | `[]` | Whitelist. Only listed chats may use the bot. |
| `false` | `[id, ...]` | `[X, ...]` | X denied even if whitelisted. |
| `true` | anything | `[]` | Open to everyone. Logged as warning. |
| `true` | anything | `[X, ...]` | Open to all except X (and other blacklisted IDs). |

Bootstrap when you don't know your `chat_id`:

1. Leave `allowed_chat_ids` empty / missing, `allowed_for_all=false`.
2. Start the bot, message it — the refusal text shows your `chat_id`.
3. Set `"allowed_chat_ids": [<your_chat_id>]`, restart.

---

## 5. Validation summary

The loader enforces, at startup:

- `extra="forbid"` — unknown fields raise.
- Token presence and non-placeholder.
- `working_dir` exists and is a directory.
- `commands_dir` exists and is a directory.
- `logs_dir` / `uploads_dir` are created if absent.
- `allowed_chat_ids` / `blacklist_chat_ids` are JSON arrays of integers
  (or null).
- `allowed_for_all` is a boolean.
- `lang` is lowercased.

Anything failing raises a clear `ValueError` containing the bot name —
fail-fast on misconfiguration.

---

## 6. Minimum viable config

```json
{
    "brain": {
        "telegram_bot_token": "123456:ABC...",
        "allowed_chat_ids": [123456789]
    }
}
```

Everything else defaults safely:
- `lang=ru` (Russian UI),
- `system_prompt=null` (uses i18n default),
- `working_dir=null` (process cwd),
- `logs_dir=null` (console only),
- `groq_api_key=null` (voice disabled),
- `uploads_dir=null` (uploads disabled),
- `commands_dir=null` (no custom commands),
- `agent_timeout_sec=600`, `approval_timeout_sec=300`,
- `session_idle_ttl_sec=86400`.

---

## 7. Full example

```json
{
    "brain": {
        "telegram_bot_token": "123456:ABC...",
        "system_prompt": "You are a friendly Telegram assistant. Be concise.",
        "lang": "en",
        "allowed_chat_ids": [123456789, 987654321],
        "blacklist_chat_ids": [],
        "working_dir": "/home/brain/workdir",
        "logs_dir": "/var/log/telegram-agent-bot",
        "draft_interval_sec": 0.2,
        "approval_timeout_sec": 300,
        "agent_timeout_sec": 600,
        "session_idle_ttl_sec": 86400,
        "chat_logger_capacity": 256,
        "groq_api_key": "gsk_...",
        "groq_model": "whisper-large-v3-turbo",
        "groq_timeout_sec": 60.0,
        "voice_max_duration_sec": 600,
        "uploads_dir": "/var/lib/telegram-agent-bot/uploads",
        "upload_max_bytes": 20971520,
        "commands_dir": "/etc/telegram-agent-bot/commands"
    },
    "public_demo": {
        "telegram_bot_token": "789012:DEF...",
        "system_prompt": "You are a polite public demo bot. Answer briefly.",
        "lang": "en",
        "allowed_for_all": true,
        "blacklist_chat_ids": [555000111],
        "working_dir": "/srv/public-demo",
        "logs_dir": "/var/log/telegram-agent-bot",
        "approval_timeout_sec": 60,
        "agent_timeout_sec": 180
    }
}
```

The first bot is private (whitelist) with all features enabled. The
second is public, with one banned chat, a tighter approval timeout, and
no voice / uploads / custom commands.
