"""Strava webhook: Strava calls us when an athlete creates/updates/deletes an activity."""
import logging
import sqlite3

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from . import pipeline, strava
from .config import settings
from .db import get_db

log = logging.getLogger(__name__)
router = APIRouter()

SUBSCRIPTIONS_URL = f"{strava.API_URL}/push_subscriptions"


@router.get("/strava/webhook")
def verify(mode: str = Query(alias="hub.mode"),
           token: str = Query(alias="hub.verify_token"),
           challenge: str = Query(alias="hub.challenge")):
    """One-time handshake when the subscription is created."""
    if mode != "subscribe" or not settings.strava_verify_token or token != settings.strava_verify_token:
        raise HTTPException(status_code=403, detail="Verification failed")
    return {"hub.challenge": challenge}


@router.post("/strava/webhook")
async def receive(request: Request, background: BackgroundTasks):
    """Strava needs a 200 within 2 seconds, so the real work runs in the background."""
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    background.add_task(handle_event, event)
    return {"ok": True}


def handle_event(event: dict) -> None:
    try:
        with get_db() as conn:
            process_event(conn, event)
    except Exception:
        log.exception("Failed to process Strava event %s", event)


def process_event(conn: sqlite3.Connection, event: dict,
                  client: strava.StravaClient | None = None) -> None:
    owner = conn.execute(
        "SELECT id FROM users WHERE strava_athlete_id = ?", (event.get("owner_id"),)
    ).fetchone()
    if owner is None:
        log.info("Ignoring event for unknown athlete %s", event.get("owner_id"))
        return
    user_id = owner["id"]
    object_type, aspect = event.get("object_type"), event.get("aspect_type")
    object_id = event.get("object_id")

    if object_type == "athlete":
        if str((event.get("updates") or {}).get("authorized")).lower() == "false":
            # Member disconnected the app on Strava: remove their Strava data.
            with conn:
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            log.info("User %d revoked Strava access; data deleted", user_id)
        return

    if object_type != "activity":
        return

    if aspect == "delete":
        with conn:
            conn.execute("DELETE FROM activities WHERE id = ? AND user_id = ?", (object_id, user_id))
        log.info("Deleted activity %s", object_id)
        return

    try:
        result = strava.import_activity(conn, user_id, object_id, client)
    except strava.StravaAuthError:
        log.warning("User %d needs to reconnect Strava", user_id)
        return
    except strava.StravaError as exc:
        log.warning("Could not fetch activity %s: %s", object_id, exc)
        return

    if result is None:
        # Not a run (or changed away from a run on update): make sure it's gone.
        with conn:
            conn.execute("DELETE FROM activities WHERE id = ? AND user_id = ?", (object_id, user_id))
    elif result is True:
        log.info("Stored new run %s for user %d", object_id, user_id)
        pipeline.on_new_run(conn, user_id, object_id)


# ---- subscription management (used by the CLI) ------------------------------

def _creds() -> dict:
    return {"client_id": settings.strava_client_id, "client_secret": settings.strava_client_secret}


def list_subscriptions(http: httpx.Client | None = None) -> list[dict]:
    resp = (http or httpx.Client()).get(SUBSCRIPTIONS_URL, params=_creds(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def create_subscription(http: httpx.Client | None = None) -> dict:
    """Strava immediately calls GET /strava/webhook to verify, so the app must be running."""
    resp = (http or httpx.Client()).post(SUBSCRIPTIONS_URL, timeout=30, data={
        **_creds(),
        "callback_url": f"{settings.base_url}/strava/webhook",
        "verify_token": settings.strava_verify_token,
    })
    if resp.status_code >= 400:
        raise strava.StravaError(f"Subscription failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def delete_subscription(subscription_id: int, http: httpx.Client | None = None) -> None:
    resp = (http or httpx.Client()).delete(f"{SUBSCRIPTIONS_URL}/{subscription_id}",
                                           params=_creds(), timeout=20)
    resp.raise_for_status()
