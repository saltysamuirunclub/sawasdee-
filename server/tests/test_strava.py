import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, strava
from app.main import app


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(strava.time, "sleep", lambda s: None)


def make_user(conn, expires_at):
    user_id = strava.upsert_user(conn, {"id": 42, "firstname": "Juu", "lastname": "S"})
    strava.save_tokens(conn, user_id, {"access_token": "old", "refresh_token": "r1", "expires_at": expires_at})
    return user_id


RUN = {
    "id": 1001, "name": "Morning Tempo", "sport_type": "Run",
    "start_date": "2026-10-01T23:00:00Z", "start_date_local": "2026-10-02T06:00:00Z",
    "distance": 12000.0, "moving_time": 3300, "elapsed_time": 3400,
    "total_elevation_gain": 40.0, "average_heartrate": 160.0, "workout_type": None,
    "splits_metric": [{"split": 1, "distance": 1000, "moving_time": 280, "elevation_difference": 2}],
}


@pytest.mark.parametrize("name,dist,wt,expected", [
    ("Morning Run", 8000, None, "easy"),
    ("Sunday", 24000, None, "long"),
    ("Anything", 10000, 2, "long"),
    ("Tempo Tuesday", 12000, None, "tempo"),
    ("6x800 track", 9000, 3, "interval"),
    ("Workout", 10000, 3, "tempo"),
    ("Samui 10K", 10000, 1, "race"),
])
def test_categorize(name, dist, wt, expected):
    assert strava.categorize(name, dist, wt) == expected


def test_expired_token_is_refreshed_before_call(conn):
    user_id = make_user(conn, expires_at=int(time.time()) - 10)
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2",
                                             "expires_at": int(time.time()) + 3600})
        return httpx.Response(200, json=RUN)

    client = strava.StravaClient(conn, user_id, httpx.Client(transport=httpx.MockTransport(handler)))
    assert strava.import_activity(conn, user_id, 1001, client) is True
    assert calls[0][1] == "/oauth/token"
    assert calls[1][2] == "Bearer new"
    row = conn.execute("SELECT * FROM activities WHERE id = 1001").fetchone()
    assert row["category"] == "tempo"
    assert json.loads(row["splits_json"])[0]["moving_time_s"] == 280
    assert conn.execute("SELECT refresh_token FROM strava_tokens").fetchone()[0] == "r2"


def test_401_triggers_one_refresh_and_retry(conn):
    user_id = make_user(conn, expires_at=int(time.time()) + 3600)

    def handler(request: httpx.Request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2",
                                             "expires_at": int(time.time()) + 3600})
        if request.headers["authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json=RUN)

    client = strava.StravaClient(conn, user_id, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.activity(1001)["id"] == 1001


def test_failed_refresh_raises_auth_error(conn):
    user_id = make_user(conn, expires_at=0)
    handler = lambda request: httpx.Response(400, json={"message": "Bad Request"})
    client = strava.StravaClient(conn, user_id, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(strava.StravaAuthError):
        client.activity(1001)


def test_server_errors_are_retried(conn):
    user_id = make_user(conn, expires_at=int(time.time()) + 3600)
    responses = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, json=RUN)])
    client = strava.StravaClient(conn, user_id,
                                 httpx.Client(transport=httpx.MockTransport(lambda r: next(responses))))
    assert client.activity(1001)["id"] == 1001


def test_non_runs_are_ignored(conn):
    user_id = make_user(conn, expires_at=int(time.time()) + 3600)
    ride = {**RUN, "sport_type": "Ride"}
    client = strava.StravaClient(conn, user_id,
                                 httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ride))))
    assert strava.import_activity(conn, user_id, 1001, client) is None
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_backfill_imports_runs_as_skipped(conn):
    user_id = make_user(conn, expires_at=int(time.time()) + 3600)
    summaries = [{**RUN, "id": 1}, {**RUN, "id": 2, "sport_type": "Swim"}, {**RUN, "id": 3}]

    def handler(request: httpx.Request):
        if request.url.path.endswith("/athlete/activities"):
            return httpx.Response(200, json=summaries)
        activity_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={**RUN, "id": activity_id})

    client = strava.StravaClient(conn, user_id, httpx.Client(transport=httpx.MockTransport(handler)))
    assert strava.backfill(conn, user_id, client=client) == 2
    assert strava.backfill(conn, user_id, client=client) == 0  # already known
    statuses = {r[0] for r in conn.execute("SELECT coach_status FROM activities")}
    assert statuses == {"skipped"}


def test_admin_gets_coach_enabled(conn, monkeypatch):
    monkeypatch.setattr(strava, "settings", strava.settings.__class__(
        **{**strava.settings.__dict__, "admin_strava_id": 42}))
    user_id = strava.upsert_user(conn, {"id": 42, "firstname": "Juu"})
    row = conn.execute("SELECT is_admin, coach_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
    assert tuple(row) == (1, 1)
    other = strava.upsert_user(conn, {"id": 7, "firstname": "Friend"})
    row = conn.execute("SELECT is_admin, coach_enabled FROM users WHERE id = ?", (other,)).fetchone()
    assert tuple(row) == (0, 0)


def test_login_flow(monkeypatch):
    monkeypatch.setattr(strava, "exchange_code", lambda code: {
        "access_token": "a", "refresh_token": "r", "expires_at": int(time.time()) + 3600,
        "athlete": {"id": 99, "firstname": "Test", "lastname": "Runner"},
    })
    monkeypatch.setattr("app.auth._backfill_job", lambda user_id: None)
    with TestClient(app) as client:
        assert client.get("/api/me").status_code == 401
        client.get("/auth/strava", follow_redirects=False)
        bad = client.get("/auth/strava/callback", params={"code": "c", "state": "wrong",
                                                          "scope": "read,activity:read_all"})
        assert bad.status_code == 400

        state = httpx.URL(client.get("/auth/strava", follow_redirects=False).headers["location"]).params["state"]
        resp = client.get("/auth/strava/callback", follow_redirects=False,
                          params={"code": "c", "state": state, "scope": "read,activity:read_all"})
        assert resp.status_code in (302, 307)
        me = client.get("/api/me").json()
        assert me["name"] == "Test Runner" and me["language"] == "en"
