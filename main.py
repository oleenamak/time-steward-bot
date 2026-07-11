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
- At goodnight or the 11:30 PM hard stop (FR-5), generates and sends a daily
  summary (tracked/untracked hours, a category bar chart, wins, one
  observation) closing with a "Steward's Verdict" — a tier (1-3) picked by
  pure rules over the day's computed facts (never by Claude) and inspired by
  the parable of the talents, with Claude only writing the prose for the
  tier it's given. The tier is stored with the summary for future weekly
  rollups.
- Right after the daily summary, renders the day (hourly table + summary +
  verdict) as markdown and pushes it to a private GitHub vault repo over a
  deploy key (see vault.py). `python main.py backfill-vault` does the same
  for every day that already has a stored summary, one-time.

Runs via long polling — no webhook, no public URL needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
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

import vault

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_CHAT_ID = int(os.environ["MY_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))

DB_PATH = os.environ.get("DB_PATH", "time_steward.db")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-5"
INTENTS = {"checkin", "goodnight", "pause", "correction", "query", "settings", "other"}
DAY_SIGNALS = {"sick", "rest_day"}

# The tier is decided entirely by decide_tier() below (pure rules over the day's
# computed facts) — Claude is only ever asked to write prose for a tier it is given.
VERDICT_TIER_LABELS = {
    1: "Well done, good and faithful servant",
    2: "Faithful in part",
    3: "The wicked and lazy servant",
}

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


def extract_response_text(response) -> str:
    """Some models (e.g. extended-thinking Sonnet) put a ThinkingBlock before the
    TextBlock, so content[0] isn't reliably the text — find it explicitly."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("no text block in response")


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
                duration_min INTEGER NOT NULL,
                is_accomplishment INTEGER NOT NULL DEFAULT 0,
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS day_meta (
                date TEXT PRIMARY KEY,
                paused_used INTEGER NOT NULL DEFAULT 0,
                sick INTEGER NOT NULL DEFAULT 0,
                rest_day INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                tier INTEGER NOT NULL CHECK (tier IN (1, 2, 3)),
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


def get_intention(date: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT text FROM intentions WHERE date = ?", (date,)).fetchone()
        return row[0] if row else None


def mark_day_flag(flag: str, date: str | None = None) -> None:
    assert flag in ("paused_used", "sick", "rest_day")
    day = date or datetime.now(TZ).date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"""
            INSERT INTO day_meta (date, {flag}) VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET {flag} = 1
            """,
            (day,),
        )


def get_day_meta(date: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM day_meta WHERE date = ?", (date,)).fetchone()
        if row is None:
            return {"date": date, "paused_used": 0, "sick": 0, "rest_day": 0}
        return dict(row)


def compute_waking_hours(end_time: datetime) -> float:
    start = end_time.replace(hour=6, minute=0, second=0, microsecond=0)
    if end_time <= start:
        return 0.0
    return (end_time - start).total_seconds() / 3600


def compute_category_hours(entries: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for e in entries:
        cat = e["category"] or e["guessed_category"]
        totals[cat] = totals.get(cat, 0.0) + (e["duration_min"] or 0) / 60
    return totals


def get_category_weekly_target(name: str) -> float | None:
    for c in get_categories():
        if c["name"] == name:
            return c["weekly_target_hours"]
    return None


def format_hours(h: float) -> str:
    return f"{h:.1f}h"


def format_minutes(m: int) -> str:
    if m < 60:
        return f"{m}m"
    hrs, mins = divmod(m, 60)
    return f"{hrs}h{mins}m" if mins else f"{hrs}h"


def build_bar_chart(category_hours: dict[str, float]) -> str:
    if not category_hours:
        return "(nothing logged)"
    max_hours = max(category_hours.values()) or 1
    lines = []
    for name, hours in category_hours.items():
        bar_len = round((hours / max_hours) * 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(f"{name:<13}{bar} {format_hours(hours)}")
    return "\n".join(lines)


def decide_tier(facts: dict) -> int:
    """Pure rules, no LLM — this is the ONLY thing that selects the tier."""
    if facts["rest_day"]:
        return 1  # Sabbath clause: a well-kept rest day is faithful stewardship.

    intention_ok = facts["intention_or_win_met"]
    deep_ok = facts["deep_work_hours"] >= facts["deep_work_daily_target"]
    untracked_low = facts["untracked_pct"] < 0.20

    if intention_ok and deep_ok and untracked_low:
        return 1

    excused = facts["paused_used"] or facts["sick"]
    deep_near_zero = facts["deep_work_hours"] < 0.25
    if (
        not excused
        and facts["untracked_pct"] > 0.50
        and not intention_ok
        and deep_near_zero
    ):
        return 3

    return 2


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


def create_entry(
    activity: str,
    guessed_category: str,
    duration_min: int,
    confidence: str,
    status: str,
    is_accomplishment: bool = False,
) -> int:
    today = datetime.now(TZ).date().isoformat()
    now = datetime.now(TZ).isoformat()
    category = guessed_category if status == "confirmed" else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO entries
                (date, activity, category, guessed_category, duration_min, is_accomplishment,
                 confidence, status, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                activity,
                category,
                guessed_category,
                duration_min,
                int(is_accomplishment),
                confidence,
                status,
                now,
                now if status == "confirmed" else None,
            ),
        )
        return cur.lastrowid


def get_entries_for_date(date: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM entries WHERE date = ? AND status != 'pending' ORDER BY id",
                (date,),
            )
        ]


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
        '"day_signal": "sick"|"rest_day"|null, '
        '"entries": [{"activity": "<short activity phrase>", '
        '"category": "<category name>", "confidence": "high|low", '
        '"duration_min": <integer minutes>, "is_accomplishment": true|false}] '
        "or null}. "
        'Use "goodnight" for sleep signals like "good night", "gn", "going to bed", '
        '"done for the day". Use "pause" when the user wants pings suppressed '
        '(e.g. "pause 2 hours", "skip today", "traveling") — set pause_hours to the '
        "requested duration in hours if given, otherwise null. Set day_signal "
        '"sick" only when the message explicitly says the person is unwell/sick '
        'today, or "rest_day" only when it explicitly says today is an intentional '
        "rest/recovery day — otherwise null; this is independent of intent and can "
        "be set alongside any intent. Use \"checkin\" for activity logs: set entries "
        "to a list with one item per distinct activity described (e.g. \"gym then "
        'emails" is two entries); for each entry always include your single '
        "best-guess category from this list, even if unsure:\n"
        f"{category_guide}\n"
        "Also estimate each entry's duration_min from the text — use an explicit "
        "duration if stated (\"2 hours\" → 120, \"30 min\" → 30), otherwise a "
        'reasonable estimate for that kind of activity. Set is_accomplishment to '
        "true only for a genuinely notable win worth highlighting later (shipped "
        "something, hit a real milestone) — most entries are routine and should be "
        'false. Set each entry\'s confidence to "low" only when the activity '
        'genuinely fits multiple categories about equally well, or fits none of '
        'them well — not for routine best-guesses; default to "high" whenever the '
        'fit is reasonably clear. Use "correction" for fixing a previous entry, '
        '"query" for questions about logged time, "settings" for category/target '
        'changes, and "other" for anything else. For any intent other than '
        '"checkin", set entries to null. Never use "Untracked" as a category — it '
        "is a system category applied only to unlogged hours, never assigned from "
        "a reply."
    )
    return _classify_system_prompt_cache


async def classify_intent(
    text: str,
) -> tuple[str, float | None, list[dict] | None, str | None]:
    valid_categories = {c["name"] for c in get_categories() if c["name"] != "Untracked"}
    for attempt in range(2):
        try:
            response = await anthropic_client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=500,
                system=get_classify_system_prompt(),
                messages=[{"role": "user", "content": text}],
            )
            raw = extract_response_text(response)
            data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            intent = data.get("intent")
            if intent not in INTENTS:
                raise ValueError(f"unknown intent: {intent!r}")
            pause_hours = data.get("pause_hours")
            day_signal = data.get("day_signal")
            if day_signal not in DAY_SIGNALS:
                day_signal = None

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
                    try:
                        duration_min = max(1, int(raw_entry.get("duration_min")))
                    except (TypeError, ValueError):
                        duration_min = 30
                    entries.append(
                        {
                            "activity": activity,
                            "category": category,
                            "confidence": confidence,
                            "duration_min": duration_min,
                            "is_accomplishment": bool(raw_entry.get("is_accomplishment")),
                        }
                    )
                if not entries:
                    raise ValueError("checkin intent but no usable entries returned")

            return intent, float(pause_hours) if pause_hours else None, entries, day_signal
        except Exception as exc:
            logger.warning("intent classification failed (attempt %d): %s", attempt + 1, exc)
    return "other", None, None, None


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
    duration = format_minutes(entry["duration_min"])
    text = f"Where should '{entry['activity']}' ({duration}) go?"
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


async def assess_intention_or_win(intention_text: str | None, entries: list[dict]) -> bool:
    """One boolean FACT fed into decide_tier() — this judges accomplishment, not tier."""
    if not entries:
        return False
    activity_lines = "\n".join(
        f'- {e["activity"]} ({e["category"] or e["guessed_category"]}, '
        f'{format_minutes(e["duration_min"])})'
        for e in entries
    )
    prompt = (
        f'Today\'s stated intention: {intention_text or "(none set)"}\n'
        f"Today's logged activities:\n{activity_lines}\n\n"
        "Did the day either (a) accomplish or make clear major progress on that "
        "intention, or (b) contain some other clear major win, even without a stated "
        'intention? Respond with JSON only: {"accomplished": true|false}.'
    )
    try:
        response = await anthropic_client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=50,
            system=(
                "You make a single yes/no judgment call about a day's productivity. "
                "Respond with JSON only, no prose."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = extract_response_text(response)
        data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        return bool(data.get("accomplished"))
    except Exception as exc:
        logger.warning("intention assessment failed: %s", exc)
        return False


async def compose_summary_content(entries: list[dict], facts: dict, tier: int) -> dict:
    """The tier (arg, already decided) is fixed; Claude only writes prose for it."""
    label = VERDICT_TIER_LABELS[tier]
    activity_lines = (
        "\n".join(
            f'- {e["activity"]} ({e["category"] or e["guessed_category"]}, '
            f'{format_minutes(e["duration_min"])})'
            for e in entries
        )
        or "(nothing logged)"
    )

    tier_instruction = {
        1: (
            "Verdict tone: warm, affirming praise for a day that hit the mark, 2-3 "
            "sentences. If facts.rest_day is true, praise honoring rest as faithful "
            "stewardship — do not mention deep work pace or hustle at all."
        ),
        2: (
            "Verdict tone: name the ONE real gap plainly (pick the single most "
            "relevant one from the facts — intention missed, deep work under pace, "
            "or untracked time — do not list more than one), without shaming, then "
            "state tomorrow's fix in a single sentence. 2-3 sentences total. If "
            "facts.paused_used or facts.sick is true, skip gap-naming and just give "
            "gentle encouragement forward instead."
        ),
        3: (
            "Verdict tone: exactly 2 sentences — one sentence of plain truth about "
            "today, one sentence of grace pointed at tomorrow. Direct but never "
            "cruel, like a coach who believes in the person."
        ),
    }[tier]

    prompt = (
        f"Today's logged activities:\n{activity_lines}\n\n"
        "Today's computed facts (already final — do not recompute, question, or "
        f"second-guess any of them):\n{json.dumps(facts, indent=2, default=str)}\n\n"
        f'The tier has ALREADY been decided by rules: tier {tier} ("{label}"). Do '
        "not choose, mention, or imply a different tier.\n\n"
        'Respond with JSON only: {"accomplishments": ["...", up to 3 short phrases, '
        'most significant first, empty list if nothing notable], "observation": '
        '"one short plain sentence noting a pattern or tradeoff in the day (empty '
        'string if nothing notable)", "verdict": "the verdict body text only — do '
        'NOT include the tier label itself, just the sentences that follow it"}.\n\n'
        f"{tier_instruction}"
    )

    try:
        response = await anthropic_client.messages.create(
            model=SONNET_MODEL,
            max_tokens=400,
            system=(
                "You write the daily summary for a personal time-tracking Telegram "
                "bot, including a closing 'Steward's Verdict' inspired by the parable "
                "of the talents (Matthew 25:14-30). Respond with JSON only, no prose "
                "outside it."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = extract_response_text(response)
        data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        return {
            "accomplishments": data.get("accomplishments") or [],
            "observation": data.get("observation") or "",
            "verdict": data.get("verdict") or "",
        }
    except Exception as exc:
        logger.warning("summary composition failed: %s", exc)
        return {"accomplishments": [], "observation": "", "verdict": ""}


async def generate_and_send_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TZ)
    date = now.date().isoformat()

    await resolve_all_pending_with_guess(context)

    entries = get_entries_for_date(date)
    category_hours = compute_category_hours(entries)
    tracked_hours = sum(category_hours.values())
    waking_hours = compute_waking_hours(now)
    untracked_hours = max(0.0, waking_hours - tracked_hours)
    untracked_pct = (untracked_hours / waking_hours) if waking_hours > 0 else 0.0

    deep_work_hours = category_hours.get("Deep Work", 0.0)
    deep_work_weekly_target = get_category_weekly_target("Deep Work") or 0.0
    deep_work_daily_target = deep_work_weekly_target / 7 if deep_work_weekly_target else 0.0

    intention_text = get_intention(date)
    day_meta = get_day_meta(date)
    intention_or_win_met = await assess_intention_or_win(intention_text, entries)

    facts = {
        "date": date,
        "tracked_hours": round(tracked_hours, 2),
        "untracked_hours": round(untracked_hours, 2),
        "untracked_pct": round(untracked_pct, 3),
        "waking_hours": round(waking_hours, 2),
        "deep_work_hours": round(deep_work_hours, 2),
        "deep_work_daily_target": round(deep_work_daily_target, 2),
        "category_hours": {k: round(v, 2) for k, v in category_hours.items()},
        "intention_text": intention_text,
        "intention_or_win_met": intention_or_win_met,
        "paused_used": bool(day_meta["paused_used"]),
        "sick": bool(day_meta["sick"]),
        "rest_day": bool(day_meta["rest_day"]),
    }

    tier = decide_tier(facts)
    content = await compose_summary_content(entries, facts, tier)

    lines = [f"📊 Daily summary — {date}", ""]
    lines.append(
        f"Tracked: {format_hours(tracked_hours)} | Untracked: "
        f"{format_hours(untracked_hours)} ({untracked_pct:.0%} of "
        f"{format_hours(waking_hours)} awake)"
    )
    lines.append("")
    lines.append(build_bar_chart(category_hours))

    if content["accomplishments"]:
        lines.append("")
        lines.append("Wins:")
        lines.extend(f"- {item}" for item in content["accomplishments"][:3])

    if content["observation"]:
        lines.append("")
        lines.append(content["observation"])

    guessed_entries = [e for e in entries if e["status"] == "guessed"]
    if guessed_entries:
        lines.append("")
        lines.extend(
            f'*"{e["activity"]}" → {e["category"]} (guessed category)' for e in guessed_entries
        )

    lines.append("")
    lines.append(f"**{VERDICT_TIER_LABELS[tier]}**")
    if content["verdict"]:
        lines.append(content["verdict"])

    await send_and_log(context, "\n".join(lines))

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO daily_summaries (date, payload_json, tier, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                payload_json = excluded.payload_json,
                tier = excluded.tier,
                sent_at = excluded.sent_at
            """,
            (date, json.dumps({**facts, **content}, default=str), tier, now.isoformat()),
        )

    if vault.is_configured():
        try:
            await export_day_to_vault(date)
        except Exception as exc:
            logger.warning("vault export failed for %s: %s", date, exc)
            await send_and_log(context, f"(Vault sync failed for {date} — will retry next time.)")
    else:
        logger.info("vault export skipped for %s: VAULT_* env vars not set", date)


def render_day_markdown(date: str) -> str:
    entries = get_entries_for_date(date)
    intention_text = get_intention(date)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        summary_row = conn.execute(
            "SELECT payload_json, tier FROM daily_summaries WHERE date = ?", (date,)
        ).fetchone()

    lines = [f"# {date}", "", f"**Intention:** {intention_text or '_none set_'}", ""]
    lines.append("| Hour | What I did | Category | Min | Win |")
    lines.append("|------|------------|----------|-----|-----|")
    for e in entries:
        hour = datetime.fromisoformat(e["created_at"]).strftime("%-I:%M %p")
        category = e["category"] or e["guessed_category"]
        win = "✓" if e["is_accomplishment"] else ""
        activity = e["activity"].replace("|", "\\|")
        lines.append(f"| {hour} | {activity} | {category} | {e['duration_min']} | {win} |")

    lines.append("")
    lines.append("## Summary")
    lines.append("")

    if summary_row is None:
        lines.append("_No summary generated for this day._")
    else:
        payload = json.loads(summary_row["payload_json"])
        tier = summary_row["tier"]
        lines.append(
            f"Tracked: {format_hours(payload['tracked_hours'])} | Untracked: "
            f"{format_hours(payload['untracked_hours'])} "
            f"({payload['untracked_pct']:.0%} of {format_hours(payload['waking_hours'])} awake)"
        )
        lines.append("")
        lines.append(build_bar_chart(payload.get("category_hours", {})))
        if payload.get("accomplishments"):
            lines.append("")
            lines.append("**Wins:**")
            lines.extend(f"- {item}" for item in payload["accomplishments"][:3])
        if payload.get("observation"):
            lines.append("")
            lines.append(payload["observation"])
        lines.append("")
        lines.append(f"**{VERDICT_TIER_LABELS[tier]}**")
        if payload.get("verdict"):
            lines.append(payload["verdict"])

    return "\n".join(lines) + "\n"


async def export_day_to_vault(date: str) -> None:
    """Raises on failure (unconfigured, git/ssh error, etc.) — callers decide
    whether to swallow it (nightly path) or surface it (backfill path)."""
    if not vault.is_configured():
        raise RuntimeError("VAULT_REPO_SSH_URL / VAULT_DEPLOY_KEY not set")
    markdown = render_day_markdown(date)
    relative_path = f"Time Steward/Logs/{date}.md"
    await asyncio.to_thread(vault.push_file, relative_path, markdown, f"Log for {date}")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != MY_CHAT_ID:
        return

    text = update.message.text
    log_message("inbound", text)

    row = get_state()
    intent, pause_hours, entries, day_signal = await classify_intent(text)

    if day_signal:
        mark_day_flag(day_signal)

    if row["awaiting_intention"]:
        set_awaiting_intention(False)
        if intent in ("checkin", "other"):
            store_intention(text)
            await send_and_log(context, f'Love it — "{text}" it is. Go make it happen.')
            return

    if intent == "goodnight":
        set_state("ASLEEP")
        await send_and_log(context, "Goodnight! Pings are paused until 6 AM.")
        await generate_and_send_daily_summary(context)
    elif intent == "pause":
        mark_day_flag("paused_used")
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
            duration = format_minutes(entry["duration_min"])
            if entry["confidence"] == "high":
                create_entry(
                    entry["activity"],
                    entry["category"],
                    entry["duration_min"],
                    "high",
                    status="confirmed",
                    is_accomplishment=entry["is_accomplishment"],
                )
                confirmed_lines.append(f'{entry["activity"]} ({duration}) → {entry["category"]} ✓')
            else:
                create_entry(
                    entry["activity"],
                    entry["category"],
                    entry["duration_min"],
                    "low",
                    status="pending",
                    is_accomplishment=entry["is_accomplishment"],
                )
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

    duration = format_minutes(entry["duration_min"])
    confirmation = f'Logged: {entry["activity"]} ({duration}) → {category["name"]} ✓'
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
        await send_and_log(context, "Logging off — here's your day.")
        await generate_and_send_daily_summary(context)
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


async def backfill_vault() -> None:
    """One-time: export every day that already has a stored daily summary.
    Days with logged entries but no summary (e.g. the bot never reached
    goodnight/hard-stop that day) are skipped — there's nothing finished to
    export for them."""
    with sqlite3.connect(DB_PATH) as conn:
        dates = [r[0] for r in conn.execute("SELECT date FROM daily_summaries ORDER BY date")]

    if not dates:
        print("No daily summaries found — nothing to backfill.")
        return

    print(f"Backfilling {len(dates)} day(s) to the vault...")
    for date in dates:
        try:
            await export_day_to_vault(date)
            print(f"  OK    {date}")
        except Exception as exc:
            print(f"  FAIL  {date}: {exc}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "backfill-vault":
        init_db()
        asyncio.run(backfill_vault())
        return

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_handler(CallbackQueryHandler(handle_category_choice))
    schedule_jobs(app)

    logger.info("Time Steward starting — polling for updates")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
