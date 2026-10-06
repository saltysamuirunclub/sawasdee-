"""SQLite access with simple numbered migrations (PRAGMA user_version)."""
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

log = logging.getLogger(__name__)

# Append new migrations to the end; never edit one that has shipped.
MIGRATIONS: list[str] = [
    # 1: users, Strava tokens, runs, coach messages, push subscriptions
    """
    CREATE TABLE users (
        id                INTEGER PRIMARY KEY,
        strava_athlete_id INTEGER UNIQUE NOT NULL,
        name              TEXT NOT NULL DEFAULT '',
        avatar_url        TEXT,
        language          TEXT NOT NULL DEFAULT 'en',
        is_admin          INTEGER NOT NULL DEFAULT 0,
        coach_enabled     INTEGER NOT NULL DEFAULT 0,
        race_name         TEXT,
        race_date         TEXT,             -- ISO date, e.g. 2026-11-28
        goal_time_s       INTEGER,          -- e.g. 12000 for 3:20:00
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE strava_tokens (
        user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        access_token  TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        expires_at    INTEGER NOT NULL      -- unix seconds
    );

    CREATE TABLE activities (
        id               INTEGER PRIMARY KEY,  -- Strava activity id
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name             TEXT NOT NULL DEFAULT '',
        sport_type       TEXT NOT NULL,
        start_date       TEXT NOT NULL,        -- UTC ISO
        start_date_local TEXT NOT NULL,
        distance_m       REAL NOT NULL,
        moving_time_s    INTEGER NOT NULL,
        elapsed_time_s   INTEGER NOT NULL,
        elevation_gain_m REAL,
        avg_hr           REAL,
        max_hr           REAL,
        workout_type     INTEGER,              -- Strava: 1 race, 2 long run, 3 workout
        category         TEXT,                 -- easy | long | tempo | interval | race
        splits_json      TEXT,
        coach_status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | skipped
        coach_error      TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_activities_user_date ON activities(user_id, start_date);

    CREATE TABLE coach_messages (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL,
        kind        TEXT NOT NULL,           -- run | weekly
        language    TEXT NOT NULL,
        content     TEXT NOT NULL,
        model       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_coach_messages_user ON coach_messages(user_id, created_at);

    CREATE TABLE push_subscriptions (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint   TEXT UNIQUE NOT NULL,
        p256dh     TEXT NOT NULL,
        auth       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.database_path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        log.info("Applying database migration %d", number)
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {number}")
    return len(MIGRATIONS)


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        migrate(conn)
