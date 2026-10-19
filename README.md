# librus-triage

Hourly check of the Librus (Synergia) messages inbox (`wiadomosci.librus.pl`).
Sends a macOS notification (and optionally an ntfy.sh push, email or Telegram
message) with the subject + sender of every new message. Built-in AI triage:
a local [basal](https://github.com/rkinas/basal) decision server classifies each
message (`action` / `important` / `info` / `auto`) with calibrated probabilities,
and deterministic sender rules can force or suppress notifications.

## Setup

1. `cp .env.example .env` and fill it in — at minimum `LIBRUS_LOGIN` and
   `LIBRUS_PASS` (your Synergia credentials).
2. Install the hourly job (launchd agent, the macOS-native cron):

   ```bash
   ./install.sh
   ```

   This creates `venv/`, installs `requests`, and loads
   `~/Library/LaunchAgents/com.librus.checker.plist` (`StartInterval=3600`).

3. Test it manually first:

   ```bash
   ./venv/bin/python3 librus_checker.py --list    # prints inbox, no notifications
   ```

   First check run only seeds the seen-IDs state file (no notification flood);
   subsequent runs notify only about genuinely new messages. Disable with
   `SEED_ON_FIRST_RUN=false` in `.env`.

## Files

| File | Purpose |
|---|---|
| `librus_checker.py` | login → fetch inbox → diff vs `.state/seen_ids.json` → notify |
| `.env.example` | config template — copy to `.env` (git-ignored) |
| `com.librus.checker.plist` | launchd template (`install.sh` fills `__PROJECT_DIR__`) |
| `install.sh` / `uninstall.sh` | setup / remove the hourly job |
| `.state/` `logs/` | seen message IDs, launchd stdout/stderr |

## Notifications

`NOTIFY_METHOD` is comma-separated — mix any of: `macos`, `ntfy`, `email`, `telegram`.

- `macos` — `osascript display notification` (built-in). First time, macOS may ask
  you to allow notifications from Script Editor.
- `ntfy` + `NTFY_TOPIC=some-secret-name` — push to your phone via ntfy.sh
  (subscribe to the same topic in the ntfy app).
- `email` — SMTP. For Gmail, set `SMTP_USER` to your address and generate an
  *app password* (myaccount.google.com/apppasswords, requires 2FA) for `SMTP_PASS`.
  `SMTP_PORT=465` uses SSL; `587` uses STARTTLS.
- `telegram` — create a bot with @BotFather for `TELEGRAM_BOT_TOKEN`, message it
  once, then get `TELEGRAM_CHAT_ID` from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`.

SMS is not included — that needs a paid gateway (Twilio etc.); Telegram or ntfy
cover the same use case for free.

## AI triage dry run

```bash
./venv/bin/python3 librus_checker.py --analyze 5           # triage last 5, full bodies — MARKS read
./venv/bin/python3 librus_checker.py --analyze 5 --preview # truncated previews only, nothing marked read
./venv/bin/python3 librus_checker.py --analyze 5 --anon    # anonymized output (screenshots)
```

Prints per message: `notify yes/no`, `action_required`, `priority`, `reason`, `action`.
Full bodies are the default — Librus marks a message read when its body is fetched
(same as opening it yourself); use `--preview` to avoid that.

### basal (default AI_PROVIDER=basal)

[basal](https://github.com/rkinas/basal) 2.2 — a Polish typed-decision engine.
`basal-serve` answers `choice`/`yes_no`/`score` questions about a "state" with a
calibrated probability per option in a single forward pass (no text generation).
The checker sends three questions per message: kind (`action`/`important`/`info`/`auto`),
urgency (score 0–2) and action type (bring / sign / pay / confirm / prepare / excuse /
other).

Install into its own venv (Apple Silicon build shown — see basal docs for CUDA):

```bash
python3 -m venv basal-env
./basal-env/bin/pip install "basal[mlx] @ https://github.com/rkinas/basal/archive/refs/tags/v2.2.0.tar.gz" mlx-lm
./basal-env/bin/basal-serve --model Remek/basal-2.0-mini-MLX-8bit --mode mlx --port 8000
# wait for: "basal: model ... ready on port 8000"
```

basal decides; it cannot summarize (no generation). For summaries use
`AI_PROVIDER=openai` (or `ollama` if you have it).

Triage currently applies to `--analyze`; the hourly check still notifies on every
new message (sender rules and filtering there are a TODO).

### Sender rules (deterministic, model can't override)

`.env`: `IMPORTANT_SENDERS` (always notify), `IGNORED_SENDERS` (never notify).

## AI summary (day 2)

Ready but off by default (`SUMMARIZE_ENABLED=false`).

- Local: install Ollama, `ollama pull llama3.1`, keep `AI_PROVIDER=ollama`.
- Remote: `AI_PROVIDER=openai` + `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
  (any OpenAI-compatible endpoint works).

Manual test: `./venv/bin/python3 librus_checker.py --summarize <messageId>`
(get the ID from `--list`).

**Note:** Librus marks a message as read when its full body is fetched — same as
opening it in the app. Listing/checking messages does not mark anything read.

## Cron alternative

If you prefer classic cron over launchd (`crontab -e`):

```cron
0 * * * * cd /path/to/librus-triage && ./venv/bin/python3 librus_checker.py >> logs/cron.log 2>&1
```

Cron on macOS needs Full Disk Access granted to `cron`/`cron`-parent in
System Settings → Privacy & Security; launchd avoids that hassle.
