"""Stratum Contextual Reasoning Engine (Phase 5) — The Analyst.

The quant layer finds a deviation; this layer explains it. Given a detected
edge (e.g. "Luka Doncic Points Over 30.5 @ +110") plus raw news text, we ask
the LLM (Groq primary -> Gemini fallback, same router policy as Phase 2) to
correlate the price gap with injuries, pace, matchup defense rank and rest
days, and return a concise two-sentence professional insight for the UI card.

Non-negotiable constraints:
  * Output is PLAIN PROSE ONLY — no markdown, no emojis, no hype. A
    Bloomberg-terminal tone is enforced by the system prompt AND by output
    sanitization (anything that slips through gets stripped).
  * The LLM never computes odds/EV here; signal_data numbers are injected
    read-only for context. This module adds zero arithmetic of its own.
  * Any failure (no keys, timeout, bad JSON, empty answer) degrades to the
    deterministic FALLBACK_INSIGHT template. An explanation is optional;
    a wrong one is not.
  * Privacy: user-identifying content is SHA-256 hashed before leaving the
    process; only market facts (public prices/lines/news) hit the provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Dict, Optional

import config

logger = logging.getLogger("stratum.reasoning")

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-1.5-flash"
TEMPERATURE = 0.2
MAX_TOKENS = 180
LLM_TIMEOUT_SECONDS = 12

FALLBACK_INSIGHT = "Edge detected based on quantitative deviation from market mean."

INSIGHT_SYSTEM_PROMPT = """You are Stratum's market-context ANALYST.

You will receive a detected pricing edge in a sports betting market and raw
news text around that game. Explain WHY the line may be soft. Consider, where
the evidence supports it: injuries and role changes (usage redistribution),
pace/tempo mismatches, opponent defensive rank against the position, rest
days and schedule spots, and market timing (line posted before news).

Hard rules:
- Return STRICT JSON only: {"insight": "..."} - no markdown, no code fences.
- Exactly 1-2 sentences, under 240 characters total.
- Professional, neutral, analyst-desk tone. No exclamation marks. No calls
  to action ("bet now", "value play"). No emojis. No hedging filler.
- Do NOT compute or restate odds, probabilities or EV figures yourself. Use
  only the facts given. If the news does not explain the discrepancy, say so
  plainly in one sentence instead of inventing a narrative.
- Never name a source URL; describe the fact, not the link."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_insight(text: Optional[str]) -> str:
    """Force any model output into clean UI prose (or fall back)."""
    if not text or not str(text).strip():
        return FALLBACK_INSIGHT
    out = str(text)
    out = re.sub(r"```[a-zA-Z]*", "", out)                 # code fences
    out = re.sub(r"\[[^\]]*\]\([^)]*\)", "", out)          # markdown links
    out = re.sub(r"[*_`#>|]", "", out)                     # md glyphs
    out = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", out)  # emoji/dingbats
    out = re.sub(r"\s+", " ", out).strip()
    out = out.replace("!", ".")                            # no exclamation marks
    # Strip hype CTAs if the model ignored instructions.
    out = re.sub(r"\b(bet now|money (move|shot)|lock(ed)? in|easy money|guaranteed)\b[^.]*\.?",
                 "", out, flags=re.IGNORECASE).strip()
    if len(out) > 320:
        cut = out[:320]
        out = cut[: max(cut.rfind("."), cut.rfind("!")) + 1] if "." in cut else cut.rstrip() + "."
    return out if out else FALLBACK_INSIGHT


def _signal_brief(signal_data: Dict) -> str:
    """Render the edge as inert facts for the prompt (no math added)."""
    parts = []
    for key in ("market", "selection", "offered_odds", "fair_odds", "edge_pct",
                "line", "bookmaker", "match", "sport"):
        val = signal_data.get(key)
        if val is not None and str(val).strip():
            parts.append(f"{key}={val}")
    return " | ".join(parts) if parts else json.dumps(signal_data, default=str)[:400]


def hash_private(value: str) -> str:
    """SHA-256 short digest — anything user-specific goes to providers only
    in hashed form (the raw value never leaves the process)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def build_user_prompt(signal_data: Dict, context_text: str, user_ref: Optional[str] = None) -> str:
    ctx = (context_text or "").strip()[:4000] or "(no news text supplied)"
    privacy = f"user_ref={hash_private(user_ref)}" if user_ref else "user_ref=none"
    return (
        f"DETECTED EDGE: {_signal_brief(signal_data)}\n"
        f"{privacy}\n\nRAW NEWS TEXT:\n\"\"\"\n{ctx}\n\"\"\"\n\n"
        'Return {"insight": "..."} now.'
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_insight(
    signal_data: Dict,
    context_text: str,
    groq_api_key: Optional[str] = None,
    google_api_key: Optional[str] = None,
    timeout: float = LLM_TIMEOUT_SECONDS,
) -> str:
    """Explain one detected edge in 1-2 professional sentences.

    Groq primary -> Gemini fallback -> deterministic template. NEVER raises:
    every failure path returns ``FALLBACK_INSIGHT`` so the UI always has a
    safe string to render.
    """
    if not isinstance(signal_data, dict):
        return FALLBACK_INSIGHT
    groq_key = groq_api_key if groq_api_key is not None else config.GROQ_API_KEY
    google_key = google_api_key if google_api_key is not None else config.GOOGLE_API_KEY
    if not (groq_key or google_key):
        logger.info("reasoning: no LLM keys configured — using template fallback")
        return FALLBACK_INSIGHT

    prompt = build_user_prompt(signal_data, context_text)
    backends = (
        ("groq", _call_groq, groq_key),
        ("gemini", _call_gemini, google_key),
    )
    last_err: Optional[Exception] = None
    for name, backend, key in backends:
        try:
            raw = backend(prompt, key, timeout)
            if raw:
                parsed = _extract_insight(raw)
                cleaned = sanitize_insight(parsed)
                if cleaned:
                    return cleaned
        except Exception as exc:  # noqa: BLE001 - any failure -> next/fallback
            last_err = exc
            logger.warning("reasoning backend %s failed: %s", name, exc)
    if last_err is not None:
        logger.info("reasoning degraded to fallback after errors: %s", last_err)
    return FALLBACK_INSIGHT


def _extract_insight(raw: str) -> Optional[str]:
    """Accept strict JSON {"insight": ...}; tolerate plain-text answers."""
    text = raw.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            val = obj.get("insight") or obj.get("analysis") or obj.get("text")
            return val if isinstance(val, str) else None
    except ValueError:
        pass
    m = re.search(r'\{\s*"insight"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}', text)
    if m:
        return m.group(1).encode().decode("unicode_escape", errors="ignore")
    # Plain prose answer (some models ignore JSON instruction) — usable as-is.
    if 20 <= len(text) <= 600:
        return text
    return None


def _call_groq(prompt: str, api_key: str, timeout: float) -> Optional[str]:
    if not api_key:
        raise RuntimeError("Groq API key not configured.")
    from groq import Groq  # lazy import keeps tests dependency-free

    client = Groq(api_key=api_key, timeout=timeout, max_retries=0)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content


def _call_gemini(prompt: str, api_key: str, timeout: float) -> Optional[str]:
    if not api_key:
        raise RuntimeError("Google API key not configured.")
    from google import genai  # lazy import
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{INSIGHT_SYSTEM_PROMPT}\n\n{prompt}",
        config=types.GenerateContentConfig(temperature=TEMPERATURE,
                                           max_output_tokens=MAX_TOKENS),
    )
    return resp.text
