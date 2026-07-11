"""Time Steward — Phase 0: pipe check.

A personal Telegram bot that:
- Ignores everyone except the owner (MY_CHAT_ID).
- Echoes back any text it receives, logging both directions to SQLite.
- Sends a 6:00 AM good-morning ping and hourly check-in pings (07:00-23:00),
  all in the America/Chicago timezone (or whatever TZ is set to).

Runs via long polling — no webhook, no public URL needed.
"""

import logging
import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_CHAT_ID = int(os.environ["MY_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # reserved for later phases
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))

DB_PATH = os.environ.get("DB_PATH", "time_steward.db")

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logger = logging.getLogger("time_steward")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
            """
        )


def log_message(direction: str, body: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (direction, body, sent_at) VALUES (?, ?, ?)",
            (direction, body, datetime.now(TZ).isoformat()),
        )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != MY_CHAT_ID:
        return

    text = update.message.text
    log_message("inbound", text)

    reply = f"got it: {text}"
    await context.bot.send_message(chat_id=MY_CHAT_ID, text=reply)
    log_message("outbound", reply)


async def send_scheduled(context: ContextTypes.DEFAULT_TYPE) -> None:
    text = context.job.data
    await context.bot.send_message(chat_id=MY_CHAT_ID, text=text)
    log_message("outbound", text)


def schedule_jobs(app: Application) -> None:
    job_queue = app.job_queue

    job_queue.run_daily(
        send_scheduled,
        time=time(hour=6, minute=0, tzinfo=TZ),
        data="Good morning ☀️",
        name="good_morning",
    )

    for hour in range(7, 24):  # 07:00 through 23:00 inclusive
        job_queue.run_daily(
            send_scheduled,
            time=time(hour=hour, minute=0, tzinfo=TZ),
            data="What did you get done this past hour?",
            name=f"hourly_check_in_{hour:02d}",
        )


def main() -> None:
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    schedule_jobs(app)

    logger.info("Time Steward starting (Phase 0) — polling for updates")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
