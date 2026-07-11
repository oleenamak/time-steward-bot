# Time Steward — Phase 0

A minimal personal Telegram bot: the "pipe check" milestone from the
[Time Steward PRD](../time-steward-prd.md), built on Telegram instead of
iMessage to avoid the third-party iMessage-API cost/privacy tradeoffs while
the core loop gets validated.

What it does:
- Long-polls Telegram (no webhook, no public URL required).
- Ignores every chat except `MY_CHAT_ID`.
- Echoes back `got it: <your message>` and logs every inbound/outbound
  message to a local SQLite `messages` table.
- Sends a 6:00 AM good-morning ping and hourly check-in pings from
  07:00–23:00, all in the `TZ` timezone (default `America/Chicago`).

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

5. **Run it**
   ```bash
   python main.py
   ```
   Message your bot on Telegram — it should reply `got it: ...` and log the
   exchange to `time_steward.db` in this directory.

## Deploying to Railway

1. Push this directory to a GitHub repo (or use `railway init` from here
   directly with the Railway CLI).
2. In Railway, create a new project from the repo.
3. Railway will detect the `Procfile` and offer a `worker` process — deploy
   that process type. This bot uses long polling, so it does **not** need a
   `web` process or an exposed port; don't add one.
4. In the project's **Variables** tab, set `TELEGRAM_BOT_TOKEN`,
   `MY_CHAT_ID`, `ANTHROPIC_API_KEY`, and `TZ=America/Chicago`.
5. Deploy. Check the service logs for `Time Steward starting (Phase 0)`.

Note: Railway's filesystem is ephemeral on redeploy, so `time_steward.db`
resets whenever the service redeploys. That's fine for Phase 0; a persistent
volume (or a move to Postgres) is a Phase 1+ concern per the PRD.
