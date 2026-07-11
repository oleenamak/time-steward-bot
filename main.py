"""Time Steward — Phase 0/1: pipe check + wake/sleep state machine.

A personal Telegram bot that:
- Ignores everyone except the owner (MY_CHAT_ID).
- Classifies every inbound message's intent with Claude Haiku (checkin,
  goodnight, pause, correction, query, settings, other). Checkin messages
  are split into one or more entries, each categorized against the
  `categories` table with a confidence level. High-confidence entries log
  immediately; low-confidence ones are asked about one at a time via an
  inline-keyboard category picker, and confirmed answers append a learning
  note back onto that category (capped at ~10 lines, oldest dropped).
  Anything still unanswered at goodnight/hard-stop is saved with the
  parser's best guess. Everything else gets the Phase 0 echo reply.
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                activity TEXT NOT NULL,
                category TEXT,
                guessed_category TEXT NOT NULL,
                confidence TEXT NOT NULL CHECK (confidence IN ('high', 'low')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'guessed')),
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                asked_at TEXT,
                message_id INTEGER
            )
            """
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


def append_category_note(category_name: str, note: str, max_lines: int = 10) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT notes FROM categories WHERE name = ?", (category_name,)
        ).fetchone()
        if row is None:
            return
        lines = [line for line in row["notes"].split("\n") if line.strip()]
        lines.append(note)
        lines = lines[-max_lines:]
        conn.execute(
            "UPDATE categories SET notes = ? WHERE name = ?",
            ("\n".join(lines), category_name),
        )
    invalidate_classify_prompt_cache()


def create_entry(activity: str, guessed_category: str, confidence: str, status: str) -> int:
    today = datetime.now(TZ).date().isoformat()
    now = datetime.now(TZ).isoformat()
    category = guessed_category if status == "confirmed" else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO entries
                (date, activity, category, guessed_category, confidence, status, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                activity,
                category,
                guessed_category,
                confidence,
                status,
                now,
                now if status == "confirmed" else None,
            ),
        )
        return cur.lastrowid


def get_entry(entry_id: int) -> sqlite3.Row | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def get_next_unasked_entry() -> sqlite3.Row | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM entries WHERE status = 'pending' AND asked_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()


def has_open_question() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE status = 'pending' AND asked_at IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None


def mark_asked(entry_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE entries SET asked_at = ?, message_id = ? WHERE id = ?",
            (datetime.now(TZ).isoformat(), message_id, entry_id),
        )


def resolve_entry(entry_id: int, category: str, status: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE entries SET category = ?, status = ?, resolved_at = ? WHERE id = ?",
            (category, status, datetime.now(TZ).isoformat(), entry_id),
        )


def get_stale_pending_entries() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM entries WHERE status = 'pending'")]


_classify_system_prompt_cache: str | None = None


def invalidate_classify_prompt_cache() -> None:
    global _classify_system_prompt_cache
    _classify_system_prompt_cache = None


def get_classify_system_prompt() -> str:
    """Built from the categories table so notes drive the categorization rules
    without needing a code change (cached — invalidated whenever a category's
    notes are updated by the learning-note feature)."""
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
        '"pause_hours": <number or null>, '
        '"entries": [{"activity": "<short activity phrase>", '
        '"category": "<category name>", "confidence": "high|low"}] or null}. '
        'Use "goodnight" for sleep signals like "good night", "gn", "going to bed", '
        '"done for the day". Use "pause" when the user wants pings suppressed '
        '(e.g. "pause 2 hours", "skip today", "traveling") — set pause_hours to the '
        "requested duration in hours if given, otherwise null. Use \"checkin\" for "
        "activity logs: set entries to a list with one item per distinct activity "
        'described (e.g. "gym then emails" is two entries); for each entry always '
        "include your single best-guess category from this list, even if unsure:\n"
        f"{category_guide}\n"
        'Set each entry\'s confidence to "low" only when the activity genuinely fits '
        'multiple categories about equally well, or fits none of them well — not for '
        'routine best-guesses; default to "high" whenever the fit is reasonably clear. '
        'Use "correction" for fixing a previous entry, "query" for questions about '
        'logged time, "settings" for category/target changes, and "other" for '
        'anything else. For any intent other than "checkin", set entries to null. '
        'Never use "Untracked" as a category — it is a system category applied only '
        "to unlogged hours, never assigned from a reply."
    )
    return _classify_system_prompt_cache


async def classify_intent(text: str) -> tuple[str, float | None, list[dict] | None]:
    valid_categories = {c["name"] for c in get_categories() if c["name"] != "Untracked"}
    for attempt in range(2):
        try:
            response = await anthropic_client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=400,
                system=get_classify_system_prompt(),
                messages=[{"role": "user", "content": text}],
            )
            raw = response.content[0].text
            data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            intent = data.get("intent")
            if intent not in INTENTS:
                raise ValueError(f"unknown intent: {intent!r}")
            pause_hours = data.get("pause_hours")

            entries = None
            if intent == "checkin":
                entries = []
                for raw_entry in data.get("entries") or []:
                    activity = (raw_entry.get("activity") or "").strip()
                    category = raw_entry.get("category")
                    confidence = raw_entry.get("confidence")
                    if not activity or category not in valid_categories or confidence not in (
                        "high",
                        "low",
                    ):
                        continue
                    entries.append(
                        {"activity": activity, "category": category, "confidence": confidence}
                    )
                if not entries:
                    raise ValueError("checkin intent but no usable entries returned")

            return intent, float(pause_hours) if pause_hours else None, entries
        except Exception as exc:
            logger.warning("intent classification failed (attempt %d): %s", attempt + 1, exc)
    return "other", None, None


async def send_and_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await context.bot.send_message(chat_id=MY_CHAT_ID, text=text)
    log_message("outbound", text)


def build_category_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    assignable = [c for c in get_categories() if c["name"] != "Untracked"]
    buttons = [
        InlineKeyboardButton(c["name"], callback_data=f"cat:{entry_id}:{c['id']}")
        for c in assignable
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def advance_pending_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask about the next not-yet-asked pending entry, if any (FIFO, one at a time)."""
    entry = get_next_unasked_entry()
    if entry is None:
        return
    text = f"Where should '{entry['activity']}' go?"
    msg = await context.bot.send_message(
        chat_id=MY_CHAT_ID, text=text, reply_markup=build_category_keyboard(entry["id"])
    )
    mark_asked(entry["id"], msg.message_id)
    log_message("outbound", text)


async def resolve_all_pending_with_guess(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Day-end fallback (FR-5-adjacent): anything still unanswered gets the
    parser's best guess, and any outstanding clarification message is edited
    to reflect that so the buttons stop looking actionable."""
    pending = get_stale_pending_entries()
    for entry in pending:
        resolve_entry(entry["id"], entry["guessed_category"], status="guessed")
        if entry["message_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=MY_CHAT_ID,
                    message_id=entry["message_id"],
                    text=f'Logged: {entry["activity"]} → {entry["guessed_category"]} '
                    "(*guessed category)",
                )
            except Exception as exc:
                logger.warning("failed to edit stale clarification message: %s", exc)
    return len(pending)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != MY_CHAT_ID:
        return

    text = update.message.text
    log_message("inbound", text)

    row = get_state()
    intent, pause_hours, entries = await classify_intent(text)

    if row["awaiting_intention"]:
        set_awaiting_intention(False)
        if intent in ("checkin", "other"):
            store_intention(text)
            await send_and_log(context, f'Love it — "{text}" it is. Go make it happen.')
            return

    if intent == "goodnight":
        guessed_count = await resolve_all_pending_with_guess(context)
        set_state("ASLEEP")
        msg = "Goodnight! Pings are paused until 6 AM."
        if guessed_count:
            plural = "entry" if guessed_count == 1 else "entries"
            msg += f"\n({guessed_count} {plural} auto-guessed since you didn't confirm in time.)"
        await send_and_log(context, msg)
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
    elif intent == "checkin" and entries:
        confirmed_lines = []
        any_new_pending = False
        for entry in entries:
            if entry["confidence"] == "high":
                create_entry(entry["activity"], entry["category"], "high", status="confirmed")
                confirmed_lines.append(f'{entry["activity"]} → {entry["category"]} ✓')
            else:
                create_entry(entry["activity"], entry["category"], "low", status="pending")
                any_new_pending = True

        if confirmed_lines:
            if len(confirmed_lines) == 1:
                await send_and_log(context, f"Logged: {confirmed_lines[0]}")
            else:
                await send_and_log(
                    context, "Logged:\n" + "\n".join(f"- {line}" for line in confirmed_lines)
                )

        if any_new_pending and not has_open_question():
            await advance_pending_queue(context)
    else:
        await send_and_log(context, f"got it: {text}")


async def handle_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_chat is None or update.effective_chat.id != MY_CHAT_ID:
        return
    if not query.data or not query.data.startswith("cat:"):
        return

    try:
        _, entry_id_str, category_id_str = query.data.split(":")
        entry_id, category_id = int(entry_id_str), int(category_id_str)
    except ValueError:
        return

    entry = get_entry(entry_id)
    if entry is None or entry["status"] != "pending":
        return  # already resolved (e.g. by the day-end fallback) — stale button, ignore

    category = next((c for c in get_categories() if c["id"] == category_id), None)
    if category is None:
        return

    resolve_entry(entry_id, category["name"], status="confirmed")
    append_category_note(category["name"], f'{entry["activity"]} → {category["name"]}')

    confirmation = f'Logged: {entry["activity"]} → {category["name"]} ✓'
    await query.edit_message_text(confirmation)
    log_message("outbound", confirmation)

    await advance_pending_queue(context)


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
        guessed_count = await resolve_all_pending_with_guess(context)
        msg = "Logging off — here's your day. (Full summary coming soon.)"
        if guessed_count:
            plural = "entry" if guessed_count == 1 else "entries"
            msg += f"\n({guessed_count} {plural} auto-guessed since you didn't confirm in time.)"
        await send_and_log(context, msg)
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
    app.add_handler(CallbackQueryHandler(handle_category_choice))
    schedule_jobs(app)

    logger.info("Time Steward starting — polling for updates")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
