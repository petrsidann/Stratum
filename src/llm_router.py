"""Stratum AI Brain — LLM router (Phase 2).

NON-NEGOTIABLE RULE: the LLM is ONLY an extractor/reader. It never computes
odds, probabilities, Kelly, or any arithmetic. All math lives in
``src/quant_engine.py``. If the model output contains a numeric value that is
not literally present in the source text, we treat it as invalid (hallucinated)
and null it out before returning.

Routing: Groq (llama-3.3-70b-versatile) primary -> Google Gemini Flash
(gemini-1.5-flash) fallback. If both fail, raise ``BrainUnavailableError`` so
the caller degrades to manual entry. Never fabricate data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import config

logger = logging.getLogger("stratum.brain")

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-1.5-flash"
TEMPERATURE = 0.1

NUMERIC_FIELDS = (
    "spread_home",
    "spread_away",
    "ml_home_american",
    "ml_away_american",
    "total_line",
    "public_ticket_pct_home",
)
VALID_SHARP_SIGNALS = ("none", "steam", "rlm", "stale")

EXTRACTION_SYSTEM_PROMPT = """You are Stratum's market-context EXTRACTOR.

Your ONLY job is to read the provided raw text about a sports betting market
and copy across values that appear VERBATIM in that text. You are NOT an
analyst: you must NEVER compute odds, probabilities, payouts, Kelly stakes,
averages, or ANY arithmetic, and you must NEVER guess or invent a number.

Respond with STRICT JSON only (no markdown fences, no commentary) with exactly
these keys:
  "spread_home": number or null
  "spread_away": number or null
  "ml_home_american": integer American odds (e.g. -150, +130) or null
  "ml_away_american": integer American odds or null
  "total_line": number or null
  "public_ticket_pct_home": number 0-100 or null
  "sharp_signal": one of "none" | "steam" | "rlm" | "stale"
  "injury_notes": string or null
  "source_quotes": list of the EXACT sentences the numbers came from (for audit)

Rules:
- If a value is not present in the text, use null. Do not guess.
- Every non-null numeric field MUST be copied verbatim from the text; if you
  had to calculate it in any way, output null instead.
- If nothing at all is present, return all-null with sharp_signal "none" and an
  empty source_quotes list."""


class BrainUnavailableError(RuntimeError):
    """Raised when every configured LLM backend failed; caller uses manual entry."""


def _numbers_in_text(text: str) -> set[str]:
    """All numeric literals appearing in the source text, normalized.

    '-150', '+130', '130', '45.5', '55%' all normalize to {'150','130','45.5','55'}.
    Used to reject any number the model produced that we did not fetch.
    """
    found: set[str] = set()
    for tok in re.findall(r"[+-]?\d+(?:\.\d+)?", text or ""):
        found.add(tok.lstrip("+"))
    return found


def _is_plausible_number(value: Any, allowed: set[str]) -> bool:
    """True iff value is a real number whose literal appears in the source text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    s = repr(float(value))
    if s.endswith(".0"):
        s = s[:-2]
    return s.lstrip("+") in allowed


def sanitize_extraction(parsed: dict, raw_text: str) -> dict:
    """Enforce Rule 1: drop any numeric field not literally present in the text.

    The LLM is an extractor only — a number it computed/hallucinated is invalid
    and becomes None, exactly like a missing value.
    """
    if not isinstance(parsed, dict):
        raise ValueError("LLM extraction payload is not a JSON object")
    allowed = _numbers_in_text(raw_text)
    clean: dict[str, Any] = {}
    for key in NUMERIC_FIELDS:
        val = parsed.get(key)
        clean[key] = val if _is_plausible_number(val, allowed) else None
    signal = parsed.get("sharp_signal")
    clean["sharp_signal"] = signal if signal in VALID_SHARP_SIGNALS else "none"
    notes = parsed.get("injury_notes")
    clean["injury_notes"] = notes.strip() if isinstance(notes, str) and notes.strip() else None
    quotes = parsed.get("source_quotes")
    if isinstance(quotes, list):
        clean["source_quotes"] = [q for q in quotes if isinstance(q, str) and q.strip()]
    else:
        clean["source_quotes"] = []
    return clean


class StratumBrain:
    """Free-tier LLM router: Groq primary, Gemini Flash fallback. Extractor only."""

    def __init__(
        self,
        groq_api_key: Optional[str] = None,
        google_api_key: Optional[str] = None,
    ) -> None:
        self._groq_key = groq_api_key if groq_api_key is not None else config.GROQ_API_KEY
        self._google_key = google_api_key if google_api_key is not None else config.GOOGLE_API_KEY

    # -- configuration ------------------------------------------------------
    def is_configured(self) -> bool:
        """True if at least one backend key exists."""
        return bool(self._groq_key or self._google_key)

    # -- public API ---------------------------------------------------------
    def extract_market_context(self, raw_text: str, sport: str) -> dict:
        """Extract structured market context from scraped/news text.

        NEVER performs arithmetic itself; all returned numbers are validated
        against the source text. Raises BrainUnavailableError if both backends
        fail — the caller must then fall back to manual entry.
        """
        if not raw_text or not raw_text.strip():
            raise BrainUnavailableError("No source text provided to extract from.")
        user_prompt = (
            f"SPORT: {sport}\n\nRAW TEXT:\n\"\"\"\n{raw_text[:8000]}\n\"\"\"\n\n"
            "Return the STRICT JSON object now. Remember: extract verbatim or null."
        )

        last_error: Optional[Exception] = None
        for attempt in (self._groq, self._gemini):
            try:
                payload = attempt(user_prompt)
                parsed = payload if isinstance(payload, dict) else json.loads(payload)
                if not isinstance(parsed, dict):
                    raise ValueError("extraction payload was not a JSON object")
                return sanitize_extraction(parsed, raw_text)
            except Exception as exc:  # noqa: BLE001 - any failure triggers fallback
                last_error = exc
                logger.warning("brain backend %s failed: %s", attempt.__name__, exc)

        raise BrainUnavailableError(f"All LLM backends failed. Last error: {last_error}") from last_error

    # -- private backends ---------------------------------------------------
    def _groq(self, prompt: str) -> Any:
        """Primary: Groq llama-3.3-70b-versatile, temp 0.1, JSON response format."""
        if not self._groq_key:
            raise RuntimeError("Groq API key not configured.")
        from groq import Groq  # lazy import so tests pass without the package

        client = Groq(api_key=self._groq_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return json.loads(resp.choices[0].message.content)

    def _gemini(self, prompt: str) -> Any:
        """Fallback: Google Gemini 1.5 Flash with application/json mime type."""
        if not self._google_key:
            raise RuntimeError("Google API key not configured.")
        from google import genai  # lazy import so tests pass without the package
        from google.genai import types

        client = genai.Client(api_key=self._google_key)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{EXTRACTION_SYSTEM_PROMPT}\n\n{prompt}",
            config=types.GenerateContentConfig(
                temperature=TEMPERATURE,
                response_mime_type="application/json",
            ),
        )
        return json.loads(resp.text)
