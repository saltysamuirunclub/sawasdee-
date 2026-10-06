"""What happens after a new run is stored. The coach is wired in here in step 4."""
import logging
import sqlite3

log = logging.getLogger(__name__)


def on_new_run(conn: sqlite3.Connection, user_id: int, activity_id: int) -> None:
    coach_enabled = conn.execute(
        "SELECT coach_enabled FROM users WHERE id = ?", (user_id,)
    ).fetchone()[0]
    if not coach_enabled:
        with conn:
            conn.execute("UPDATE activities SET coach_status='skipped' WHERE id=?", (activity_id,))
        return
    log.info("New run %d for user %d is waiting for the coach", activity_id, user_id)
