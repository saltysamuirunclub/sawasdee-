"""Minimal translations: one JSON file per language in locales/."""
import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "en"

# Shown in the language picker; the coach is told to answer in this language.
LANGUAGE_NAMES = {"en": "English", "de": "Deutsch"}


@lru_cache
def _catalog(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def supported_languages() -> list[str]:
    return [lang for lang in LANGUAGE_NAMES if (LOCALES_DIR / f"{lang}.json").exists()]


def normalize_language(lang: str | None) -> str:
    return lang if lang in supported_languages() else DEFAULT_LANGUAGE


def t(key: str, lang: str = DEFAULT_LANGUAGE, **params) -> str:
    """Translate key, falling back to English, then to the key itself."""
    text = _catalog(normalize_language(lang)).get(key) or _catalog(DEFAULT_LANGUAGE).get(key, key)
    return text.format(**params) if params else text
