"""Settings loaded from environment variables (and an optional .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    base_url: str
    secret_key: str
    strava_client_id: str
    strava_client_secret: str
    strava_verify_token: str
    admin_strava_id: int | None
    anthropic_api_key: str
    claude_model: str
    vapid_public_key: str
    vapid_private_key: str
    vapid_contact: str
    database_path: Path
    log_dir: Path
    timezone: str


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    env = os.environ.get
    admin = env("ADMIN_STRAVA_ID", "").strip()
    return Settings(
        base_url=env("BASE_URL", "http://localhost:8000").rstrip("/"),
        secret_key=env("SECRET_KEY", "dev-insecure-key"),
        strava_client_id=env("STRAVA_CLIENT_ID", ""),
        strava_client_secret=env("STRAVA_CLIENT_SECRET", ""),
        strava_verify_token=env("STRAVA_VERIFY_TOKEN", ""),
        admin_strava_id=int(admin) if admin.isdigit() else None,
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        claude_model=env("CLAUDE_MODEL", "claude-haiku-4-5"),
        vapid_public_key=env("VAPID_PUBLIC_KEY", ""),
        vapid_private_key=env("VAPID_PRIVATE_KEY", ""),
        vapid_contact=env("VAPID_CONTACT", "mailto:admin@example.com"),
        database_path=_path(env("DATABASE_PATH", "data/club.db")),
        log_dir=_path(env("LOG_DIR", "logs")),
        timezone=env("TIMEZONE", "Asia/Bangkok"),
    )


settings = load_settings()
