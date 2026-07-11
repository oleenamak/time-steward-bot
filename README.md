# Time Steward — Phase 0/1

A minimal personal Telegram bot: the "pipe check" + wake/sleep state machine
milestones from the [Time Steward PRD](../time-steward-prd.md), built on
Telegram instead of iMessage to avoid the third-party iMessage-API
cost/privacy tradeoffs while the core loop gets validated.

What it does:
- Long-polls Telegram (no webhook, no public URL required).
- Ignores every chat except `MY_CHAT_ID`.
- Classifies every inbound message's intent with Claude Haiku (`checkin`,
  `goodnight`, `pause`, `correction`, `query`, `settings`, `other`):
  - `goodnight` → sets state to `ASLEEP`, suppressing pings until 6 AM.
  - `pause` → suppresses pings for a given duration ("pause 2 hours") or for
    the rest of the day ("skip today"), then resumes automatically.
  - everything else gets the Phase 0 echo reply, `got it: <text>` (full
    check-in parsing, corrections, queries, and settings are later phases).
- Tracks state (`AWAKE`/`ASLEEP`, `paused_until`) in a single-row SQLite
  `state` table, and logs every inbound/outbound message to `messages`.
- Sends a 6:00 AM good-morning ping that also asks "What's the one thing
  that would make today a win?" — the next reply is stored as that day's
  intention in the `intentions` table.
- Sends hourly check-in pings from 07:00–23:00, skipped whenever the state
  is `ASLEEP` or currently paused.
- At goodnight or the 11:30 PM hard stop, generates and sends the real daily
  summary (tracked/untracked hours, category bar chart, wins, one
  observation), closing with a **Steward's Verdict** — a tier (1-3) chosen
  entirely by rules over the day's computed facts (deep work pace, untracked
  %, an intention/win check, pause/sick/rest-day flags), with Claude only
  writing the prose for the tier it's given. The tier is stored with the
  summary for a future weekly rollup.
- Right after the summary, renders the day as markdown (intention, an
  hourly table, then the summary + verdict under `## Summary`) and pushes
  it to a private GitHub vault repo (`time-steward-vault`) over an SSH
  deploy key — see `vault.py`. `python main.py backfill-vault` does the
  same for every day that already has a stored summary, one-time.
- All scheduling in the `TZ` timezone (default `America/Chicago`).

**Known gap:** Railway's filesystem is ephemeral, so `time_steward.db`
(and everything in it — entries, summaries, intentions) is wiped on every
redeploy. The vault export is the only durable record right now. Fixing
this properly means a Railway volume or moving to Postgres — not done yet.

## Local setup

1. **Create a bot and get a token**
   Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   and copy the token it gives you.

2. **Get your chat ID**
   Message your new bot once (anything), then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   read `message.chat.id` from the JSON response. That's `MY_CHAT_ID`.

3. **Install dependencies**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   # then edit .env and fill in TELEGRAM_BOT_TOKEN, MY_CHAT_ID, ANTHROPIC_API_KEY
   ```

5. **(Optional) Set up the vault repo** — see [Vault setup](#vault-setup)
   below if you want nightly logs pushed to GitHub.

6. **Run it**
   ```bash
   python main.py
   ```
   Message your bot on Telegram — it should reply `got it: ...` and log the
   exchange to `time_steward.db` in this directory.

## Vault setup

The bot pushes each day's log to a private GitHub repo over an SSH deploy
key, scoped to that one repo only (not a personal access token with broader
access). One-time setup:

1. **Create a private repo** for the vault, e.g. `time-steward-vault`.
2. **Generate a deploy key** (no passphrase — it needs to run unattended):
   ```bash
   ssh-keygen -t ed25519 -f vault_deploy_key -N "" -C "time-steward-bot deploy key"
   ```
3. **Add the public key to the repo** with write access:
   ```bash
   gh repo deploy-key add vault_deploy_key.pub --title "time-steward-bot" \
     --allow-write --repo <you>/time-steward-vault
   ```
   (Or via the GitHub UI: repo → Settings → Deploy keys → Add deploy key →
   check "Allow write access".)
4. **Set env vars**, both locally (`.env`) and on Railway. The private key
   has real newlines — env vars need it on one line, so escape them as
   literal `\n` (the bot un-escapes this at runtime):
   ```bash
   VAULT_REPO_SSH_URL=git@github.com:<you>/time-steward-vault.git
   VAULT_DEPLOY_KEY=$(awk '{printf "%s\\n", $0}' vault_deploy_key)
   ```
5. **Delete the local key files** once both env vars are set — they're not
   needed on disk after that.

If `VAULT_REPO_SSH_URL` / `VAULT_DEPLOY_KEY` aren't set, the vault export is
skipped silently (everything else still works).

## Deploying to Railway

1. Push this directory to a GitHub repo (or use `railway init` from here
   directly with the Railway CLI).
2. In Railway, create a new project from the repo.
3. Railway will detect the `Procfile` and offer a `worker` process — deploy
   that process type. This bot uses long polling, so it does **not** need a
   `web` process or an exposed port; don't add one.
4. In the project's **Variables** tab, set `TELEGRAM_BOT_TOKEN`,
   `MY_CHAT_ID`, `ANTHROPIC_API_KEY`, `TZ=America/Chicago`, and (if using the
   vault) `VAULT_REPO_SSH_URL` / `VAULT_DEPLOY_KEY`.
5. Deploy. Check the service logs for `Time Steward starting`.

Note: Railway's filesystem is ephemeral on redeploy, so `time_steward.db`
resets whenever the service redeploys — see the known-gap note above.

## Backfilling the vault

To export every day that already has a stored daily summary (skips days
with no summary yet):

```bash
python main.py backfill-vault
```

This reads from whatever `time_steward.db` is in scope, so to backfill
Railway's live data, run it inside the deployed container rather than
locally:

```bash
railway ssh --service time-steward-bot
python main.py backfill-vault
```
