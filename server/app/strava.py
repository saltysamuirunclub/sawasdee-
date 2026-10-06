"""Strava API: OAuth, automatic token refresh, fetching and parsing runs."""
import json
import logging
import sqlite3
import time
from urllib.parse import urlencode

import httpx

from .config import settings

log = logging.getLogger(__name__)

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_URL = "https://www.strava.com/api/v3"
SCOPE = "read,activity:read_all"
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
REFRESH_MARGIN_S = 300


class StravaError(Exception):
    pass


class StravaAuthError(StravaError):
    """The user must reconnect Strava (token revoked or scope missing)."""


def authorize_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.strava_client_id,
        "redirect_uri": f"{settings.base_url}/auth/strava/callback",
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    })


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """HTTP call with retries on network errors, 429 and 5xx."""
    delay = 2.0
    for attempt in range(4):
        try:
            resp = client.request(method, url, timeout=20, **kwargs)
        except httpx.TransportError as exc:
            log.warning("Strava network error (%s), attempt %d", exc, attempt + 1)
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            log.warning("Strava returned %d, attempt %d", resp.status_code, attempt + 1)
        if attempt < 3:
            time.sleep(delay)
            delay *= 2
    raise StravaError(f"Strava request failed after retries: {method} {url}")


def exchange_code(code: str, client: httpx.Client | None = None) -> dict:
    """Trade the OAuth callback code for tokens + athlete profile."""
    client = client or httpx.Client()
    resp = _request(client, "POST", TOKEN_URL, data={
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    if resp.status_code != 200:
        raise StravaAuthError(f"Code exchange failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def save_tokens(conn: sqlite3.Connection, user_id: int, token: dict) -> None:
    with conn:
        conn.execute(
            """INSERT INTO strava_tokens (user_id, access_token, refresh_token, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token, expires_at=excluded.expires_at""",
            (user_id, token["access_token"], token["refresh_token"], token["expires_at"]),
        )


def upsert_user(conn: sqlite3.Connection, athlete: dict) -> int:
    """Create or update the user for a Strava athlete; returns users.id."""
    athlete_id = int(athlete["id"])
    name = " ".join(p for p in (athlete.get("firstname"), athlete.get("lastname")) if p)
    is_admin = int(athlete_id == settings.admin_strava_id)
    with conn:
        conn.execute(
            """INSERT INTO users (strava_athlete_id, name, avatar_url, is_admin, coach_enabled)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(strava_athlete_id) DO UPDATE SET name=excluded.name,
                 avatar_url=excluded.avatar_url,
                 is_admin=MAX(users.is_admin, excluded.is_admin),
                 coach_enabled=MAX(users.coach_enabled, excluded.coach_enabled)""",
            (athlete_id, name, athlete.get("profile"), is_admin, is_admin),
        )
    return conn.execute(
        "SELECT id FROM users WHERE strava_athlete_id = ?", (athlete_id,)
    ).fetchone()[0]


class StravaClient:
    """Per-user API client that keeps the access token fresh."""

    def __init__(self, conn: sqlite3.Connection, user_id: int, http: httpx.Client | None = None):
        self.conn = conn
        self.user_id = user_id
        self.http = http or httpx.Client()

    def _token(self, force_refresh: bool = False) -> str:
        row = self.conn.execute(
            "SELECT access_token, refresh_token, expires_at FROM strava_tokens WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        if row is None:
            raise StravaAuthError("No Strava tokens stored for user")
        if not force_refresh and row["expires_at"] > time.time() + REFRESH_MARGIN_S:
            return row["access_token"]

        log.info("Refreshing Strava token for user %d", self.user_id)
        resp = _request(self.http, "POST", TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": row["refresh_token"],
        })
        if resp.status_code != 200:
            raise StravaAuthError(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")
        token = resp.json()
        save_tokens(self.conn, self.user_id, token)
        return token["access_token"]

    def get(self, path: str, **params) -> dict | list:
        for force_refresh in (False, True):
            headers = {"Authorization": f"Bearer {self._token(force_refresh)}"}
            resp = _request(self.http, "GET", API_URL + path, headers=headers, params=params)
            if resp.status_code == 401 and not force_refresh:
                continue  # token rejected early: refresh once and retry
            if resp.status_code == 401:
                raise StravaAuthError("Strava rejected the refreshed token")
            if resp.status_code == 404:
                raise StravaError(f"Not found: {path}")
            resp.raise_for_status()
            return resp.json()
        raise StravaError("unreachable")

    def activity(self, activity_id: int) -> dict:
        return self.get(f"/activities/{activity_id}")

    def activities_since(self, after_ts: int) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self.get("/athlete/activities", after=after_ts, per_page=100, page=page)
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1


# ---- parsing & storage -------------------------------------------------------

TEMPO_WORDS = ("tempo", "threshold", "schwelle", "marathon pace", " mp", "progression")
INTERVAL_WORDS = ("interval", "intervall", "repeats", "x400", "x800", "x1000", "x1k", "fartlek", "track")
LONG_WORDS = ("long run", "longrun", "langer lauf", "long")
LONG_RUN_MIN_M = 18_000


def categorize(name: str, distance_m: float, workout_type: int | None) -> str:
    """easy | long | tempo | interval | race — Strava's workout type wins, then name, then distance."""
    lowered = f" {name.lower()} "
    if workout_type == 1:
        return "race"
    if workout_type == 2:
        return "long"
    if any(w in lowered for w in INTERVAL_WORDS):
        return "interval"
    if any(w in lowered for w in TEMPO_WORDS):
        return "tempo"
    if workout_type == 3:
        return "tempo"
    if any(w in lowered for w in LONG_WORDS) or distance_m >= LONG_RUN_MIN_M:
        return "long"
    return "easy"


def parse_splits(detail: dict) -> list[dict]:
    return [
        {
            "km": s.get("split"),
            "distance_m": s.get("distance"),
            "moving_time_s": s.get("moving_time"),
            "elevation_m": s.get("elevation_difference"),
            "avg_hr": s.get("average_heartrate"),
        }
        for s in detail.get("splits_metric") or []
    ]


def is_run(activity: dict) -> bool:
    return (activity.get("sport_type") or activity.get("type")) in RUN_TYPES


def store_activity(conn: sqlite3.Connection, user_id: int, a: dict) -> bool:
    """Insert or update a run. Returns True if it was new."""
    name = a.get("name") or ""
    workout_type = a.get("workout_type")
    exists = conn.execute("SELECT 1 FROM activities WHERE id = ?", (a["id"],)).fetchone()
    with conn:
        conn.execute(
            """INSERT INTO activities (id, user_id, name, sport_type, start_date, start_date_local,
                 distance_m, moving_time_s, elapsed_time_s, elevation_gain_m, avg_hr, max_hr,
                 workout_type, category, splits_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, sport_type=excluded.sport_type,
                 distance_m=excluded.distance_m, moving_time_s=excluded.moving_time_s,
                 elapsed_time_s=excluded.elapsed_time_s, elevation_gain_m=excluded.elevation_gain_m,
                 avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, workout_type=excluded.workout_type,
                 category=excluded.category,
                 splits_json=COALESCE(excluded.splits_json, activities.splits_json)""",
            (
                a["id"], user_id, name, a.get("sport_type") or a.get("type"),
                a["start_date"], a["start_date_local"], a.get("distance") or 0,
                a.get("moving_time") or 0, a.get("elapsed_time") or 0,
                a.get("total_elevation_gain"), a.get("average_heartrate"), a.get("max_heartrate"),
                workout_type, categorize(name, a.get("distance") or 0, workout_type),
                json.dumps(parse_splits(a)) if "splits_metric" in a else None,
            ),
        )
    return exists is None


def import_activity(conn: sqlite3.Connection, user_id: int, activity_id: int,
                    client: StravaClient | None = None) -> bool | None:
    """Fetch one activity with splits and store it if it's a run.
    Returns True if new, False if updated, None if not a run."""
    client = client or StravaClient(conn, user_id)
    detail = client.activity(activity_id)
    if not is_run(detail):
        return None
    return store_activity(conn, user_id, detail)


def backfill(conn: sqlite3.Connection, user_id: int, weeks: int = 8,
             client: StravaClient | None = None) -> int:
    """Import the last N weeks of runs (marked as already coached). Returns count imported."""
    client = client or StravaClient(conn, user_id)
    after = int(time.time()) - weeks * 7 * 86400
    count = 0
    for summary in client.activities_since(after):
        if not is_run(summary):
            continue
        known = conn.execute("SELECT 1 FROM activities WHERE id = ?", (summary["id"],)).fetchone()
        if known:
            continue
        try:
            store_activity(conn, user_id, client.activity(summary["id"]))
        except StravaError as exc:
            log.warning("Backfill: detail fetch failed for %s (%s); storing summary", summary["id"], exc)
            store_activity(conn, user_id, summary)
        with conn:  # history should not trigger coach messages
            conn.execute("UPDATE activities SET coach_status='skipped' WHERE id=?", (summary["id"],))
        count += 1
    log.info("Backfill for user %d imported %d runs", user_id, count)
    return count
