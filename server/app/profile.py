"""Per-member coach profile: filled in the app, sent to the coach with every request."""
import json
import re
import sqlite3
from datetime import date, timedelta

MARATHON_KM = 42.195
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# key: (kind, extra). kinds: int, float, text, time (h:mm:ss -> seconds), choice, days
FIELDS: dict[str, tuple[str, object]] = {
    # training availability
    "runs_per_week":    ("choice", ["3", "4", "5", "6"]),
    "run_days":         ("days", None),
    "long_run_day":     ("choice", DAYS),
    "weekday_max_min":  ("int", (20, 240)),
    # about me
    "age":              ("int", (10, 100)),
    "sex":              ("choice", ["male", "female", "other"]),
    "weight_kg":        ("float", (30, 200)),
    "years_running":    ("float", (0, 80)),
    "current_weekly_km": ("float", (0, 300)),
    "injuries":         ("text", 300),
    "other_sports":     ("text", 200),
    # personal bests
    "pb_5k":            ("time", "m"),
    "pb_10k":           ("time", "m"),
    "pb_half":          ("time", "h"),
    "pb_marathon":      ("time", "h"),
    # heart rate (all optional)
    "max_hr":           ("int", (120, 230)),
    "resting_hr":       ("int", (30, 100)),
    "threshold_hr":     ("int", (100, 220)),
    "hr_source":        ("choice", ["chest", "wrist", "none"]),
    # race extras
    "course":           ("choice", ["flat", "rolling", "hilly"]),
    "race_temp_c":      ("int", (-10, 45)),
    "plan_b_time":      ("time", "h"),
    # coach style
    "coach_tone":       ("choice", ["friendly", "direct", "strict"]),
}


class ProfileError(ValueError):
    pass


def parse_time(value: str, short: str = "m") -> int:
    """h:mm:ss -> seconds. Two parts mean mm:ss (short='m') or h:mm (short='h', e.g. '3:20')."""
    parts = value.strip().split(":")
    if not all(re.fullmatch(r"\d{1,3}", p) for p in parts) or not 1 < len(parts) <= 3:
        raise ProfileError(f"Invalid time '{value}', use h:mm:ss or mm:ss")
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        nums = [0] + nums if short == "m" else nums + [0]
    h, m, s = nums
    if m > 59 or s > 59:
        raise ProfileError(f"Invalid time '{value}'")
    return h * 3600 + m * 60 + s


def format_time(seconds: int | float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_pace(sec_per_km: float) -> str:
    return format_time(sec_per_km)


def clean(data: dict) -> dict:
    """Validate form input; empty values are dropped (= unknown)."""
    out: dict = {}
    for key, raw in data.items():
        if key not in FIELDS:
            raise ProfileError(f"Unknown field '{key}'")
        if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []:
            continue
        kind, extra = FIELDS[key]
        try:
            if kind in ("int", "float"):
                value = int(raw) if kind == "int" else round(float(raw), 1)
                lo, hi = extra
                if not lo <= value <= hi:
                    raise ProfileError(f"{key} must be between {lo} and {hi}")
            elif kind == "text":
                value = str(raw).strip()[:extra]
            elif kind == "time":
                value = parse_time(str(raw), extra)
            elif kind == "choice":
                value = str(raw)
                if value not in extra:
                    raise ProfileError(f"{key} must be one of {extra}")
            elif kind == "days":
                days = raw if isinstance(raw, list) else str(raw).split(",")
                value = [d.strip().lower() for d in days if d.strip()]
                if not value or any(d not in DAYS for d in value):
                    raise ProfileError("run_days must be weekday names like mon,wed,sat")
                value = sorted(set(value), key=DAYS.index)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError(f"Invalid value for {key}") from exc
        out[key] = value
    return out


def target_paces(goal_time_s: int) -> dict[str, tuple[float, float]]:
    """Training pace ranges (sec/km) derived from marathon goal time."""
    mp = goal_time_s / MARATHON_KM
    return {
        "easy": (mp * 1.20, mp * 1.34),
        "long": (mp * 1.16, mp * 1.27),
        "marathon": (mp, mp),
        "tempo": (mp * 0.933, mp * 0.958),
        "intervals": (mp * 0.863, mp * 0.887),
    }


def estimated_max_hr(conn: sqlite3.Connection, user_id: int, weeks: int = 12) -> int | None:
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    row = conn.execute(
        "SELECT MAX(max_hr) FROM activities WHERE user_id = ? AND start_date >= ?",
        (user_id, since),
    ).fetchone()
    return int(row[0]) if row and row[0] else None


def load(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT profile_json FROM users WHERE id = ?", (user_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn: sqlite3.Connection, user_id: int, data: dict) -> dict:
    """Merge validated fields into the stored profile. Empty value = clear that field."""
    current = load(conn, user_id)
    cleaned = clean(data)
    for key in data:
        current.pop(key, None)
    current.update(cleaned)
    with conn:
        conn.execute("UPDATE users SET profile_json = ? WHERE id = ?", (json.dumps(current), user_id))
    return current


def render_for_coach(conn: sqlite3.Connection, user_id: int) -> str:
    """Markdown block describing the member, inserted into the coach prompt."""
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    p = load(conn, user_id)
    unknown = "unknown"

    def val(key, fmt=str, unit=""):
        return f"{fmt(p[key])}{unit}" if key in p else unknown

    lines = ["## Member profile (from the app — this wins over coach.md)"]
    lines.append(f"- Name: {user['name'] or unknown}")
    lines.append(f"- Age: {val('age')} · Sex: {val('sex')} · Weight: {val('weight_kg', unit=' kg')}")
    lines.append(f"- Running for: {val('years_running')} years · Current weekly distance: {val('current_weekly_km', unit=' km')}")
    lines.append(f"- Injuries / weak spots: {val('injuries')}")
    lines.append(f"- Other sports: {val('other_sports')}")
    lines.append("- Personal bests: " + " · ".join(
        f"{label} {val(key, format_time)}" for key, label in
        (("pb_5k", "5K"), ("pb_10k", "10K"), ("pb_half", "Half"), ("pb_marathon", "Marathon"))))

    lines.append("\n### Goal race")
    lines.append(f"- Race: {user['race_name'] or unknown} · Date: {user['race_date'] or unknown}")
    if user["goal_time_s"]:
        lines.append(f"- Goal: {format_time(user['goal_time_s'])} "
                     f"(marathon pace {format_pace(user['goal_time_s'] / MARATHON_KM)} /km)")
    else:
        lines.append("- Goal: unknown")
    lines.append(f"- Course: {val('course')} · Expected temperature: {val('race_temp_c', unit=' °C')} · "
                 f"Plan B: {val('plan_b_time', format_time)}")

    lines.append("\n### Availability")
    lines.append(f"- Runs per week: {val('runs_per_week')}")
    lines.append(f"- Days I can run: {', '.join(p['run_days']) if 'run_days' in p else unknown}")
    lines.append(f"- Long run day: {val('long_run_day')} · Max weekday run: {val('weekday_max_min', unit=' min')}")

    lines.append("\n### Heart rate")
    est = estimated_max_hr(conn, user_id)
    max_hr = val("max_hr") if "max_hr" in p else (
        f"unknown (highest seen in last 12 weeks: {est} bpm — real max is at least this)" if est else unknown)
    lines.append(f"- Max HR: {max_hr} · Resting: {val('resting_hr')} · Threshold: {val('threshold_hr')} · "
                 f"Source: {val('hr_source')}")

    if user["goal_time_s"]:
        lines.append("\n### Target paces (calculated from goal time, before heat adjustment)")
        for name, (lo, hi) in target_paces(user["goal_time_s"]).items():
            rng = format_pace(lo) if lo == hi else f"{format_pace(lo)}–{format_pace(hi)}"
            lines.append(f"- {name}: {rng} /km")

    lines.append(f"\n- Coach tone: {val('coach_tone')}")
    notes = (user["weekly_notes"] or "").strip()
    lines.append(f"\n### This week's notes\n{notes or '(none)'}")
    return "\n".join(lines)
