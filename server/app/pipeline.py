"""What happens after a new run is stored: coach feedback + push notification."""
import logging
import sqlite3

from . import coach, i18n, push

log = logging.getLogger(__name__)


def _set_status(conn: sqlite3.Connection, activity_id: int, status: str, error: str | None = None):
    with conn:
        conn.execute("UPDATE activities SET coach_status = ?, coach_error = ? WHERE id = ?",
                     (status, error, activity_id))


def on_new_run(conn: sqlite3.Connection, user_id: int, activity_id: int) -> None:
    user = conn.execute("SELECT coach_enabled, language FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user["coach_enabled"]:
        _set_status(conn, activity_id, "skipped")
        return
    coach_run(conn, user_id, activity_id)


def coach_run(conn: sqlite3.Connection, user_id: int, activity_id: int) -> bool:
    """Ask the coach about one run and notify the member. Returns True on success."""
    lang = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    run = conn.execute("SELECT distance_m, moving_time_s FROM activities WHERE id = ?",
                       (activity_id,)).fetchone()
    title = f"🏃 {run['distance_m'] / 1000:.1f} km · {coach.pace(run['distance_m'], run['moving_time_s'])} /km"
    try:
        reply = coach.run_feedback(conn, user_id, activity_id)
    except coach.CoachError as exc:
        log.error("Coach failed for run %d: %s", activity_id, exc)
        _set_status(conn, activity_id, "failed", str(exc))
        push.send_to_user(conn, user_id, title, i18n.t("push.analysis_failed", lang),
                          url=f"/runs/{activity_id}")
        return False

    coach.save_message(conn, user_id, "run", reply, activity_id)
    _set_status(conn, activity_id, "done")
    push.send_to_user(conn, user_id, title, reply.summary, url=f"/runs/{activity_id}")
    log.info("Coach feedback sent for run %d", activity_id)
    return True


def retry_failed(conn: sqlite3.Connection, user_id: int | None = None) -> int:
    rows = conn.execute(
        "SELECT id, user_id FROM activities WHERE coach_status = 'failed'"
        + (" AND user_id = ?" if user_id else ""), (user_id,) if user_id else ()).fetchall()
    return sum(coach_run(conn, r["user_id"], r["id"]) for r in rows)
