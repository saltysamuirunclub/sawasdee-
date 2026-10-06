"""Web push notifications (works on iPhone/iPad home-screen apps, iOS 16.4+)."""
import base64
import json
import logging
import sqlite3

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .config import settings

log = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """Returns (public_key, private_key) as base64url strings for the .env file."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(public), _b64url(private)


def save_subscription(conn: sqlite3.Connection, user_id: int, sub: dict) -> None:
    try:
        endpoint, keys = sub["endpoint"], sub["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (KeyError, TypeError):
        raise ValueError("Invalid push subscription")
    if not str(endpoint).startswith("https://"):
        raise ValueError("Invalid push endpoint")
    with conn:
        conn.execute(
            """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,
                 p256dh=excluded.p256dh, auth=excluded.auth""",
            (user_id, endpoint, p256dh, auth),
        )


def delete_subscription(conn: sqlite3.Connection, user_id: int, endpoint: str) -> None:
    with conn:
        conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                     (user_id, endpoint))


def send_to_user(conn: sqlite3.Connection, user_id: int, title: str, body: str,
                 url: str = "/") -> int:
    """Send to all of the user's devices. Returns how many succeeded. Never raises."""
    if not settings.vapid_private_key:
        log.warning("VAPID keys not configured; skipping push to user %d", user_id)
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    rows = conn.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?", (user_id,)
    ).fetchall()
    for row in rows:
        try:
            webpush(
                subscription_info={"endpoint": row["endpoint"],
                                   "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_contact},
                ttl=24 * 3600,
                timeout=15,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):  # device unsubscribed or app removed
                with conn:
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
                log.info("Removed expired push subscription %d", row["id"])
            else:
                log.warning("Push to subscription %d failed: %s", row["id"], exc)
        except Exception:
            log.exception("Push to subscription %d failed", row["id"])
    if not rows:
        log.info("User %d has no devices with notifications enabled", user_id)
    return sent
