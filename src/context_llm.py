#!/usr/bin/env python3
"""Stratum QUANT BRAIN — contextual adjuster (ONE guarded LLM call per fixture).

The math model (src/model_poisson.py) prices the match; this module lets a
language model nudge the *inputs* of that model (expected goals, corner and
card rates) using qualitative context it can read but the Poisson engine
cannot: recent form strings, H2H notes, rest days, and venue weather from the
keyless Open-Meteo forecast API.

Hard contract (anti-hallucination firewall):
  * The LLM may NEVER output odds or probabilities. It only outputs deltas on
    rate parameters, within a strict JSON schema:
      {"adjustments":[{"target":"lambda_home|lambda_away|corner_rate_home|
                       corner_rate_away|card_rate","delta":float,"reason":str}],
       "confidence":0..1}
  * At most 5 adjustments; every delta clamped to +/-0.25.
  * Exactly ONE call per fixture, with provider fallback Groq -> Gemini ->
    offline. On ANY failure we return empty adjustments + confidence 0, i.e.
    pure math-only mode. Nothing downstream ever trusts an unparseable reply.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

ALLOWED_TARGETS = ("lambda_home", "lambda_away", "corner_rate_home",
                   "corner_rate_away", "card_rate")
MAX_ADJUSTMENTS = 5
DELTA_CLAMP = 0.25
TIMEOUT_S = 25

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-1.5-flash:generateContent")
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

SYSTEM_PROMPT = (
    "You are a football modelling analyst for a Poisson goal-expectancy engine. "
    "Given a compact fact pack about one fixture, you may ONLY propose small "
    "multiplicative-rate adjustments to these five numeric inputs: "
    "lambda_home, lambda_away, corner_rate_home, corner_rate_away, card_rate. "
    "You MUST NOT output odds, prices, probabilities, picks, or stakes - those "
    "are computed elsewhere from your deltas. Reply with STRICT JSON only, no "
    "markdown fences, matching exactly: "
    '{"adjustments":[{"target":"lambda_home","delta":0.0,"reason":"short text"}],' 
    '"confidence":0.0}. '
    "Rules: at most 5 adjustments; each delta between -0.25 and 0.25; confidence "
    "between 0 and 1; if the facts do not justify any change, return an empty "
    "adjustments list. Consider: injuries/suspensions implied by form, "
    "schedule fatigue (rest days), H2H tendencies, and weather (heavy rain or "
    "wind suppresses scoring and corners slightly; extreme heat fatigues "
    "late-game)."
)


def _post_json(url, payload, headers, timeout=TIMEOUT_S):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# --------------------------------------------------------------------------
# Fact pack + weather
# --------------------------------------------------------------------------

def venue_weather(lat, lon):
    """Keyless Open-Meteo current+daily summary for venue coords. Never raises."""
    if lat is None or lon is None:
        return None
    try:
        url = (f"{OPEN_METEO}?latitude={round(float(lat), 4)}&longitude={round(float(lon), 4)}"
               "&current=temperature_2m,precipitation,wind_speed_10m,weather_code"
               "&daily=sunshine_duration,precipitation_sum&forecast_days=1&timezone=auto")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0",
                                                   "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read().decode())
        cur = j.get("current") or {}
        units = j.get("current_units") or {}
        code_map = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
                    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle",
                    61: "light rain", 63: "rain", 65: "heavy rain",
                    71: "light snow", 73: "snow", 80: "rain showers",
                    81: "rain showers", 82: "violent showers", 95: "thunderstorm"}
        return {
            "temp_c": cur.get("temperature_2m"),
            "precip_mm": cur.get("precipitation"),
            "wind_kph": cur.get("wind_speed_10m"),
            "conditions": code_map.get(int(cur.get("weather_code", -1)), "unknown"),
        }
    except Exception:  # noqa: BLE001
        return None


def build_fact_pack(fixture, meta):
    """Compact plain-text fact pack fed to the single LLM call."""
    fit = meta.get("fit", {})
    lines = [
        f"FIXTURE: {fixture.get('home')} (home) vs {fixture.get('away')} (away)",
        f"LEAGUE: {fixture.get('league')} | KICKOFF: {fixture.get('kickoff_utc')}",
        f"MODEL BASELINE: lambda_home={meta.get('lambda_home')} "
        f"lambda_away={meta.get('lambda_away')} rho={meta.get('rho', 0.05)}",
        f"ATTACK/DEFENSE INDEX: home att={fit.get('attack_home')} def={fit.get('defense_home')}; "
        f"away att={fit.get('attack_away')} def={fit.get('defense_away')}",
        f"SAMPLE: home {fit.get('sample_home', 0)} games, away {fit.get('sample_away', 0)} games "
        f"(last {min(20, max(fit.get('sample_home', 0), fit.get('sample_away', 0)) or 0)} completed)",
        f"REST DAYS: home={meta.get('rest_days_home', 'unknown')} "
        f"away={meta.get('rest_days_away', 'unknown')}",
        f"H2H last meetings (dates both played): {meta.get('h2h_dates') or 'none found keyless'}",
    ]
    form = fixture.get("form_strings") or {}
    if form:
        lines.append(f"FORM home: {form.get('home', 'n/a')} | away: {form.get('away', 'n/a')}")
    w = fixture.get("venue_weather")
    if isinstance(w, dict):
        lines.append(f"VENUE WEATHER: {w.get('conditions')}, {w.get('temp_c')}C, "
                     f"wind {w.get('wind_kph')} kph, precip {w.get('precip_mm')} mm")
    else:
        lines.append("VENUE WEATHER: unavailable (no coords)")
    cr = meta.get("corner_rates") or {}
    ka = meta.get("card_rates") or {}
    lines.append(f"SET PIECES: corners/game home={cr.get('home')} away={cr.get('away')}; "
                 f"cards/game home={ka.get('home')} away={ka.get('away')}")
    return "\n".join(lines)[:1800]


# --------------------------------------------------------------------------
# Strict parsing / validation
# --------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text):
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def validate_response(obj):
    """Enforce the output contract; returns (adjustments, confidence)."""
    if not isinstance(obj, dict):
        return [], 0.0
    raw = obj.get("adjustments")
    conf = obj.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    out = []
    if isinstance(raw, list):
        for a in raw[:MAX_ADJUSTMENTS]:
            if not isinstance(a, dict):
                continue
            target = str(a.get("target", "")).strip()
            if target not in ALLOWED_TARGETS:
                continue
            try:
                delta = float(a.get("delta"))
            except (TypeError, ValueError):
                continue
            if not (-DELTA_CLAMP <= delta <= DELTA_CLAMP):
                delta = max(-DELTA_CLAMP, min(DELTA_CLAMP, delta))
            reason = str(a.get("reason", ""))[:160]
            # Firewall: reject anything that smells like odds/probability leakage.
            low = reason.lower()
            if re.search(r"\bodds?\b|\bprice[sd]?\b|\bprobabilit|\bpayout|\bimplied", low):
                continue
            out.append({"target": target, "delta": round(delta, 4), "reason": reason})
    return out, conf


# --------------------------------------------------------------------------
# Providers (one call total; Groq -> Gemini -> offline)
# --------------------------------------------------------------------------

def _ask_groq(prompt):
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_TOKEN")
    if not key:
        raise RuntimeError("no_groq_key")
    j = _post_json(GROQ_URL, {
        "model": os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
        "temperature": 0.1,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }, {"Authorization": f"Bearer {key}"})
    return j["choices"][0]["message"]["content"]


def _ask_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("no_gemini_key")
    j = _post_json(f"{GEMINI_URL}?key={key}", {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400,
                             "responseMimeType": "application/json"},
    }, {})
    return j["candidates"][0]["content"]["parts"][0]["text"]


def get_context_adjustments(fixture, meta, transport=None):
    """ONE guarded call per fixture. Returns dict:
       {adjustments:[...], confidence:float, provider:'groq|gemini|offline'}
    Any failure -> empty adjustments, confidence 0 (math-only mode).
    `transport` allows tests to inject a fake LLM transport.
    """
    fact_pack = build_fact_pack(fixture, meta)
    prompt = f"FACT PACK:\n{fact_pack}\nReturn STRICT JSON now."
    providers = [("groq", _ask_groq), ("gemini", _ask_gemini)]
    if transport is not None:
        providers = [("mock", transport)]
    for name, fn in providers:
        try:
            text = fn(prompt)
            obj = _extract_json(text)
            if obj is None:
                continue
            adjustments, conf = validate_response(obj)
            if conf <= 0 and not adjustments:
                continue
            return {"adjustments": adjustments, "confidence": round(conf, 3),
                    "provider": name}
        except Exception:  # noqa: BLE001 - fall through to next provider
            continue
    return {"adjustments": [], "confidence": 0.0, "provider": "offline"}


# --------------------------------------------------------------------------
# Applying adjustments back into the model inputs
# --------------------------------------------------------------------------

def apply_adjustments(meta, adjustments):
    """Multiply base rates by (1 + delta) for each accepted adjustment.

    Returns a new meta dict with adjusted lambdas/rates plus the audit trail.
    Lambdas stay clamped inside the model's safe range.
    """
    out = dict(meta)
    lh = float(out.get("lambda_home", 1.4))
    la = float(out.get("lambda_away", 1.1))
    cr = dict(out.get("corner_rates") or {})
    ka = dict(out.get("card_rates") or {})
    applied = []
    for a in adjustments or []:
        t, d = a.get("target"), float(a.get("delta", 0.0))
        d = max(-DELTA_CLAMP, min(DELTA_CLAMP, d))
        if t == "lambda_home":
            lh = max(0.15, min(4.0, lh * (1.0 + d)))
        elif t == "lambda_away":
            la = max(0.15, min(4.0, la * (1.0 + d)))
        elif t == "corner_rate_home":
            cr["home"] = max(0.5, float(cr.get("home", 5.0)) * (1.0 + d))
        elif t == "corner_rate_away":
            cr["away"] = max(0.5, float(cr.get("away", 4.5)) * (1.0 + d))
        elif t == "card_rate":
            ka["home"] = max(0.3, float(ka.get("home", 2.0)) * (1.0 + d))
            ka["away"] = max(0.3, float(ka.get("away", 2.5)) * (1.0 + d))
        else:
            continue
        applied.append(a)
    out["lambda_home"] = round(lh, 3)
    out["lambda_away"] = round(la, 3)
    out["corner_rates"] = cr
    out["card_rates"] = ka
    out["context_applied"] = applied
    return out


if __name__ == "__main__":  # offline demo
    fx = {"home": "Arsenal", "away": "Liverpool", "league": "eng.1",
          "kickoff_utc": "2026-09-30T19:00Z"}
    meta = {"lambda_home": 1.5, "lambda_away": 1.2, "fit": {},
            "corner_rates": {"home": 5.6, "away": 4.6},
            "card_rates": {"home": 2.0, "away": 2.3}}
    res = get_context_adjustments(fx, meta)
    print(json.dumps(res, indent=1))
