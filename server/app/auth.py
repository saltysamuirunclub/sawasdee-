"""Login with Strava: each club member is a Strava athlete."""
import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import strava
from .db import get_db

log = logging.getLogger(__name__)
router = APIRouter()


def current_user_id(request: Request) -> int | None:
    return request.session.get("user_id")


def require_user(request: Request) -> int:
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user_id


def _backfill_job(user_id: int) -> None:
    try:
        with get_db() as conn:
            strava.backfill(conn, user_id)
    except Exception:  # never let a background job crash silently
        log.exception("Backfill failed for user %d", user_id)


@router.get("/auth/strava")
def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(strava.authorize_url(state))


@router.get("/auth/strava/callback")
def callback(request: Request, background: BackgroundTasks,
             code: str | None = None, state: str | None = None,
             scope: str = "", error: str | None = None):
    expected = request.session.pop("oauth_state", None)
    if error or not code:
        log.info("Strava login cancelled: %s", error)
        return RedirectResponse("/?login=cancelled")
    if not expected or state != expected:
        raise HTTPException(status_code=400, detail="Invalid login state, please try again")
    if "activity:read" not in scope:
        return RedirectResponse("/?login=missing_scope")

    try:
        token = strava.exchange_code(code)
    except strava.StravaError:
        log.exception("Strava code exchange failed")
        return RedirectResponse("/?login=failed")

    with get_db() as conn:
        user_id = strava.upsert_user(conn, token["athlete"])
        strava.save_tokens(conn, user_id, token)
        has_runs = conn.execute(
            "SELECT 1 FROM activities WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()

    request.session["user_id"] = user_id
    if not has_runs:
        background.add_task(_backfill_job, user_id)
    log.info("User %d logged in", user_id)
    return RedirectResponse("/")


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
