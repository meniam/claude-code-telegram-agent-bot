# Installing agent-bot

Python Telegram bot that talks to Claude through the Claude Agent SDK + aiogram.

## Requirements

- **macOS / Linux** (Windows is not tested).
- **Python 3.10+** (3.11 / 3.12 / 3.14 recommended).
- **Node.js 18+** — needed once to install the Claude Code CLI and run `claude login`. The SDK ships a bundled `claude` for runtime use.
- A **Telegram** account and a bot token from [@BotFather](https://t.me/BotFather).
- A **Claude / Max / Team** subscription ([claude login](https://docs.anthropic.com/en/docs/claude-code/setup)) **or** an API key from [console.anthropic.com](https://console.anthropic.com/).
- *(optional)* A **Groq API key** from [console.groq.com/keys](https://console.groq.com/keys) — required only if you want voice/audio messages to be transcribed.

## 1. Install the Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

Authenticate:

```bash
claude login
```

Credentials are stored in `~/.claude/`. The bundled SDK copy picks them up automatically. Alternative — export `ANTHROPIC_API_KEY` before launching the bot.

## 2. Get a Telegram bot token

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. `/newbot` → set a name and `@username`.
3. Copy the issued token (format: `123456789:AA...`).

Keep the Telegram bot token secret — it lives in `src/config/config.json`, which is in `.gitignore`.

## 3. Clone the project and install dependencies

```bash
git clone <repo-url> agent-bot
cd agent-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Create the config

```bash
cp src/config/config.example.json src/config/config.json
```

Open `src/config/config.json` and fill in at least one section:

```json
{
  "brain": {
    "telegram_bot_token": "123456:ABC...",
    "system_prompt": null,
    "lang": "en",
    "allowed_chat_ids": [],
    "working_dir": "/Users/me/Projects/some-project",
    "logs_dir": "/Users/me/Projects/agent-bot/logs",
    "draft_interval_sec": 0.2,
    "approval_timeout_sec": 300
  }
}
```

| Field | What it sets |
|---|---|
| `telegram_bot_token` | Token from @BotFather. **Required.** |
| `system_prompt` | System prompt for Claude. `null` → fallback to translation `default_system_prompt`. |
| `lang` | UI language for bot-facing strings. Bundled (top-20 world languages): `en`, `zh`, `hi`, `es`, `ar`, `fr`, `bn`, `pt`, `ru`, `ur`, `id`, `de`, `ja`, `sw`, `mr`, `te`, `tr`, `ta`, `vi`, `ko`. Default: `ru`. |
| `allowed_for_all` | Boolean. Default `false`. Set to `true` only if you intend the bot to be public — the access gate then accepts every non-blacklisted chat. Logged as a warning at startup. |
| `allowed_chat_ids` | Telegram chat IDs allowed to talk to the bot. `null` / missing / `[]` → **fail-closed**, nobody is allowed (bot replies with the refusal containing the sender's chat_id). `[id, id, ...]` → whitelist. Ignored when `allowed_for_all: true`. |
| `blacklist_chat_ids` | Telegram chat IDs that are always denied. Wins over `allowed_for_all` and the whitelist. `null` / missing / `[]` → no blacklist. |
| `working_dir` | Claude Code working directory (`null` → process cwd). A non-existent path causes a startup error. |
| `logs_dir` | Root log directory. `null` → console only. Otherwise `<logs_dir>/<internal_name>/bot.log` + `<chat_id>.log` are written. |
| `draft_interval_sec` | Minimum seconds between draft-message updates while streaming. Default `0.2`. |
| `approval_timeout_sec` | Seconds to wait for a permission reply before auto-deny. Default `300`. |
| `agent_timeout_sec` | Hard timeout per Claude turn (seconds). Default `180`. |
| `session_idle_ttl_sec` | Idle TTL for a per-chat `ClaudeSDKClient`; closed by background GC after this many seconds without traffic. Default `86400` (24 h). Set `0` to disable. |
| `chat_logger_capacity` | Max number of per-chat file loggers kept in memory (LRU). Default `256`. |
| `groq_api_key` | Groq API key for voice/audio transcription. Override via env `GROQ_API_KEY_<INTERNAL_NAME>` or fallback `GROQ_API_KEY`. `null` / missing → voice handler is disabled. |
| `groq_model` | Whisper model on Groq. Default `whisper-large-v3-turbo`. |
| `groq_timeout_sec` | HTTP timeout for the transcription call. Default `60.0`. |
| `voice_max_duration_sec` | Reject voice/audio longer than this (seconds). Default `600`. `0` disables the cap. |
| `uploads_dir` | Directory for incoming `photo` / `document` / `sticker` files. `null` / missing → uploads are disabled. The path is also forwarded to `ClaudeAgentOptions(add_dirs=[...])` so Claude's `Read` works without a permission prompt. |
| `upload_max_bytes` | Reject uploads larger than this (bytes). Default `20971520` (20 MB). `0` disables the local check. |

`internal_name` is the top-level key (`brain` in the example). The log subdirectory is named after it.

### Multiple bots

You can declare several sections — each one runs concurrently inside the same process:

```json
{
  "brain": { "telegram_bot_token": "...", "working_dir": "/path/A" },
  "research": { "telegram_bot_token": "...", "working_dir": "/path/B" }
}
```

## 5. Run

```bash
source .venv/bin/activate
python -m src.bot
```

You should see in the console:

```
INFO root: loaded 1 bot(s): brain
INFO bot.brain: [brain] starting as @YourBot
INFO aiogram.dispatcher: Run polling for bot @YourBot ...
```

Open the bot in Telegram → `/start` → ask a question. The bot will:

1. Set an emoji reaction on your message.
2. Stream Claude's reply through `sendMessageDraft` (typing animation).
3. Send the final response as a separate MarkdownV2 message.

Commands:

- `/start` — greeting.
- `/new` — start a fresh Claude Code session (context is dropped).

Voice / audio messages are transcribed via Groq when `groq_api_key` is set — the bot echoes the transcript as a blockquote and runs the same agent flow on the recognized text.

Photos, documents and stickers are saved under `<uploads_dir>/<chat_id>/` when `uploads_dir` is set. The agent runs right after the upload — caption (if any) acts as the user prompt; otherwise Claude gets just the file paths and is told to inspect them. Albums are debounced ~1.5 s so a multi-photo upload becomes one agent turn. Claude reads the saved files via the `Read` tool; `uploads_dir` is passed to the SDK as `add_dirs`, so reads are not blocked even when the path is outside `working_dir`.

## 6. Restricting who can talk to the bot

The bot is **fail-closed by default**: a missing or empty whitelist means nobody is allowed.

Set `allowed_chat_ids` to a list of Telegram chat IDs that may use the bot:

```json
"allowed_chat_ids": [123456789, 987654321]
```

Full semantics — gate evaluates in this order: `blacklist_chat_ids` → `allowed_for_all` → `allowed_chat_ids`.

| `allowed_for_all` | `allowed_chat_ids` | `blacklist_chat_ids` | Effect for sender X |
|---|---|---|---|
| `false` (default) | `null` / missing / `[]` | any | Closed to everyone. |
| `false` (default) | `[id, ...]` | `[]` | Whitelist only. Outsiders get the refusal. |
| `false` (default) | `[id, ...]` | `[X, ...]` | X is denied even if whitelisted. |
| `true`            | anything             | `[]` | Open to everyone. Startup logs a warning. |
| `true`            | anything             | `[X, ...]` | Open to everyone *except* X (and other blacklisted IDs). |

When someone outside the list sends a message, the bot replies with their `chat_id` and instructions to forward it to the administrator. The admin adds the ID to `config.json` and restarts the process.

To find your own chat_id quickly: leave `allowed_chat_ids` empty (`[]`), send a message, copy the `chat_id` from the refusal, then add it.

To kick a misbehaving user from a public bot: add their ID to `blacklist_chat_ids` and restart. The blacklist beats `allowed_for_all`.

> **Public bots**: only set `"allowed_for_all": true` if you really want anyone on Telegram to drive the agent on `working_dir`. Combined with permission rules in `.claude/settings.local.json`, this is how you'd run a read-only public assistant. For everything else, use the whitelist.

## 7. Voice transcription (optional)

Voice notes and `audio` attachments are routed to Groq's OpenAI-compatible `audio/transcriptions` endpoint. Setup:

1. Create a key at [console.groq.com/keys](https://console.groq.com/keys).
2. Either drop it into `config.json` as `groq_api_key`, or export `GROQ_API_KEY=...` (per-bot override: `GROQ_API_KEY_<INTERNAL_NAME>`).
3. Restart the process — startup logs print `groq transcription enabled (model=...)` when the feature is active.

Tunables:

- `groq_model` — default `whisper-large-v3-turbo`. Switch to `whisper-large-v3` for higher accuracy.
- `groq_timeout_sec` — HTTP timeout. Default `60.0`.
- `voice_max_duration_sec` — drop audio longer than this. Default `600`. `0` disables the cap.

Behaviour without a key: any voice/audio message gets a one-line refusal (`voice_disabled`) and is not forwarded to the agent.

## 8. File uploads (optional)

Photos, documents and stickers are saved under `<uploads_dir>/<chat_id>/<timestamp>_<file_id>_<original_name>`. Setup:

1. Add `"uploads_dir": "/var/lib/telegram-agent-bot/uploads"` (or any writable absolute path) to `config.json`.
2. Make sure the user running the bot can write to that directory and the user running Claude Code can read from it (in the simple case both are the same user).
3. Restart — startup logs print `uploads enabled at <path>` when the feature is active.

Flow:

- Single file → agent fires immediately. The caption (if any) becomes the user prompt; otherwise Claude gets only the file paths and is asked to use `Read`.
- Album (multiple photos in one Telegram message) → debounced ~1.5 s. The agent runs once after the last item lands. Caption from any item in the album is preserved.
- Subsequent text/voice messages also drain anything still queued, so files attached at different times can be combined into one prompt.

Filename layout per file kind:

| Telegram type | Saved as | `kind` shown to Claude |
| --- | --- | --- |
| `photo` (compressed) | `<ts>_<file_id>_photo.jpg` | `image` |
| `document` | `<ts>_<file_id>_<original file name>` | `document` |
| `sticker`, static (`.webp`) | `<ts>_<file_id>_sticker_<set>.webp` | `image` |
| `sticker`, animated (`.tgs`) | `<ts>_<file_id>_sticker_<set>.tgs` | `binary (animated sticker, Lottie JSON)` |
| `sticker`, video (`.webm`) | `<ts>_<file_id>_sticker_<set>.webm` | `binary (video sticker)` |

Permissions: `uploads_dir` is forwarded to `ClaudeAgentOptions(add_dirs=[...])`. Files saved there can be read by Claude's `Read` tool without triggering the inline-button permission prompt — even when `uploads_dir` is outside `working_dir`. Other tools acting on those paths (`Edit`, `Bash`) still go through the gate as usual.

Tunables:

- `upload_max_bytes` — reject uploads larger than this. Default `20971520` (20 MB). `0` disables the local check.

Behaviour without `uploads_dir`: every `photo` / `document` / `sticker` gets `upload_disabled` and the file is **not** saved.

## 9. Permission prompts

When Claude wants to use an `ask`-level tool (Bash, Write, Edit, etc.) the bot sends inline buttons:

- **✅ Allow** — once.
- **🚫 Deny** — once.
- **♾️ Always allow this session** — adds an `addRules / behavior=allow / destination=session` rule. Reset on `/new` or restart.

To allow a tool persistently (across restarts), add a rule manually to `<working_dir>/.claude/settings.local.json`:

```json
{
  "permissions": {
    "allow": ["Read", "Edit", "Bash(ls:*)"]
  }
}
```

The SDK loads `user/project/local` settings — those rules never reach the permission gate.

The prompt message is **deleted** as soon as you click any button (or on timeout). The verdict shows up as a Telegram callback toast, so the chat does not pile up with stale button rows.

### `AskUserQuestion` (interactive quiz)

Claude's built-in `AskUserQuestion` tool is intercepted before it would otherwise hit the Allow/Deny/Always gate. The bot walks the `questions` array sequentially:

- Single-select: each option is a button — first tap fires the answer.
- Multi-select: option buttons toggle `▫️ ↔ ☑️`. The bottom row carries `✅ Done` to submit.
- Every question has a `⏭ Skip` button.

Result handed back to Claude (as the tool response):

```
User responded to AskUserQuestion via Telegram inline buttons:

1. <question text>
   → 'option label A'

2. <question text>
   → (skipped)
```

Per-question timeout reuses `approval_timeout_sec` (default 300 s). On expiry the bot sends a short notice and tells Claude no answer was given.

Sending any new message (text / voice / photo / document / sticker) while a quiz is mid-flight auto-skips the rest of the questions — the old turn finishes immediately and the new message is processed. This avoids a deadlock when you start typing a follow-up instead of clicking a stale button.

## 10. Directory layout

```
agent-bot/
├── src/
│   ├── __init__.py
│   ├── bot.py              # entry point: python -m src.bot
│   ├── agent.py            # AgentSessionManager (per-chat ClaudeSDKClient + idle GC)
│   ├── streaming.py        # DraftStreamer
│   ├── permissions.py      # TelegramPermissionGate
│   ├── reactions.py        # keyword → emoji reactions
│   ├── transcribe.py       # GroqTranscriber (voice/audio → text)
│   ├── uploads.py          # UploadStore: saves photos/documents + per-chat queue
│   ├── logs.py             # BotLogs: bot.log + per-chat files
│   ├── config/
│   │   ├── __init__.py     # BotConfig, load()
│   │   ├── config.json             # real config, .gitignore'd
│   │   └── config.example.json     # template
│   └── i18n/
│       ├── __init__.py     # Translator
│       └── <lang>.json     # ru, en, zh, hi, es, ar, fr, bn, pt, ur, id, de, ja, sw, mr, te, tr, ta, vi, ko
├── logs/                   # auto-created when logs_dir is set
├── CLAUDE.md
├── INSTALLATION.md
├── README.md
├── requirements.txt
└── .gitignore
```

## 11. Updating

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

Restart the process so changes take effect.

## Troubleshooting

| Symptom | Check |
|---|---|
| `config.json not found` | Did you copy `config.example.json` → `config.json`? Is it inside `src/config/`? |
| `telegram_bot_token is missing` | Placeholder `put-...` left in place or field is empty. |
| `working_dir does not exist` | `working_dir` is not a directory. Fix it or set to `null`. |
| `claude login` complains | Is `@anthropic-ai/claude-code` installed? Does `which claude` resolve? |
| Bot is silent | Check logs at `logs/<internal_name>/bot.log`. Confirm `Run polling` appeared. |
| Permission prompt every time | No rules in `.claude/settings.local.json` inside `working_dir`. Or use the "Always" button in chat. |
| No streaming animation | `sendMessageDraft` is a recent Bot API method. Make sure your Telegram client is up to date. |
| Voice message gets `voice_disabled` reply | `groq_api_key` not set in `config.json` and no `GROQ_API_KEY` / `GROQ_API_KEY_<NAME>` env var. |
| Transcription returns `voice_error` | Check `bot.log` — usually a bad API key, exhausted quota, or unsupported audio format. |
| Voice longer than expected gets rejected | Bump `voice_max_duration_sec` (default `600`s) or set it to `0`. |
| Photo/document gets `upload_disabled` | `uploads_dir` not set in `config.json`. Add it and restart. |
| `upload_too_large` reply | Raise `upload_max_bytes` or set `0` to disable the local check. The Telegram Bot API hard cap is 20 MB without a self-hosted Bot API server. |
| Claude says it cannot find the file | Confirm Claude Code's user can read `uploads_dir`. The path printed in logs (`upload saved: ... path=...`) must be reachable from `working_dir` user context. |
| `Read` permission prompt for an uploaded file | The bot wires `uploads_dir` into `ClaudeAgentOptions(add_dirs=[...])` automatically. If you still see prompts, you either restarted before this fix or `uploads_dir` is unset — re-check `config.json` and the startup log line `uploads enabled at <path>`. |
| Quiz buttons appear but nothing happens after click | Make sure the chat's `chat_id` is in `allowed_chat_ids` (the callback authz check rejects out-of-chat clicks). Restart if you upgraded across the AskUserQuestion patch. |
| New message ignored while a quiz is on screen | Old behaviour. After the auto-skip patch, any new message cancels the running quiz and is processed in the same turn. Restart the bot if you are running the pre-patch build. |
