import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, strava, webhook
from app.main import app
from tests.test_strava import RUN


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def user(conn, coach_enabled=1):
    user_id = strava.upsert_user(conn, {"id": 42, "firstname": "Juu"})
    strava.save_tokens(conn, user_id, {"access_token": "a", "refresh_token": "r",
                                       "expires_at": int(time.time()) + 3600})
    with conn:
        conn.execute("UPDATE users SET coach_enabled = ? WHERE id = ?", (coach_enabled, user_id))
    return user_id


def client_returning(conn, user_id, payload):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    return strava.StravaClient(conn, user_id, http)


def event(aspect="create", **kw):
    return {"object_type": "activity", "object_id": 1001, "aspect_type": aspect,
            "owner_id": 42, "updates": {}, **kw}


def test_verify_handshake(monkeypatch):
    monkeypatch.setattr(webhook, "settings", webhook.settings.__class__(
        **{**webhook.settings.__dict__, "strava_verify_token": "secret"}))
    with TestClient(app) as c:
        ok = c.get("/strava/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secret",
                                              "hub.challenge": "abc"})
        assert ok.json() == {"hub.challenge": "abc"}
        bad = c.get("/strava/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                               "hub.challenge": "abc"})
        assert bad.status_code == 403


def test_post_returns_immediately_and_queues(monkeypatch):
    seen = []
    monkeypatch.setattr(webhook, "handle_event", seen.append)
    with TestClient(app) as c:
        assert c.post("/strava/webhook", json=event()).status_code == 200
    assert seen == [event()]


def test_new_run_is_stored_and_triggers_pipeline(conn, monkeypatch):
    user_id = user(conn)
    triggered = []
    monkeypatch.setattr(webhook.pipeline, "on_new_run", lambda c, u, a: triggered.append((u, a)))
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))  # duplicate delivery
    assert triggered == [(user_id, 1001)]
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_update_renames_without_retriggering(conn, monkeypatch):
    user_id = user(conn)
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    triggered = []
    monkeypatch.setattr(webhook.pipeline, "on_new_run", lambda *a: triggered.append(a))
    webhook.process_event(conn, event("update"), client_returning(conn, user_id, {**RUN, "name": "Long one"}))
    assert triggered == []
    assert conn.execute("SELECT name FROM activities").fetchone()[0] == "Long one"


def test_update_to_non_run_removes_it(conn):
    user_id = user(conn)
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    webhook.process_event(conn, event("update"), client_returning(conn, user_id, {**RUN, "sport_type": "Walk"}))
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_delete(conn):
    user_id = user(conn)
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    webhook.process_event(conn, event("delete"))
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_member_without_coach_is_skipped(conn):
    user_id = user(conn, coach_enabled=0)
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    assert conn.execute("SELECT coach_status FROM activities").fetchone()[0] == "skipped"


def test_deauthorization_deletes_user_data(conn):
    user_id = user(conn)
    webhook.process_event(conn, event(), client_returning(conn, user_id, RUN))
    webhook.process_event(conn, {"object_type": "athlete", "object_id": 42, "aspect_type": "update",
                                 "owner_id": 42, "updates": {"authorized": "false"}})
    for table in ("users", "activities", "strava_tokens"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_unknown_athlete_is_ignored(conn):
    webhook.process_event(conn, {**event(), "owner_id": 999})  # must not raise or call Strava
