"""FastAPI entry point. Run with: uvicorn app.main:app (from the server/ folder)."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import auth, webhook
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
