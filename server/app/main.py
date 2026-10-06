"""FastAPI entry point. Run with: uvicorn app.main:app (from the server/ folder)."""
import logging
from contextlib import asynccontextmanager

from datetime import date

from fastapi import Body, Depends, FastAPI, HTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import auth, i18n, profile, webhook
from .config import settings
from .db import get_db, init_db
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    log.info("Salty Island Run Club started")
    yield


app = FastAPI(title="Salty Island Run Club", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="sirc_session",
    max_age=180 * 86400,
    same_site="lax",
    https_only=settings.base_url.startswith("https://"),
)
app.include_router(auth.router)
app.include_router(webhook.router)


@app.get("/healthz")
def healthz() -> dict:
    with get_db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/me")
def me(user_id: int = Depends(auth.require_user)) -> dict:
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, name, avatar_url, language, is_admin, coach_enabled, race_name, race_date, goal_time_s "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        runs = conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ?", (user_id,)).fetchone()[0]
    return {**dict(user), "runs": runs}


@app.get("/api/profile")
def get_profile(user_id: int = Depends(auth.require_user)) -> dict:
    with get_db() as conn:
        user = conn.execute(
            "SELECT language, race_name, race_date, goal_time_s, weekly_notes FROM users WHERE id = ?",
            (user_id,)).fetchone()
        return {
            "language": user["language"],
            "race_name": user["race_name"],
            "race_date": user["race_date"],
            "goal_time": profile.format_time(user["goal_time_s"]) if user["goal_time_s"] else None,
            "weekly_notes": user["weekly_notes"],
            "profile": profile.load(conn, user_id),
            "estimated_max_hr": profile.estimated_max_hr(conn, user_id),
            "fields": {k: {"kind": kind, "options": extra if kind == "choice" else None}
                       for k, (kind, extra) in profile.FIELDS.items()},
            "languages": {code: i18n.LANGUAGE_NAMES[code] for code in i18n.supported_languages()},
        }


@app.put("/api/profile")
def update_profile(payload: dict = Body(...), user_id: int = Depends(auth.require_user)) -> dict:
    """Partial update: only keys present in the payload change."""
    updates: dict = {}
    try:
        if "language" in payload:
            updates["language"] = i18n.normalize_language(payload["language"])
        if "race_name" in payload:
            updates["race_name"] = (payload["race_name"] or "").strip()[:100] or None
        if "race_date" in payload:
            raw = payload["race_date"]
            updates["race_date"] = date.fromisoformat(raw).isoformat() if raw else None
        if "goal_time" in payload:
            raw = payload["goal_time"]
            updates["goal_time_s"] = profile.parse_time(raw, "h") if raw else None
        if "weekly_notes" in payload:
            updates["weekly_notes"] = (payload["weekly_notes"] or "").strip()[:1000]
        with get_db() as conn:
            if "profile" in payload:
                profile.save(conn, user_id, payload["profile"] or {})
            if updates:
                cols = ", ".join(f"{k} = ?" for k in updates)
                with conn:
                    conn.execute(f"UPDATE users SET {cols} WHERE id = ?", (*updates.values(), user_id))
    except (profile.ProfileError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return get_profile(user_id)
