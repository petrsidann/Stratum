"""Stratum free data layer (Phase 2) — Open-Meteo weather + polite DDG scraping.

Every public function is wrapped in try/except and returns None / "" on any
failure, logging a WARNING instead of crashing the UI. Callers must degrade to
manual entry when data cannot be fetched. We NEVER fabricate numbers:
"Unknown" is a valid state; a made-up price is not.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("stratum.scraper")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 StratumResearch/0.1"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
REQUEST_TIMEOUT = 10
POLITE_SLEEP_SECONDS = 1.0  # be respectful: pause between network hits

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DDG_URL = "https://html.duckduckgo.com/html/"

# WMO weather interpretation codes -> human condition names.
_WMO_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Severe thunderstorm",
}

# American odds token: optional sign, 100..4 digits (avoids matching plain years).
# Lookahead allows a sentence-final period but not decimals/digits ("+130." ok, "-3.5" not).
_AMERICAN_RE = re.compile(r"(?<![\d.])([+-]\d{3,4})(?!\d|\.\d)")
_SPREAD_RE = re.compile(
    r"(?:spread|line)[^+\-\d]{0,20}([+-]?\d{1,2}(?:\.5)?(?:\s*[/-]\s*\d{1,2}(?:\.5)?)?)",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(
    r"(?:total|over/under|o/u)[^+\-\d]{0,15}(\d{1,3}(?:\.5)?)",
    re.IGNORECASE,
)


def fetch_weather(lat: float, lon: float) -> Optional[dict]:
    """Current conditions via Open-Meteo (free, no API key).

    Returns {temp_c, wind_kmh, precip_mm, condition} or None on any failure.
    """
    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,wind_speed_10m,precipitation,weather_code",
                "timezone": "auto",
            },
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        current = resp.json().get("current") or {}
        temp = current.get("temperature_2m")
        if temp is None:
            raise ValueError("missing temperature_2m in payload")
        code = current.get("weather_code")
        return {
            "temp_c": float(temp),
            "wind_kmh": float(current.get("wind_speed_10m", 0) or 0),
            "precip_mm": float(current.get("precipitation", 0) or 0),
            "condition": _WMO_CONDITIONS.get(int(code), "Unknown") if code is not None else "Unknown",
        }
    except Exception as exc:  # noqa: BLE001 - never crash the UI
        logger.warning("fetch_weather failed for (%s, %s): %s", lat, lon, exc)
        return None


def fetch_match_context(query: str) -> str:
    """Polite DuckDuckGo HTML search; returns top-3 result snippets as text.

    Returns "" when nothing usable is found (caller degrades to manual entry).
    """
    try:
        if not query or not query.strip():
            return ""
        search_query = f"{query} odds line movement sharp money injury"
        resp = requests.post(
            DDG_URL,
            data={"q": search_query},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        snippets = []
        for result in soup.select(".result, .web-result")[:3]:
            title_el = result.select_one(".result__title, h2")
            snip_el = result.select_one(".result__snippet, .result__excerpt")
            parts = [
                el.get_text(" ", strip=True)
                for el in (title_el, snip_el)
                if el is not None and el.get_text(strip=True)
            ]
            if parts:
                snippets.append(" — ".join(parts))
        # Be polite: one second between our own network requests.
        time.sleep(POLITE_SLEEP_SECONDS)
        if not snippets:
            logger.warning("fetch_match_context found no snippets for %r", query)
            return ""
        return "\n\n".join(snippets)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_match_context failed for %r: %s", query, exc)
        return ""


def parse_odds_from_text(text: str) -> Optional[dict]:
    """Regex extraction of American odds / spread / total from raw text.

    Returns a dict with keys ml_home_american, ml_away_american, spread_home,
    total_line (each value or None). Returns None only when there is NO numeric
    signal at all. Never fills defaults: an absent price stays None ("Unknown").
    """
    try:
        if not text or not text.strip():
            return None
        odds_tokens = _AMERICAN_RE.findall(text)
        if not odds_tokens:
            return None  # no American-odds-shaped number anywhere -> Unknown

        ml_home: Optional[int] = None
        ml_away: Optional[int] = None
        negatives = [int(t) for t in odds_tokens if t.startswith("-")]
        positives = [int(t) for t in odds_tokens if t.startswith("+")]
        if negatives:
            ml_home = max(negatives)      # e.g. -150 beats -200 as "the favorite"
        if positives:
            ml_away = min(positives)      # closest plus-money = counterpart dog
        if ml_home is None and len(positives) >= 2:
            ml_home, ml_away = positives[0], positives[1]
        if ml_away is None and len(negatives) >= 2 and ml_home is not None:
            ml_away = next(n for n in negatives if n != ml_home)

        spread: Optional[float] = None
        m = _SPREAD_RE.search(text)
        if m:
            spread = -abs(float(m.group(1).split("/")[0].strip()))

        total: Optional[float] = None
        m = _TOTAL_RE.search(text)
        if m:
            total = float(m.group(1))

        if ml_home is None and ml_away is None and spread is None and total is None:
            return None
        return {
            "ml_home_american": ml_home,
            "ml_away_american": ml_away,
            "spread_home": spread,
            "total_line": total,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_odds_from_text failed: %s", exc)
        return None
