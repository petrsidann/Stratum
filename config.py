"""Stratum configuration — loads environment variables / secrets safely.

Secret resolution order (first non-empty wins):
  1. ``st.secrets``  — Streamlit Cloud's Secrets manager (.streamlit/secrets.toml)
  2. ``os.environ``  — plain env vars / local .env via python-dotenv

Every accessor returns a safe default instead of raising when a variable is
missing, so the app can boot in degraded mode without crashing — even with
zero keys and zero network on share.streamlit.io.
"""

from __future__ import annotations

import os

try:
    # Load .env if python-dotenv is available; never crash if it isn't.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional dependency
    pass


def _streamlit_secret(key: str) -> str:
    """Read one key from Streamlit's secrets store, or "" if unavailable.

    Wrapped in a bare try/except because outside a Streamlit runtime (plain
    pytest, `python main.py`) st.secrets raises FileNotFound/KeyError — that
    simply means "no secrets here", which is a valid, non-fatal state.
    """
    try:
        import streamlit as st

        value = st.secrets.get(key)  # type: ignore[attr-defined]
        return str(value) if value not in (None, "") else ""
    except Exception:
        return ""


def get_env(key: str, default: str = "") -> str:
    """Return a secret: Streamlit secrets first, then os.environ, then default."""
    value = _streamlit_secret(key) or os.getenv(key)
    return value if value not in (None, "") else default


# --- Core settings ---------------------------------------------------------
GROQ_API_KEY: str = get_env("GROQ_API_KEY")
GOOGLE_API_KEY: str = get_env("GOOGLE_API_KEY")
DATABASE_PATH: str = get_env("STRATUM_DB", "stratum.db")
APP_ENV: str = get_env("APP_ENV", "development")
