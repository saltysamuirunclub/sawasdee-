import pytest
from fastapi.testclient import TestClient

from app import db, profile, strava
from app.main import app


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def test_time_parsing():
    assert profile.parse_time("3:20", "h") == 12000
    assert profile.parse_time("3:20:00") == 12000
    assert profile.parse_time("21:30") == 1290          # 5K as mm:ss
    with pytest.raises(profile.ProfileError):
        profile.parse_time("3:75", "h")
    assert profile.format_time(12000) == "3:20:00"


def test_paces_for_320_goal():
    paces = {k: (profile.format_pace(lo), profile.format_pace(hi))
             for k, (lo, hi) in profile.target_paces(12000).items()}
    assert paces["marathon"] == ("4:44", "4:44")
    assert paces["easy"] == ("5:41", "6:21")
    assert paces["tempo"] == ("4:25", "4:32")


def test_clean_validates_and_drops_empty():
    out = profile.clean({"age": "38", "run_days": "sun, Tue,tue", "max_hr": "", "pb_half": "1:32:10"})
    assert out == {"age": 38, "run_days": ["tue", "sun"], "pb_half": 5530}
    for bad in ({"age": "5"}, {"runs_per_week": "9"}, {"run_days": "funday"}, {"nope": 1}):
        with pytest.raises(profile.ProfileError):
            profile.clean(bad)


def test_save_merges_and_clears(conn):
    user_id = strava.upsert_user(conn, {"id": 1, "firstname": "A"})
    profile.save(conn, user_id, {"age": 38, "max_hr": 185})
    profile.save(conn, user_id, {"max_hr": ""})            # clearing a field
    assert profile.load(conn, user_id) == {"age": 38}


def test_render_marks_unknowns_and_estimates_hr(conn):
    user_id = strava.upsert_user(conn, {"id": 1, "firstname": "Juu"})
    with conn:
        conn.execute("UPDATE users SET race_date='2026-11-28', goal_time_s=12000 WHERE id=?", (user_id,))
        conn.execute("""INSERT INTO activities (id, user_id, sport_type, start_date, start_date_local,
            distance_m, moving_time_s, elapsed_time_s, max_hr) VALUES (1, ?, 'Run', date('now'),
            date('now'), 10000, 3000, 3000, 182)""", (user_id,))
    text = profile.render_for_coach(conn, user_id)
    assert "marathon pace 4:44 /km" in text
    assert "highest seen in last 12 weeks: 182 bpm" in text
    assert "Age: unknown" in text


def test_profile_api(monkeypatch):
    monkeypatch.setattr(strava, "exchange_code", lambda code: {
        "access_token": "a", "refresh_token": "r", "expires_at": 9999999999,
        "athlete": {"id": 555, "firstname": "Api"}})
    monkeypatch.setattr("app.auth._backfill_job", lambda user_id: None)
    with TestClient(app) as c:
        import httpx
        state = httpx.URL(c.get("/auth/strava", follow_redirects=False).headers["location"]).params["state"]
        c.get("/auth/strava/callback", params={"code": "x", "state": state, "scope": "activity:read_all"})
        r = c.put("/api/profile", json={"language": "de", "goal_time": "3:20", "race_date": "2026-11-28",
                                        "weekly_notes": "Travelling Thu", "profile": {"runs_per_week": "4"}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["language"] == "de" and body["goal_time"] == "3:20:00"
        assert body["profile"] == {"runs_per_week": "4"}
        assert c.put("/api/profile", json={"profile": {"age": "500"}}).status_code == 422
        assert c.put("/api/profile", json={"race_date": "28.11.2026"}).status_code == 422
