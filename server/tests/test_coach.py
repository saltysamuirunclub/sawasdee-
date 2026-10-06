import json
from types import SimpleNamespace

import pytest

from app import coach, db, pipeline, push, strava
from tests.test_strava import RUN


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


ANSWER = """**Summary:** Solid tempo, controlled pacing — easy day next.
**Evaluation:** Nice even splits.
**Next session:** Thu · easy · 8 km · 5:45–6:15 /km
**Tip:** Drink early."""


class FakeMessages:
    def __init__(self, text=ANSWER, stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error, self.calls = text, stop_reason, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)], stop_reason=self.stop_reason,
            model=kwargs["model"], usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            _request_id="req_test")


@pytest.fixture
def fake_claude(monkeypatch):
    messages = FakeMessages()
    monkeypatch.setattr(coach, "client", lambda: SimpleNamespace(messages=messages))
    return messages


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "send_to_user", lambda conn, uid, title, body, url="/": sent.append((title, body, url)) or 1)
    return sent


def setup_member(conn, language="en"):
    user_id = strava.upsert_user(conn, {"id": 42, "firstname": "Juu"})
    with conn:
        conn.execute("""UPDATE users SET coach_enabled=1, language=?, race_date='2026-11-28',
                        goal_time_s=12000, weekly_notes='Slept badly' WHERE id=?""", (language, user_id))
    strava.store_activity(conn, user_id, {**RUN, "id": 1, "start_date": "2026-09-20T23:00:00Z",
                                          "start_date_local": "2026-09-21T06:00:00Z", "name": "Long Sunday",
                                          "distance": 24000})
    strava.store_activity(conn, user_id, {**RUN, "description": "legs heavy"})
    return user_id


def test_prompt_contains_everything(conn, fake_claude):
    user_id = setup_member(conn, "de")
    reply = coach.run_feedback(conn, user_id, 1001)
    call = fake_claude.calls[0]
    prompt = call["messages"][0]["content"]
    assert call["system"].startswith("# Coach instructions")
    assert call["model"] == coach.settings.claude_model
    for expected in ("Morning Tempo", "legs heavy", "Splits per km", "Long Sunday", "Days to race",
                     "marathon pace 4:44", "Slept badly", "Answer in Deutsch"):
        assert expected in prompt, expected
    assert reply.summary == "Solid tempo, controlled pacing — easy day next."


def test_pipeline_success_saves_and_pushes(conn, fake_claude, pushes):
    user_id = setup_member(conn)
    pipeline.on_new_run(conn, user_id, 1001)
    msg = conn.execute("SELECT * FROM coach_messages").fetchone()
    assert msg["kind"] == "run" and msg["activity_id"] == 1001 and msg["summary"].startswith("Solid")
    assert conn.execute("SELECT coach_status FROM activities WHERE id=1001").fetchone()[0] == "done"
    assert pushes == [("🏃 12.0 km · 4:35 /km", msg["summary"], "/runs/1001")]


def test_pipeline_failure_marks_failed_and_notifies(conn, monkeypatch, pushes):
    user_id = setup_member(conn)
    fake = FakeMessages(error=coach.anthropic.APIConnectionError(request=None))
    monkeypatch.setattr(coach, "client", lambda: SimpleNamespace(messages=fake))
    pipeline.on_new_run(conn, user_id, 1001)
    row = conn.execute("SELECT coach_status, coach_error FROM activities WHERE id=1001").fetchone()
    assert row["coach_status"] == "failed" and "reach" in row["coach_error"]
    assert "failed" in pushes[0][1]

    monkeypatch.setattr(coach, "client", lambda: SimpleNamespace(messages=FakeMessages()))
    assert pipeline.retry_failed(conn) == 1
    assert conn.execute("SELECT coach_status FROM activities WHERE id=1001").fetchone()[0] == "done"


def test_refusal_and_empty_answers_are_errors(conn, monkeypatch):
    user_id = setup_member(conn)
    for fake in (FakeMessages(stop_reason="refusal"), FakeMessages(text="")):
        monkeypatch.setattr(coach, "client", lambda f=fake: SimpleNamespace(messages=f))
        with pytest.raises(coach.CoachError):
            coach.run_feedback(conn, user_id, 1001)


def test_summary_fallback_and_truncation():
    assert coach.extract_summary("Great run!\nMore text") == "Great run!"
    assert len(coach.extract_summary("**Summary:** " + "x" * 300)) == 118


def test_weekly_plan_includes_previous_plan(conn, fake_claude):
    user_id = setup_member(conn)
    with conn:
        conn.execute("""INSERT INTO coach_messages (user_id, kind, language, content, created_at)
                        VALUES (?, 'weekly', 'en', 'OLD PLAN', '2026-01-01')""", (user_id,))
    coach.weekly_plan(conn, user_id)
    assert "OLD PLAN" in fake_claude.calls[0]["messages"][0]["content"]


def test_push_removes_expired_subscriptions(conn, monkeypatch):
    user_id = setup_member(conn)
    push.save_subscription(conn, user_id, {"endpoint": "https://web.push.apple.com/a",
                                           "keys": {"p256dh": "k", "auth": "a"}})
    push.save_subscription(conn, user_id, {"endpoint": "https://web.push.apple.com/b",
                                           "keys": {"p256dh": "k", "auth": "a"}})
    monkeypatch.setattr(push, "settings", push.settings.__class__(
        **{**push.settings.__dict__, "vapid_private_key": "x"}))

    def fake_webpush(subscription_info, **kw):
        assert json.loads(kw["data"])["title"] == "T"
        if subscription_info["endpoint"].endswith("/a"):
            raise push.WebPushException("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr(push, "webpush", fake_webpush)
    assert push.send_to_user(conn, user_id, "T", "B") == 1
    left = [r[0] for r in conn.execute("SELECT endpoint FROM push_subscriptions")]
    assert left == ["https://web.push.apple.com/b"]


def test_invalid_subscription_rejected(conn):
    user_id = setup_member(conn)
    with pytest.raises(ValueError):
        push.save_subscription(conn, user_id, {"endpoint": "http://evil", "keys": {"p256dh": "k", "auth": "a"}})
