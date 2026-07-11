"""Time Steward — Phase 0/1: pipe check + wake/sleep state machine.

A personal Telegram bot that:
- Ignores everyone except the owner (MY_CHAT_ID).
- Classifies every inbound message's intent with Claude Haiku (checkin,
  goodnight, pause, correction, query, settings, other) — checkin messages
  are also categorized against the `categories` table (Deep Work, Shallow
  Work, Training, Faith, People, Life & Rest, Untracked); everything else
  gets the Phase 0 echo reply.
- Tracks AWAKE/ASLEEP state in SQLite; hourly pings are skipped while ASLEEP
  or paused.
- Sends a 6:00 AM good-morning ping (which also asks for today's one-thing
  intention) and hourly check-in pings (07:00-23:00) while AWAKE.
- Auto-sleeps at 11:30 PM if still AWAKE (FR-5 hard stop).

Runs via long polling — no webhook, no public URL needed.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_CHAT_ID = int(os.environ["MY_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))

DB_PATH = os.environ.get("DB_PATH", "time_steward.db")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
INTENTS = {"checkin", "goodnight", "pause", "correction", "query", "settings", "other"}

# Seeded fresh into the `categories` table on every startup — see FR-11 in the PRD.
# (name, weekly_target_hours or None, notes used to steer the categorization prompt)
DEFAULT_CATEGORIES = [
    ("Deep Work", 20, "Writing, strategy, prospecting, outreach, not Shallow Work."),
    ("Shallow Work", 10, "Meetings, calls, email, Slack, admin, scheduling, logistics."),
    ("Training", 7, "Gym, runs, biking, not social workouts"),
    ("Faith", 5, "Bible study, church, prayer, faith-related reading or writing, daily devotions"),
    (
        "People",
        8,
        "Friends, community events, run clubs, meals WITH others, calls with "
        "friends/family, social workouts",
    ),
    ("Life & Rest", None, "Errands, solo meals, chores, commuting, leisure, downtime."),
    ("Untracked", None, "System category for unlogged hours only — never assign from a reply."),
]

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logger = logging.getLogger("time_steward")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL CHECK (state IN ('AWAKE', 'ASLEEP')),
                updated_at TEXT NOT NULL,
                paused_until TEXT,
                awaiting_intention INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO state (id, state, updated_at) VALUES (1, 'ASLEEP', ?)",
            (datetime.now(TZ).isoformat(),),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intentions (
                date TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                weekly_target_hours REAL,
                notes TEXT NOT NULL
            )
            """
        )
        # Categories aren't yet editable via chat (FR-11), so the seed list in code
        # is authoritative — wipe and reseed on every startup.
        conn.execute("DELETE FROM categories")
        conn.executemany(
            "INSERT INTO categories (name, weekly_target_hours, notes) VALUES (?, ?, ?)",
            DEFAULT_CATEGORIES,
        )


def log_message(direction: str, body: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (direction, body, sent_at) VALUES (?, ?, ?)",
            (direction, body, datetime.now(TZ).isoformat()),
        )


def get_state() -> sqlite3.Row:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM state WHERE id = 1").fetchone()


def set_state(new_state: str, clear_pause: bool = False) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if clear_pause:
            conn.execute(
                "UPDATE state SET state = ?, updated_at = ?, paused_until = NULL WHERE id = 1",
                (new_state, datetime.now(TZ).isoformat()),
            )
        else:
            conn.execute(
                "UPDATE state SET state = ?, updated_at = ? WHERE id = 1",
                (new_state, datetime.now(TZ).isoformat()),
            )


def set_paused_until(until: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE state SET paused_until = ?, updated_at = ? WHERE id = 1",
            (until.isoformat(), datetime.now(TZ).isoformat()),
        )


def set_awaiting_intention(flag: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE state SET awaiting_intention = ? WHERE id = 1", (int(flag),))


def store_intention(text: str) -> None:
    today = datetime.now(TZ).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO intentions (date, text, created_at) VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET text = excluded.text, created_at = excluded.created_at
            """,
            (today, text, datetime.now(TZ).isoformat()),
        )


def next_six_am() -> datetime:
    now = datetime.now(TZ)
    candidate = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def is_paused(row: sqlite3.Row) -> bool:
    if not row["paused_until"]:
        return False
    return datetime.fromisoformat(row["paused_until"]) > datetime.now(TZ)


def get_categories() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY id")]


_classify_system_prompt_cache: str | None = None


def get_classify_system_prompt() -> str:
    """Built from the categories table so notes drive the categorization rules
    without needing a code change (cached — categories are reseeded only at
    startup, not editable mid-run yet)."""
    global _classify_system_prompt_cache
    if _classify_system_prompt_cache is not None:
        return _classify_system_prompt_cache

    def describe(c: dict) -> str:
        target = f'{c["weekly_target_hours"]:g}h/week target' if c["weekly_target_hours"] else "no weekly target"
        return f'- {c["name"]} ({target}): {c["notes"]}'

    assignable = [c for c in get_categories() if c["name"] != "Untracked"]
    category_guide = "\n".join(describe(c) for c in assignable)

    _classify_system_prompt_cache = (
        "You classify inbound texts to a personal time-tracking Telegram bot. "
        "Respond with JSON only, no prose, matching exactly this schema: "
        '{"intent": "checkin|goodnight|pause|correction|query|settings|other", '
        '"pause_hours": <number or null>, "category": "<category name>|null"}. '
        'Use "goodnight" for sleep signals like "good night", "gn", "going to bed", '
        '"done for the day". Use "pause" when the user wants pings suppressed '
        '(e.g. "pause 2 hours", "skip today", "traveling") — set pause_hours to the '
        "requested duration in hours if given, otherwise null. Use \"checkin\" for "
        "activity logs — when intent is \"checkin\", also set category to the single "
        f"best-fit category from this list:\n{category_guide}\n"
        'Use "correction" for fixing a previous entry, "query" for questions about '
        'logged time, "settings" for category/target changes, and "other" for '
        'anything else. For any intent other than "checkin", set category to null. '
        'Never use "Untracked" as a category — it is a system category applied only '
        "to unlogged hours, never assigned from a reply."
    )
    return _classify_system_prompt_cache


async def classify_intent(text: str) -> tuple[str, float | None, str | None]:
    valid_categories = {c["name"] for c in get_categories() if c["name"] != "Untracked"}
    for attempt in range(2):
        try:
            response = await anthropic_client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=150,
                system=get_classify_system_prompt(),
                messages=[{"role": "user", "content": text}],
            )
            raw = response.content[0].text
            data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            intent = data.get("intent")
            if intent not in INTENTS:
                raise ValueError(f"unknown intent: {intent!r}")
            pause_hours = data.get("pause_hours")
            category = data.get("category")
            if category not in valid_categories:
                category = None
            return intent, float(pause_hours) if pause_hours else None, category
        except Exception as exc:
            logger.warning("intent classification failed (attempt %d): %s", attempt + 1, exc)
    return "other", None, None


async def send_and_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await context.bot.send_message(chat_id=MY_CHAT_ID, text=text)
    log_message("outbound", text)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != MY_CHAT_ID:
        return

    text = update.message.text
    log_message("inbound", text)

    row = get_state()
    intent, pause_hours, category = await classify_intent(text)

    if row["awaiting_intention"]:
        set_awaiting_intention(False)
        if intent in ("checkin", "other"):
            store_intention(text)
            await send_and_log(context, f'Love it — "{text}" it is. Go make it happen.')
            return

    if intent == "goodnight":
        set_state("ASLEEP")
        await send_and_log(context, "Goodnight! Pings are paused until 6 AM.")
    elif intent == "pause":
        if pause_hours:
            until = datetime.now(TZ) + timedelta(hours=pause_hours)
            set_paused_until(until)
            await send_and_log(
                context, f"Pausing pings for {pause_hours:g}h — back around {until:%-I:%M %p}."
            )
        else:
            until = next_six_am()
            set_paused_until(until)
            await send_and_log(context, "Skipping pings for the rest of today. See you at 6 AM.")
    elif intent == "checkin" and category:
        await send_and_log(context, f"got it: {text}\nLogged under {category}.")
    else:
        await send_and_log(context, f"got it: {text}")


async def good_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    set_state("AWAKE", clear_pause=True)
    set_awaiting_intention(True)
    await send_and_log(
        context, "Good morning ☀️\nWhat's the one thing that would make today a win?"
    )


async def hourly_check_in(context: ContextTypes.DEFAULT_TYPE) -> None:
    row = get_state()
    if row["state"] != "AWAKE" or is_paused(row):
        return
    await send_and_log(context, context.job.data)


async def hard_stop(context: ContextTypes.DEFAULT_TYPE) -> None:
    row = get_state()
    if row["state"] == "AWAKE":
        await send_and_log(context, "Logging off — here's your day. (Full summary coming soon.)")
        set_state("ASLEEP")


def schedule_jobs(app: Application) -> None:
    job_queue = app.job_queue

    job_queue.run_daily(
        good_morning,
        time=time(hour=6, minute=0, tzinfo=TZ),
        name="good_morning",
    )

    for hour in range(7, 24):  # 07:00 through 23:00 inclusive
        job_queue.run_daily(
            hourly_check_in,
            time=time(hour=hour, minute=0, tzinfo=TZ),
            data="What did you get done this past hour?",
            name=f"hourly_check_in_{hour:02d}",
        )

    job_queue.run_daily(
        hard_stop,
        time=time(hour=23, minute=30, tzinfo=TZ),
        name="hard_stop",
    )


def main() -> None:
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    schedule_jobs(app)

    logger.info("Time Steward starting — polling for updates")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
