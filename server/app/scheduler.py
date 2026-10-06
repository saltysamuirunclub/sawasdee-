"""Monday morning weekly plan (06:00 local time)."""
import logging
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import coach, i18n, pipeline, push
from .config import settings
from .db import get_db

log = logging.getLogger(__name__)


def week_start_utc() -> str:
    """Start of the current week (Monday 00:00 local) in UTC, as stored in created_at."""
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    monday = datetime.combine(today - timedelta(days=today.weekday()), time(0), tz)
    return monday.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def has_plan_this_week(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM coach_messages WHERE user_id = ? AND kind = 'weekly' AND created_at >= ?",
        (user_id, week_start_utc()),
    ).fetchone() is not None


def send_weekly_plan(conn: sqlite3.Connection, user_id: int, force: bool = False) -> bool:
    if not force and has_plan_this_week(conn, user_id):
        log.info("User %d already has a plan this week", user_id)
        return False
    lang = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    try:
        reply = coach.weekly_plan(conn, user_id)
    except coach.CoachError as exc:
        log.error("Weekly plan failed for user %d: %s", user_id, exc)
        push.send_to_user(conn, user_id, i18n.t("push.weekly_title", lang),
                          i18n.t("push.weekly_failed", lang), url="/plan")
        return False
    coach.save_message(conn, user_id, "weekly", reply)
    push.send_to_user(conn, user_id, i18n.t("push.weekly_title", lang), reply.summary, url="/plan")
    log.info("Weekly plan sent to user %d", user_id)
    return True


def weekly_job() -> None:
    try:
        with get_db() as conn:
            users = [r[0] for r in conn.execute("SELECT id FROM users WHERE coach_enabled = 1")]
            for user_id in users:
                try:
                    send_weekly_plan(conn, user_id)
                except Exception:
                    log.exception("Weekly plan crashed for user %d", user_id)
    except Exception:
        log.exception("Weekly job failed")


def retry_job() -> None:
    """Retry coach analysis for runs that failed in the last 2 days."""
    try:
        with get_db() as conn:
            n = pipeline.retry_failed(conn, max_age_days=2)
            if n:
                log.info("Retried %d failed runs", n)
    except Exception:
        log.exception("Retry job failed")


def start() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        weekly_job,
        # 06:00 is the real send; later runs only catch users whose plan failed
        CronTrigger(day_of_week="mon", hour="6,9,13,18", minute=0, timezone=settings.timezone),
        id="weekly_plan",
        misfire_grace_time=6 * 3600,  # still run if the server was down at 06:00
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(retry_job, "interval", hours=3, id="retry_failed", replace_existing=True)
    scheduler.start()
    log.info("Scheduler started; next weekly plan at %s", scheduler.get_job("weekly_plan").next_run_time)
    return scheduler
