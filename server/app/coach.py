"""The AI coach: builds the prompt from coach.md + member profile + runs, calls Claude."""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from . import i18n, profile
from .config import ROOT, settings

log = logging.getLogger(__name__)

COACH_FILE = ROOT / "coach.md"
HISTORY_DAYS = 28
MAX_TOKENS = 2000


class CoachError(Exception):
    pass


@dataclass
class CoachReply:
    content: str
    summary: str
    model: str


_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise CoachError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=3)
    return _client


def today_local() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


# ---- formatting helpers ------------------------------------------------------

def pace(distance_m: float, seconds: float) -> str:
    return profile.format_pace(seconds / (distance_m / 1000)) if distance_m else "-"


def _hr(value) -> str:
    return f"{value:.0f}" if value else "n/a"


def describe_run(row: sqlite3.Row, with_splits: bool = True) -> str:
    lines = [
        f"- Date: {row['start_date_local'][:16].replace('T', ' ')} (local)",
        f"- Title: {row['name']} · Type: {row['category']} ({row['sport_type']})",
        f"- Distance: {row['distance_m'] / 1000:.2f} km · Moving time: {profile.format_time(row['moving_time_s'])}"
        f" · Pace: {pace(row['distance_m'], row['moving_time_s'])} /km",
        f"- Elevation gain: {row['elevation_gain_m'] or 0:.0f} m",
        f"- Heart rate: avg {_hr(row['avg_hr'])} · max {_hr(row['max_hr'])}",
    ]
    if row["description"]:
        lines.append(f"- Member's note: {row['description'].strip()[:500]}")
    if with_splits and row["splits_json"]:
        splits = json.loads(row["splits_json"])
        if splits:
            lines.append("- Splits per km: " + " | ".join(
                f"{s['km']}: {pace(s['distance_m'] or 0, s['moving_time_s'] or 0)}"
                + (f" @{s['avg_hr']:.0f}" if s.get("avg_hr") else "")
                for s in splits))
    return "\n".join(lines)


def history_table(conn: sqlite3.Connection, user_id: int, until: str, days: int = HISTORY_DAYS,
                  exclude_id: int | None = None) -> str:
    since = (datetime.fromisoformat(until.replace("Z", "+00:00")) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM activities WHERE user_id = ? AND start_date >= ? AND start_date <= ?
           AND id IS NOT ? ORDER BY start_date""",
        (user_id, since[:19], until, exclude_id),
    ).fetchall()
    if not rows:
        return "(no runs in this period)"
    out = ["| Date | Type | km | Time | Pace | Avg HR | Title |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['start_date_local'][:10]} | {r['category']} | {r['distance_m'] / 1000:.1f} | "
                   f"{profile.format_time(r['moving_time_s'])} | {pace(r['distance_m'], r['moving_time_s'])} | "
                   f"{_hr(r['avg_hr'])} | {r['name'][:40]} |")
    weekly = conn.execute(
        """SELECT strftime('%Y-W%W', start_date_local) AS wk, SUM(distance_m)/1000.0, COUNT(*)
           FROM activities WHERE user_id = ? AND start_date >= ? AND start_date <= ?
           GROUP BY wk ORDER BY wk""",
        (user_id, since[:19], until),
    ).fetchall()
    out.append("\nWeekly totals (incl. this run): " + " · ".join(f"{w[0]}: {w[1]:.1f} km ({w[2]} runs)" for w in weekly))
    return "\n".join(out)


def race_context(user: sqlite3.Row) -> str:
    today = today_local()
    text = f"Today: {today.isoformat()} ({today.strftime('%A')})"
    if user["race_date"]:
        days = (date.fromisoformat(user["race_date"]) - today).days
        text += f" · Days to race: {days} (~{days / 7:.1f} weeks)"
    return text


def language_instruction(user: sqlite3.Row) -> str:
    lang = i18n.normalize_language(user["language"])
    return f"Answer in {i18n.LANGUAGE_NAMES[lang]}."


# ---- Claude call -------------------------------------------------------------

def extract_summary(text: str) -> str:
    match = re.search(r"\*\*Summary:?\*\*:?\s*(.+)", text)
    line = match.group(1) if match else next((l for l in text.splitlines() if l.strip()), "")
    line = line.strip().strip("*").strip()
    return line if len(line) <= 120 else line[:117].rstrip() + "…"


def ask_claude(system: str, prompt: str) -> CoachReply:
    try:
        response = client().messages.create(
            model=settings.claude_model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as exc:
        raise CoachError("Anthropic API key is invalid") from exc
    except anthropic.RateLimitError as exc:
        raise CoachError("Anthropic rate limit reached, try again later") from exc
    except anthropic.APIStatusError as exc:
        raise CoachError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise CoachError("Could not reach the Anthropic API") from exc

    if response.stop_reason == "refusal":
        raise CoachError("Claude declined this request")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise CoachError(f"Empty answer (stop_reason={response.stop_reason})")
    if response.stop_reason == "max_tokens":
        log.warning("Coach answer was cut off at max_tokens")
    log.info("Claude usage: in=%s out=%s request=%s", response.usage.input_tokens,
             response.usage.output_tokens, response._request_id)
    return CoachReply(content=text, summary=extract_summary(text), model=response.model)


def system_prompt() -> str:
    try:
        return COACH_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoachError(f"Cannot read {COACH_FILE}: {exc}") from exc


def run_feedback(conn: sqlite3.Connection, user_id: int, activity_id: int) -> CoachReply:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    run = conn.execute("SELECT * FROM activities WHERE id = ? AND user_id = ?",
                       (activity_id, user_id)).fetchone()
    if run is None:
        raise CoachError(f"Run {activity_id} not found")
    prompt = "\n\n".join([
        "# Task\nEvaluate my new run and give me my next session. Use the 'After each run' format.",
        race_context(user),
        profile.render_for_coach(conn, user_id),
        "## New run\n" + describe_run(run),
        f"## My runs in the {HISTORY_DAYS} days before\n"
        + history_table(conn, user_id, run["start_date"], exclude_id=run["id"]),
        language_instruction(user),
    ])
    return ask_claude(system_prompt(), prompt)


def weekly_plan(conn: sqlite3.Connection, user_id: int) -> CoachReply:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    today = today_local()
    prompt = "\n\n".join([
        "# Task\nIt's Monday. Review my last week and create this week's plan (Monday to Sunday). "
        "Use the 'Monday weekly plan' format.",
        race_context(user),
        profile.render_for_coach(conn, user_id),
        f"## My runs in the last {HISTORY_DAYS} days\n"
        + history_table(conn, user_id, datetime.now(ZoneInfo("UTC")).isoformat()[:19]),
        "## Last week's plan\n" + (last_weekly_plan(conn, user_id, today) or "(none)"),
        language_instruction(user),
    ])
    return ask_claude(system_prompt(), prompt)


def last_weekly_plan(conn: sqlite3.Connection, user_id: int, before: date) -> str | None:
    row = conn.execute(
        """SELECT content FROM coach_messages WHERE user_id = ? AND kind = 'weekly'
           AND created_at < ? ORDER BY created_at DESC LIMIT 1""",
        (user_id, before.isoformat()),
    ).fetchone()
    return row["content"] if row else None


def save_message(conn: sqlite3.Connection, user_id: int, kind: str, reply: CoachReply,
                 activity_id: int | None = None) -> int:
    language = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    with conn:
        cur = conn.execute(
            """INSERT INTO coach_messages (user_id, activity_id, kind, language, content, summary, model)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, activity_id, kind, language, reply.content, reply.summary, reply.model),
        )
    return cur.lastrowid
