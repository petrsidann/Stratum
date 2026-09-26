"""Stratum configuration — loads environment variables safely.

All secrets live in a local `.env` file (gitignored). Every accessor returns
a safe default instead of raising when a variable is missing, so the app can
boot in degraded mode without crashing.
"""

from __future__ import annotations

import os

try:
    # Load .env if python-dotenv is available; never crash if it isn't.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass


def get_env(key: str, default: str = "") -> str:
    """Return an environment variable, or a safe default if unset/empty."""
    value = os.getenv(key)
    return value if value not in (None, "") else default


# --- Core settings ---------------------------------------------------------
GROQ_API_KEY: str = get_env("GROQ_API_KEY")
GOOGLE_API_KEY: str = get_env("GOOGLE_API_KEY")
DATABASE_PATH: str = get_env("STRATUM_DB", "stratum.db")
APP_ENV: str = get_env("APP_ENV", "development")
