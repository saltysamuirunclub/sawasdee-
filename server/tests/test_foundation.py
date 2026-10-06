from fastapi.testclient import TestClient

from app import db, i18n
from app.main import app


def test_migrations_create_tables_and_are_idempotent():
    conn = db.connect(":memory:")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert db.migrate(conn) == len(db.MIGRATIONS)  # second run is a no-op
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "strava_tokens", "activities", "coach_messages", "push_subscriptions"} <= tables


def test_user_defaults_to_english():
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute("INSERT INTO users (strava_athlete_id, name) VALUES (1, 'Test')")
    assert conn.execute("SELECT language FROM users").fetchone()[0] == "en"


def test_translation_with_fallbacks():
    assert i18n.t("nav.runs", "de") == "Läufe"
    assert i18n.t("home.days_to_race", "en", days=53) == "53 days to race day"
    assert i18n.t("nav.runs", "xx") == "Runs"           # unknown language -> English
    assert i18n.t("does.not.exist", "de") == "does.not.exist"


def test_all_locales_have_the_same_keys():
    en = set(i18n._catalog("en"))
    for lang in i18n.supported_languages():
        assert set(i18n._catalog(lang)) == en, lang


def test_healthz():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
