# Installing agent-bot

Python Telegram bot that talks to Claude through the Claude Agent SDK + aiogram.

## Requirements

- **macOS / Linux** (Windows is not tested).
- **Python 3.10+** (3.11 / 3.12 / 3.14 recommended).
- **Node.js 18+** — needed once to install the Claude Code CLI and run `claude login`. The SDK ships a bundled `claude` for runtime use.
- A **Telegram** account and a bot token from [@BotFather](https://t.me/BotFather).
- A **Claude / Max / Team** subscription ([claude login](https://docs.anthropic.com/en/docs/claude-code/setup)) **or** an API key from [console.anthropic.com](https://console.anthropic.com/).

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
| `allowed_chat_ids` | Telegram chat IDs allowed to talk to the bot. `null` / missing = open to everyone. `[]` = closed to everyone. `[id, id, ...]` = whitelist. Outsiders get a refusal containing their chat_id. |
| `working_dir` | Claude Code working directory (`null` → process cwd). A non-existent path causes a startup error. |
| `logs_dir` | Root log directory. `null` → console only. Otherwise `<logs_dir>/<internal_name>/bot.log` + `<chat_id>.log` are written. |
| `draft_interval_sec` | Minimum seconds between draft-message updates while streaming. Default `0.2`. |
| `approval_timeout_sec` | Seconds to wait for a permission reply before auto-deny. Default `300`. |
| `agent_timeout_sec` | Hard timeout per Claude turn (seconds). Default `180`. |
| `session_idle_ttl_sec` | Idle TTL for a per-chat `ClaudeSDKClient`; closed by background GC after this many seconds without traffic. Default `3600`. Set `0` to disable. |
| `chat_logger_capacity` | Max number of per-chat file loggers kept in memory (LRU). Default `256`. |

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

## 6. Restricting who can talk to the bot

Set `allowed_chat_ids` to a list of Telegram chat IDs that may use the bot:

```json
"allowed_chat_ids": [123456789, 987654321]
```

Semantics:

- `null` or missing field — open to everyone (default).
- `[]` — closed to everyone.
- `[id, id, ...]` — only listed chat IDs are allowed.

When someone outside the list sends a message, the bot replies with their `chat_id` and instructions to forward it to the administrator. The admin adds the ID to `config.json` and restarts the process.

To find your own chat_id quickly: lock the bot with `[]`, send a message, copy the `chat_id` from the refusal, then add it.

## 7. Permission prompts

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

## 8. Directory layout

```
agent-bot/
├── src/
│   ├── __init__.py
│   ├── bot.py              # entry point: python -m src.bot
│   ├── agent.py            # AgentSessionManager (per-chat ClaudeSDKClient + idle GC)
│   ├── streaming.py        # DraftStreamer
│   ├── permissions.py      # TelegramPermissionGate
│   ├── reactions.py        # keyword → emoji reactions
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

## 9. Updating

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
