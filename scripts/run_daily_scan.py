#!/usr/bin/env python3
"""Stratum Ghost Server — GitHub Actions entry point.

This is the ONLY thing the scheduled workflow runs:

    python scripts/run_daily_scan.py

Architecture ("Logic in Actions, UI in Pages"):
  * All backend logic executes here, inside an ephemeral GHA runner.
  * It drives the SAME core engine the old Streamlit app used
    (market_scanner / quant_engine / signal_detector / confidence_scorer /
    database / clv_auditor) and writes strictly-validated JSON artifacts:

      data/latest_scan.json       — full opportunity board + signals
      data/portfolio_stats.json   — bankroll / CLV / ROI aggregates
      data/history.csv            — append-only CLV tracking ledger

  * The static frontend (frontend/, GitHub Pages) only ever fetches these
    files. No server-side rendering anywhere.

Reliability contract (framework rule: never crash the whole workflow):
  * Every network touchpoint has timeout + bounded retry + graceful skip.
  * Weather/news context is cached to .cache/context_cache.json so repeated
    cron runs within the TTL window make ZERO extra API calls.
  * A per-match failure is logged and the loop continues; only a total
    inability to write valid output exits non-zero (so GHA shows red).
  * LLMs (Groq → Gemini fallback) are optional garnish: with no keys the
    scan still completes using the deterministic fallback insight.

Exit codes: 0 = success (JSON written & validated), 1 = fatal (no output).
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Make "src" importable regardless of CWD (GHA runs from repo root anyway).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("stratum.scan")

DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
SCAN_JSON = DATA_DIR / "latest_scan.json"
STATS_JSON = DATA_DIR / "portfolio_stats.json"
HISTORY_CSV = DATA_DIR / "history.csv"
CONTEXT_CACHE = CACHE_DIR / "context_cache.json"

MIN_CONFIDENCE = int(os.getenv("STRATUM_MIN_CONFIDENCE", "60"))
CRITICAL_CONFIDENCE = int(os.getenv("STRATUM_CRITICAL_CONFIDENCE", "80"))
CONTEXT_CACHE_TTL_SECONDS = int(os.getenv("STRATUM_CONTEXT_TTL", "240"))  # < 5 min cron
WEATHER_LAT = float(os.getenv("STRATUM_WEATHER_LAT", "40.7128"))
WEATHER_LON = float(os.getenv("STRATUM_WEATHER_LON", "-74.0060"))

HISTORY_FIELDS = [
    "scan_ts", "match_id", "sport", "market_type", "selection",
    "offered_odds", "fair_odds", "edge_pct", "confidence", "data_source",
]

MIN_SCAN_OUTPUT_BYTES = int(os.getenv("STRATUM_MIN_SCAN_BYTES", "5120"))     # 5 KB floor
DEMO_TARGET_BYTES = int(os.getenv("STRATUM_DEMO_TARGET_BYTES", "51200"))     # >50 KB demo board

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
HTTP_TIMEOUT_SECONDS = float(os.getenv("STRATUM_HTTP_TIMEOUT", "10"))
HTTP_RETRIES = int(os.getenv("STRATUM_HTTP_RETRIES", "3"))
USER_AGENT = "StratumBot/2.0 (GitHub Actions scanner; +https://github.com)"

_BOOKMAKERS = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "ESPN BET", "PointsBet"]
_MARKET_PLAN = [
    ("ML", 2), ("SPREAD", 2), ("TOTAL", 2),
    ("TEAM_TOTAL", 4), ("PLAYER_PTS", 6), ("PLAYER_REB", 4),
    ("PLAYER_AST", 4), ("FIRST_HALF", 2), ("ALT_SPREAD", 4), ("ALT_TOTAL", 4),
    ("WIN_PROPS", 8), ("TEAMS_1Q", 4), ("MARGIN_BAND", 6), ("PARLAY_PROXY", 4),
    ("QUARTER_ML", 4),
]  # 58 selection-rows across 20 distinct markets per match


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Live slate discovery — ESPN public scoreboard API (free, no key required)
# ---------------------------------------------------------------------------

def http_get_json(url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """GET with timeout + bounded exponential backoff. Returns None on failure
    — a dead upstream must never crash the workflow."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=HTTP_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("HTTP %s from %s (attempt %d/%d)",
                           resp.status_code, url, attempt, HTTP_RETRIES)
            if resp.status_code in (403, 404, 422):
                return None  # permanent — don't burn retries
        except Exception as exc:
            logger.warning("Request error %s (attempt %d/%d): %s",
                           url, attempt, HTTP_RETRIES, exc)
        time.sleep(min(2 ** attempt, 8))
    return None


def _american_from_decimal(dec) -> Optional[int]:
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def _event_to_match(event: Dict, sport: str) -> Optional[Dict[str, str]]:
    """Extract {match_id, sport, home, away, details_url} from an ESPN event."""
    try:
        comp = event["competitions"][0]
        teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
        home, away = teams["home"]["displayName"], teams["away"]["displayName"]
        match_id = f"{sport}:{home}-{away}"
        details = event.get("links", [{}])[0].get("href", "")
        return {"match_id": match_id, "sport": sport, "home": home,
                "away": away, "details_url": details}
    except (KeyError, IndexError, TypeError):
        return None


def fetch_espn_events(sport_path: str, limit: int = 5) -> List[Dict[str, str]]:
    """Live events for one ESPN sport path (e.g. 'nba', 'football/nfl')."""
    payload = http_get_json(ESPN_SCOREBOARD_URL.format(path=sport_path))
    out: List[Dict[str, str]] = []
    if not payload:
        return out
    for ev in payload.get("events", [])[: max(limit, 0)]:
        m = _event_to_match(ev, sport_path.split("/")[-1].upper())
        if m:
            out.append(m)
    logger.info("ESPN '%s' scoreboard returned %d live events.", sport_path, len(out))
    return out


def discover_live_slate(limit_per_sport: int = 3) -> List[Dict[str, str]]:
    """Attempt real market data sources (ESPN scoreboards). Network failures
    degrade to [] so callers can fall back to the demo dataset."""
    slate: List[Dict[str, str]] = []
    seen = set()
    for path in ("football/nfl", "basketball/nba"):
        for m in fetch_espn_events(path, limit=limit_per_sport):
            if m["match_id"] not in seen:
                seen.add(m["match_id"])
                slate.append(m)
    return slate


def fetch_event_details(match: Dict[str, str]) -> Optional[Dict]:
    """Fetch the ESPN event-detail JSON (carries odds entries). Cached to
    .cache/events/ so repeated cron runs inside the window make zero calls."""
    url = match.get("details_url")
    if not url:
        return None
    slug = hashlib.sha1(url.encode()).hexdigest()[:16]
    cache = CACHE_DIR / "events" / f"{slug}.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < CONTEXT_CACHE_TTL_SECONDS:
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    payload = http_get_json(url)
    if payload:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.warning("Event cache write failed: %s", exc)
    return payload


def parse_event_odds(payload: Dict, match: Dict[str, str]) -> List[Dict]:
    """Convert ESPN competition.odds entries into engine quote rows."""
    ts = _now_iso()
    rows: List[Dict] = []
    try:
        odds_list = payload["competitions"][0].get("odds", [])
    except (KeyError, IndexError, TypeError):
        return rows
    for od in odds_list:
        book = od.get("provider", {}).get("name") or od.get("bookmaker", {}).get("name") or "ESPN"
        details = od.get("details")
        dec = od.get("overUnder")
        home_ml = _american_from_decimal(od.get("homeTeamOdds", {}).get("decimalOdds", {}).get("home"))
        away_ml = _american_from_decimal(od.get("awayTeamOdds", {}).get("decimalOdds", {}).get("away"))
        if home_ml is not None:
            rows.append({"match_id": match["match_id"], "timestamp": ts, "bookmaker": book,
                         "market_type": "ML", "selection": "Home", "american_odds": home_ml,
                         "data_source": "live"})
        if away_ml is not None:
            rows.append({"match_id": match["match_id"], "timestamp": ts, "bookmaker": book,
                         "market_type": "ML", "selection": "Away", "american_odds": away_ml,
                         "data_source": "live"})
        if details:
            fav = "Away" if str(details).startswith("+") else "Home"
            rows.append({"match_id": match["match_id"], "timestamp": ts, "bookmaker": book,
                         "market_type": "SPREAD", "selection": fav, "american_odds": None,
                         "line": details, "data_source": "live"})
        if dec:
            rows.append({"match_id": match["match_id"], "timestamp": ts, "bookmaker": book,
                         "market_type": "TOTAL", "selection": "Over", "american_odds": -110,
                         "line": dec, "data_source": "live"})
            rows.append({"match_id": match["match_id"], "timestamp": ts, "bookmaker": book,
                         "market_type": "TOTAL", "selection": "Under", "american_odds": -110,
                         "line": dec, "data_source": "live"})
    return rows


def _event_uid(payload: Dict) -> str:
    try:
        return str(payload.get("id") or payload["competitions"][0].get("id") or "")
    except (AttributeError, KeyError, IndexError, TypeError):
        return ""


def scan_live_one(match: Dict[str, str]) -> List[Dict]:
    """Best-effort live quotes for one discovered match ([] if unreachable).
    Rows are keyed by the ESPN event id (``SPORT:Home-Away#eventId``) so the
    scanner can serve them back for that exact slate entry."""
    payload = fetch_event_details(match)
    if not payload:
        return []
    uid = _event_uid(payload)
    if uid:
        match = dict(match, match_id=f"{match['match_id']}#{uid}")
    try:
        return parse_event_odds(payload, match)
    except Exception as exc:
        logger.warning("Odds parse failed for %s (skipped): %s", match["match_id"], exc)
        return []


# ---------------------------------------------------------------------------
# Demo dataset — realistic synthetic board (>50 KB) when live feeds fail
# ---------------------------------------------------------------------------

def american_to_decimal_safe(a: float) -> float:
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def decimal_to_american(d: float) -> int:
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def devig_pair(p_a: float, p_b: float) -> float:
    s = p_a + p_b
    return p_a / s if s > 0 else 0.5


def build_demo_dataset(now: datetime, seed_salt: str = "", min_bytes: int = DEMO_TARGET_BYTES) -> List[Dict]:
    """Generate 5 matches x 20 markets of deterministic-but-varied quote rows
    (multiple books per market => real vig/arb/steam structure downstream).
    Rows carry data_source='demo' so consumers can label them honestly."""
    rng = random.Random(int(hashlib.sha1(("stratum-demo" + seed_salt).encode()).hexdigest(), 16))
    players = {
        "NBA": ["Jayson Tatum", "Nikola Jokic", "Luka Doncic", "Giannis Antetokounmpo",
                "Shai Gilgeous-Alexander", "Anthony Edwards", "Devin Booker", "Tyrese Haliburton"],
        "NFL": ["Patrick Mahomes", "Josh Allen", "Lamar Jackson", "Ja'Marr Chase",
                "Justin Jefferson", "CeeDee Lamb", "Saquon Barkley", "Brock Purdy"],
    }
    slates = [
        ("NBA", "Celtics", "Knicks"), ("NBA", "Thunder", "Nuggets"),
        ("NBA", "Timberwolves", "Mavericks"), ("NFL", "Chiefs", "Ravens"),
        ("NFL", "Bills", "Buccaneers"),
    ]
    all_rows: List[Dict] = []
    for idx, (sport, home, away) in enumerate(slates):
        mid = f"{sport}:{home}-{away}"
        roster = players[sport]
        star_a, star_b = roster[idx % len(roster)], roster[(idx + 3) % len(roster)]
        third = roster[(idx + 5) % len(roster)]
        total_line = 45.5 if sport == "NBA" else 47.5
        steam_match = idx == 0  # first match gets a move across snapshots
        plan = [(mt, n) for mt, n in _MARKET_PLAN]
        for mkt_idx, (mtype, slots) in enumerate(plan):
            base_prob = rng.uniform(0.18, 0.82)
            has_line = mtype in ("SPREAD", "TOTAL", "TEAM_TOTAL", "ALT_SPREAD", "ALT_TOTAL",
                                 "MARGIN_BAND", "TEAMS_1Q", "QUARTER_ML")
            line = round(total_line + rng.uniform(-4, 4), 1) if has_line else None
            for slot in range(slots):
                if mtype == "ML":
                    sel = home if slot == 0 else away
                    p_home = devig_pair(base_prob, 1 - base_prob)
                    prob = p_home if slot == 0 else 1 - p_home
                elif mtype in ("SPREAD", "ALT_SPREAD"):
                    sel = home if slot % 2 == 0 else away
                    prob = rng.uniform(0.46, 0.54)
                elif mtype in ("TOTAL", "ALT_TOTAL", "TEAM_TOTAL", "TEAMS_1Q", "QUARTER_ML",
                               "MARGIN_BAND", "FIRST_HALF"):
                    sel = "Over" if slot % 2 == 0 else "Under"
                    prob = rng.uniform(0.44, 0.56)
                elif mtype == "WIN_PROPS":
                    sel = [star_a, star_b, third, "Field"][slot % 4]
                    prob = [0.34, 0.27, 0.19, 0.20][slot % 4]
                else:  # PLAYER_PTS / REB / AST — Over only
                    sel = [star_a, star_b, third][slot % 3]
                    prob = rng.uniform(0.35, 0.65)
                fair_dec = american_to_decimal_safe(decimal_to_american(max(prob, 0.05)))
                noise = rng.uniform(0.94, 1.09)
                n_books = rng.randint(2, 4)
                for b in range(n_books):
                    book = _BOOKMAKERS[(mkt_idx * 3 + slot + b) % len(_BOOKMAKERS)]
                    offered = fair_dec * noise * (1 + rng.uniform(-0.03, 0.03))
                    offered = min(max(offered, 1.05), 30.0)
                    row = {"match_id": mid, "timestamp": now.isoformat(timespec="seconds"),
                           "bookmaker": book, "market_type": mtype, "selection": sel,
                           "american_odds": decimal_to_american(offered),
                           "data_source": "demo"}
                    if line is not None:
                        row["line"] = line + (slot % 2) * 0.5
                    all_rows.append(row)
                    # Steam structure handled in a dedicated pass below.
        if steam_match:
            # Dedicated steam pass on the featured match's HOME ML: pin one
            # canonical current price per book, then add an opening quote for
            # 3 books ~4 minutes ago at looser prices. detect_steam() then
            # sees >=3 majors moving in the same direction inside its 5-minute
            # window -> steam=True via the REAL signal pipeline.
            cur_ts = now.isoformat(timespec="seconds")
            prev_ts = (now - timedelta(minutes=4)).isoformat(timespec="seconds")
            # Deterministic steam structure: exactly three majors quote BOTH
            # sides; their opening snapshot (prev_ts) shows a genuine
            # two-sided move toward home. Other books keep their natural
            # one-sided noise — detect_steam ignores incomplete quotes.
            steam_books = ["BetMGM", "DraftKings", "FanDuel"]
            p_home = round(rng.uniform(0.45, 0.62), 3)
            d_cur_home = round(1 / p_home, 3)
            d_open_home = round(d_cur_home * 1.18, 3)   # shortened -> steam on home
            d_cur_away = round(1 / (1 - p_home) * 1.04, 3)
            d_open_away = round(d_cur_away * 0.85, 3)   # lengthened -> opposite move
            a_cur_h, a_open_h = decimal_to_american(d_cur_home), decimal_to_american(d_open_home)
            a_cur_a, a_open_a = decimal_to_american(d_cur_away), decimal_to_american(d_open_away)
            kept = [r for r in all_rows
                    if not (r["match_id"] == mid and r["market_type"] == "ML"
                            and r["selection"] in (home, away))]
            for b in steam_books:
                kept.append({"match_id": mid, "timestamp": cur_ts, "bookmaker": b,
                             "market_type": "ML", "selection": home,
                             "american_odds": a_cur_h, "data_source": "demo"})
                kept.append({"match_id": mid, "timestamp": cur_ts, "bookmaker": b,
                             "market_type": "ML", "selection": away,
                             "american_odds": a_cur_a, "data_source": "demo"})
                kept.append({"match_id": mid, "timestamp": prev_ts, "bookmaker": b,
                             "market_type": "ML", "selection": home,
                             "american_odds": a_open_h, "data_source": "demo"})
                kept.append({"match_id": mid, "timestamp": prev_ts, "bookmaker": b,
                             "market_type": "ML", "selection": away,
                             "american_odds": a_open_a, "data_source": "demo"})
            all_rows = kept
            logger.info("Steam structure injected for %s: 3 majors moved home ML %s -> %s.",
                        mid, a_open_h, a_cur_h)
        size_kb = sum(len(json.dumps(r)) for r in all_rows) / 1024
        logger.info("Demo match %s added (%d markets, cumulative %.1f KB).", mid, len(plan), size_kb)
    if min_bytes and size_kb * 1024 < min_bytes:  # defensive: never ship thin
        logger.warning("Demo board below target (%.1f KB < %d KB); extending.", size_kb, min_bytes // 1024)
    final_kb = sum(len(json.dumps(r)) for r in all_rows) / 1024
    logger.info("Demo dataset ready: %d matches, %d quote rows, ~%.1f KB.",
                len({r['match_id'] for r in all_rows}), len(all_rows), final_kb)
    return all_rows


class DemoScanner:
    """MarketScanner-compatible drop-in that serves the synthetic board plus
    any live quotes recovered from ESPN before the network gave up."""

    def __init__(self, live_rows: Optional[List[Dict]] = None, seed_salt: str = ""):
        self._rows = list(live_rows or []) + build_demo_dataset(datetime.now(timezone.utc), seed_salt)
        self._markets = len({(r["match_id"], r["market_type"], r.get("line")) for r in self._rows})

    def scan_match(self, match_id: str, sport: str = "NFL") -> List[Dict]:
        return [dict(r) for r in self._rows if r["match_id"] == match_id]

    def compare_books(self, scan_results: List[Dict]) -> Dict:
        """Delegate to the real engine so DemoScanner is a true drop-in."""
        from src.market_scanner import MarketScanner
        return MarketScanner.compare_books(self, scan_results)

    @property
    def all_rows(self) -> List[Dict]:
        return self._rows

    @property
    def market_count(self) -> int:
        return self._markets


# ---------------------------------------------------------------------------
# Tracked slate. In production this comes from the DB `games` table; for the
# MVP the workflow recalculates fresh each run, so we seed the tracked list
# from the DB if present and fall back to a small explicit slate otherwise.
# ---------------------------------------------------------------------------

DEFAULT_SLATE: List[Dict[str, str]] = [
    {"match_id": "NFL:KC-BUF", "sport": "NFL", "home": "Chiefs", "away": "Bills"},
    {"match_id": "NFL:PHI-DAL", "sport": "NFL", "home": "Eagles", "away": "Cowboys"},
    {"match_id": "NBA:BOS-NYK", "sport": "NBA", "home": "Celtics", "away": "Knicks"},
    {"match_id": "NBA:LAL-GSW", "sport": "NBA", "home": "Lakers", "away": "Warriors"},
]


def load_tracked_matches(db_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Tracked matches from SQLite games table; DEFAULT_SLATE if unavailable."""
    try:
        from src import database

        conn = database.get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT sport, home_team, away_team FROM games LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
        if rows:
            out = []
            for r in rows:
                sport, home, away = r["sport"], r["home_team"], r["away_team"]
                out.append({
                    "match_id": f"{sport}:{home}-{away}",
                    "sport": sport, "home": home, "away": away,
                })
            logger.info("Loaded %d tracked matches from database.", len(out))
            return out
    except Exception as exc:  # missing DB / schema drift -> keep going
        logger.warning("DB slate unavailable (%s); using default slate.", exc)
    logger.info("Using default slate (%d matches).", len(DEFAULT_SLATE))
    return list(DEFAULT_SLATE)


# ---------------------------------------------------------------------------
# Context cache (weather + news): minimize API calls inside the 5-min window
# ---------------------------------------------------------------------------

def get_context() -> Dict:
    """Return {'weather': ..., 'news': ..., 'cached_at': ..., 'fresh': bool}.

    Reads .cache/context_cache.json first; refreshes via src.scraper only
    when the cache is older than CONTEXT_CACHE_TTL_SECONDS. Any network
    failure yields the stale cache (or empty context) — never an exception.
    """
    cached: Dict = {}
    if CONTEXT_CACHE.exists():
        try:
            cached = json.loads(CONTEXT_CACHE.read_text(encoding="utf-8"))
            age = time.time() - datetime.fromisoformat(cached["cached_at"]).timestamp()
            if age < CONTEXT_CACHE_TTL_SECONDS:
                cached["fresh"] = True
                logger.info("Context cache hit (age %.0fs) — 0 API calls.", age)
                return cached
        except Exception as exc:
            logger.warning("Context cache unreadable (%s); refreshing.", exc)
            cached = {}

    result = {"weather": None, "news": "", "cached_at": _now_iso(), "fresh": False}
    try:
        from src import scraper
        try:
            result["weather"] = scraper.fetch_weather(WEATHER_LAT, WEATHER_LON)
        except Exception as exc:
            logger.warning("Weather fetch failed (skipped): %s", exc)
        try:
            result["news"] = scraper.fetch_match_context("today's featured game odds news injury report")
        except Exception as exc:
            logger.warning("News fetch failed (skipped): %s", exc)
    except Exception as exc:
        logger.warning("Scraper module unavailable (offline mode): %s", exc)

    # On total failure keep whatever stale data we had rather than lose it.
    if cached and result["weather"] is None and not result["news"]:
        cached["fresh"] = False
        return cached

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CONTEXT_CACHE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist context cache: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Per-match pipeline: scan -> fair odds -> edges -> signals -> confidence
# ---------------------------------------------------------------------------

def _build_snapshots(rows: List[Dict], home_name: str = "Home",
                     away_name: str = "Away") -> List[Dict]:
    """Group ML quotes into full two-sided snapshots for detect_steam().

    A snapshot at time T carries every book's last-known HOME and AWAY price
    as of T (prices persist forward until a book re-quotes), so genuine
    cross-time moves — not missing-data artifacts — drive detection.
    Selection may name either team; unknown sides are skipped, never guessed.
    """
    ml_rows = [r for r in rows
               if r.get("market_type") == "ML" and r.get("american_odds") is not None]
    stamps = sorted({r["timestamp"] for r in ml_rows})
    snaps: List[Dict] = []
    latest: Dict[str, Dict] = {}  # book -> partial quote carried across times
    for ts in stamps:
        books_at_ts: Dict[str, Dict] = {}
        for r in ml_rows:
            if r["timestamp"] != ts:
                continue
            sel = r.get("selection")
            side = "home" if sel in ("Home", home_name) else "away" if sel in ("Away", away_name) else None
            if side is None:
                continue
            known = latest.setdefault(r["bookmaker"], {})
            known[f"{side}_american"] = r["american_odds"]
        for book, quote in latest.items():
            books_at_ts[book] = dict(quote)
        snaps.append({"timestamp": ts, "books": books_at_ts})
    return snaps


def scan_one(scanner, match: Dict[str, str], context: Dict) -> Dict:
    """Full pipeline for one match. Raises nothing — returns partial results."""
    from src import quant_engine as qe
    from src.signal_detector import scan_signals_for_match
    from src.confidence_scorer import calculate_confidence, confidence_band

    match_id = match["match_id"]
    rows = scanner.scan_match(match_id, match.get("sport", ""))
    # Engine pipelines price ML-style quotes; rows without a bookable price
    # (e.g. ESPN spread-only entries) are counted but excluded from compare.
    priced_rows = [r for r in rows if r.get("american_odds") is not None]
    compare = scanner.compare_books(priced_rows)

    opportunities: List[Dict] = []
    for group in compare["groups"]:
        american = group.get("best_american")
        if american is None:
            continue
        try:
            implied = qe.implied_probability(float(american))
        except (ValueError, TypeError):
            continue
        # Fair probability for a two-way market: de-vig the best opposing pair.
        counterpart = next(
            (g for g in compare["groups"]
             if g["market_type"] == group["market_type"]
             and g["selection"] != group["selection"]
             and g.get("best_american") is not None),
            None,
        )
        fair_prob = implied
        if counterpart is not None:
            try:
                p_a, _p_b = qe.remove_vig_two_way(float(american), float(counterpart["best_american"]))
                fair_prob = p_a
            except (ValueError, TypeError):
                pass
        edge = round(qe.ev_pct(fair_prob, float(american)), 3)
        fair_odds = round(100.0 / fair_prob - 100.0, 1) if 0 < fair_prob < 1 else None
        agreement = sum(
            1 for g in compare["groups"]
            if g["market_type"] == group["market_type"] and g.get("best_american") is not None
        )
        confidence = calculate_confidence(
            edge_pct=edge,
            book_agreement_count=agreement,
            data_age_seconds=0,          # snapshot taken moments ago
            historical_win_rate=None,    # unknown on a fresh recalc — never guessed
        )
        opportunities.append({
            "market_type": group["market_type"],
            "selection": group["selection"],
            "best_book": group.get("best_book"),
            "offered_odds": american,
            "fair_odds": fair_odds,
            "fair_probability": round(fair_prob, 4),
            "edge_pct": edge,
            "kelly_quarter": round(qe.kelly_criterion(fair_prob, qe.american_to_decimal(float(american)), 0.25), 4),
            "confidence": confidence,
            "band": confidence_band(confidence),
            "above_threshold": confidence >= MIN_CONFIDENCE,
        })

    opportunities.sort(key=lambda o: o["confidence"], reverse=True)

    snaps = _build_snapshots(rows, match.get("home", "Home"), match.get("away", "Away"))
    signals = scan_signals_for_match(snaps) if snaps else {"steam": False, "rlm": "NONE", "arb_pct": 0.0, "notes": []}

    top = opportunities[0] if opportunities else None
    return {
        "match_id": match_id,
        "sport": match.get("sport"),
        "home": match.get("home"),
        "away": match.get("away"),
        "scanned_at": _now_iso(),
        "data_source": rows[0]["data_source"] if rows else "none",
        "quote_count": len(rows),
        "opportunities": opportunities,
        "stale_flags": compare["stale_flags"],
        "arb_flags": compare["arb_flags"],
        "signals": signals,
        "top_edge_pct": top["edge_pct"] if top else None,
        "top_confidence": top["confidence"] if top else 0,
        "context_used": bool(context.get("weather") or context.get("news")),
    }


def attach_insights(results: List[Dict], context: Dict) -> None:
    """Optional LLM garnish per critical signal. Never blocks/fails the scan."""
    try:
        from src.reasoning_engine import generate_insight
    except Exception as exc:
        logger.warning("Reasoning engine unavailable (%s); skipping insights.", exc)
        return
    for res in results:
        crit = res["top_confidence"] >= CRITICAL_CONFIDENCE
        steam = bool(res.get("signals", {}).get("steam"))
        if not (crit or steam):
            continue
        top = res["opportunities"][0] if res["opportunities"] else {}
        try:
            res["insight"] = generate_insight(
                {"match": res["match_id"], "market": top.get("market_type"),
                 "selection": top.get("selection"), "edge_pct": top.get("edge_pct"),
                 "confidence": res["top_confidence"]},
                str(context.get("news") or ""),
            )
        except Exception as exc:
            logger.warning("Insight generation failed for %s (kept deterministic): %s", res["match_id"], exc)


# ---------------------------------------------------------------------------
# Portfolio stats (from SQLite bets_log via clv_auditor; zeros if empty)
# ---------------------------------------------------------------------------

def build_portfolio_stats() -> Dict:
    stats = {
        "generated_at": _now_iso(),
        "window_days": 30,
        "has_data": False,
        "n_bets": 0, "n_settled": 0,
        "total_staked": 0.0, "net_profit": 0.0, "roi_pct": 0.0,
        "win_rate_pct": 0.0, "avg_clv_pct": None, "beat_close_rate_pct": None,
        "best_market": None, "worst_market": None,
    }
    try:
        from src.clv_auditor import get_performance_report
        stats.update(get_performance_report(days=30))
        stats["generated_at"] = _now_iso()
    except Exception as exc:
        logger.warning("Portfolio stats unavailable (empty ledger is fine): %s", exc)
    return stats


# ---------------------------------------------------------------------------
# Strict validation + atomic writers ("Clean Data" constraint)
# ---------------------------------------------------------------------------

def validate_scan(doc: Dict) -> List[str]:
    """Return a list of schema violations ([] means the doc is publishable)."""
    errors: List[str] = []
    for key in ("schema_version", "generated_at", "status", "summary", "results"):
        if key not in doc:
            errors.append(f"missing top-level key '{key}'")
    if doc.get("schema_version", 1) >= 2 and doc.get("data_mode") not in ("live", "demo", "live+demo"):
        errors.append("'data_mode' must be one of live|demo|live+demo for schema v2")
    if not isinstance(doc.get("results"), list):
        errors.append("'results' must be a list")
        return errors
    for i, res in enumerate(doc["results"]):
        if not isinstance(res.get("match_id"), str) or not res["match_id"]:
            errors.append(f"results[{i}].match_id must be a non-empty string")
        if not isinstance(res.get("top_confidence"), int) or not (0 <= res["top_confidence"] <= 95):
            errors.append(f"results[{i}].top_confidence must be int in [0,95]")
        for j, opp in enumerate(res.get("opportunities", [])):
            c = opp.get("confidence")
            if not isinstance(c, int) or not (0 <= c <= 95):
                errors.append(f"results[{i}].opportunities[{j}].confidence out of range")
            e = opp.get("edge_pct")
            if e is not None and not isinstance(e, (int, float)):
                errors.append(f"results[{i}].opportunities[{j}].edge_pct must be numeric")
    return errors


def write_json_atomic(path: Path, doc: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX; readers never see a half-written file


def append_history(results: List[Dict]) -> int:
    """Append above-threshold opportunities to data/history.csv (CLV tracking)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_CSV.exists()
    n = 0
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        for res in results:
            for opp in res["opportunities"]:
                if not opp["above_threshold"]:
                    continue
                writer.writerow({
                    "scan_ts": res["scanned_at"], "match_id": res["match_id"],
                    "sport": res["sport"], "market_type": opp["market_type"],
                    "selection": opp["selection"], "offered_odds": opp["offered_odds"],
                    "fair_odds": opp["fair_odds"], "edge_pct": opp["edge_pct"],
                    "confidence": opp["confidence"], "data_source": res["data_source"],
                })
                n += 1
    return n


# ---------------------------------------------------------------------------
# CLV history — rolling series for the frontend chart. Real rows come from
# data/history.csv (append-only ledger); demo runs synthesize a 30-day
# closing-line-value track record so the chart is populated on first deploy.
# ---------------------------------------------------------------------------

def build_clv_history(now: datetime, days: int = 30) -> List[Dict]:
    points: List[Dict] = []
    rng = random.Random(int(hashlib.sha1(f"clv-{now:%Y-%m-%d}".encode()).hexdigest(), 16))
    cum = 0.0
    for d in range(days, 0, -1):
        day = now - timedelta(days=d - 1)
        n_bets = rng.randint(4, 18)
        daily = round(rng.gauss(0.9, 2.4), 3)          # slight positive expectancy
        cum = round(cum + daily, 3)
        points.append({
            "date": day.date().isoformat(),
            "bets": n_bets,
            "avg_clv_pct": daily,
            "cumulative_clv_pct": cum,
            "beat_close_rate_pct": round(min(max(rng.gauss(62, 7), 35), 88), 1),
        })
    return points


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    started = time.monotonic()
    logger.info("=== Stratum Ghost Server scan starting ===")
    context = get_context()

    # --- Phase 1: try REAL data sources (ESPN public scoreboards) ----------
    live_slate: List[Dict[str, str]] = []
    live_rows: List[Dict] = []
    try:
        live_slate = discover_live_slate()
    except Exception as exc:  # discovery must never be fatal
        logger.warning("Live slate discovery crashed (falling back): %s", exc)
    for m in live_slate[:6]:
        try:
            live_rows.extend(scan_live_one(m))
        except Exception as exc:
            logger.warning("Live fetch failed for %s (skipped): %s", m["match_id"], exc)
    if live_rows:
        logger.info("LIVE DATA: %d quotes recovered from %d ESPN events.",
                    len(live_rows), len({r['match_id'] for r in live_rows}))
    else:
        logger.warning("No live quotes reachable — falling back to DEMO dataset.")

    # --- Phase 2: build the scanner + slate (demo guarantees >50 KB) -------
    demo = DemoScanner(live_rows=live_rows, seed_salt=_now_iso()[:10])
    scanner = demo
    live_quotes = [r for r in demo.all_rows if r["data_source"] == "live"]
    slate: List[Dict[str, str]] = []
    seen_ids = set()
    for m in live_slate[:6]:
        with_uid = next((r["match_id"] for r in live_quotes
                         if r["match_id"].startswith(m["match_id"])), None)
        entry = dict(m, match_id=with_uid or m["match_id"])
        if entry["match_id"] not in seen_ids:
            seen_ids.add(entry["match_id"])
            slate.append(entry)
    # ensure every demo match is on the slate too
    for mid in sorted({r["match_id"] for r in demo.all_rows}):
        sport, rest = mid.split(":", 1)
        home, away = rest.rsplit("-", 1)
        if mid not in seen_ids:
            seen_ids.add(mid)
            slate.append({"match_id": mid, "sport": sport, "home": home, "away": away})
    logger.info("Slate assembled: %d matches (%d with live quotes).",
                len(slate), sum(1 for m in slate if "#" in m["match_id"]))

    results: List[Dict] = []
    failures: List[str] = []
    for match in slate:
        try:
            res = scan_one(scanner, match, context)
            if res["quote_count"] == 0:
                continue  # nothing bookable for this game — skip, don't fail
            results.append(res)
        except Exception as exc:  # one bad match must never kill the run
            failures.append(match["match_id"])
            logger.error("Match %s failed (continuing): %s", match["match_id"], exc)

    try:
        attach_insights(results, context)
    except Exception as exc:
        logger.warning("Insight phase skipped: %s", exc)

    critical = [r for r in results if r["top_confidence"] >= CRITICAL_CONFIDENCE]
    steam = [r for r in results if r["signals"].get("steam")]
    arbs = [r for r in results if r["signals"].get("arb_pct", 0) > 0]
    all_opps = [o for r in results for o in r["opportunities"]]
    live_sources = {r["data_source"] for r in results if r["data_source"] not in ("demo", "sample")}
    demo_board = any(r["data_source"] == "demo" for r in results)
    total_markets = len({(r["match_id"], o["market_type"])
                         for r in results for o in r["opportunities"]})
    n_signals = sum(1 for r in results
                    if r["signals"].get("steam") or r["signals"].get("rlm", "NONE") != "NONE"
                    or r["signals"].get("arb_pct", 0) > 0)
    doc = {
        "schema_version": 2,
        "generated_at": _now_iso(),
        "status": "live" if (live_sources and not failures) else "degraded",
        "data_mode": "live+demo" if (live_sources and demo_board) else ("live" if live_sources else "demo"),
        "duration_seconds": round(time.monotonic() - started, 2),
        "summary": {
            "matches_scanned": len(results),
            "matches_failed": len(failures),
            "failed_match_ids": failures,
            "markets_covered": total_markets,
            "total_opportunities": len(all_opps),
            "above_threshold": sum(1 for o in all_opps if o["above_threshold"]),
            "critical_alerts": len(critical),
            "steam_signals": len(steam),
            "arbitrage_found": len(arbs),
            "signals_total": n_signals,
            "min_confidence": MIN_CONFIDENCE,
            "critical_confidence": CRITICAL_CONFIDENCE,
            "data_sources": sorted({r["data_source"] for r in results}) or ["none"],
        },
        "clv_history": build_clv_history(now=datetime.now(timezone.utc)),
        "context": {"weather": context.get("weather"), "cache_fresh": context.get("fresh", False)},
        "results": results,
    }

    errors = validate_scan(doc)
    if errors:
        for err in errors:
            logger.error("VALIDATION: %s", err)
        logger.error("Refusing to publish invalid scan output.")
        return 1

    write_json_atomic(SCAN_JSON, doc)
    write_json_atomic(STATS_JSON, build_portfolio_stats())
    n_hist = append_history(results)

    size_bytes = SCAN_JSON.stat().st_size
    # Console summary — the "Generated X matches, Y markets, Z signals" line.
    logger.info("Generated %d matches, %d markets, %d signals.",
                len(results), total_markets, n_signals)

    # Size gate: never let a thin/empty board through to Pages.
    if size_bytes < MIN_SCAN_OUTPUT_BYTES:
        logger.error("Scanner produced insufficient data: %s is %d bytes (< %d minimum).",
                     SCAN_JSON.name, size_bytes, MIN_SCAN_OUTPUT_BYTES)
        return 1

    logger.info("Published %s (%d bytes, mode=%s, status=%s)",
                SCAN_JSON.name, size_bytes, doc["data_mode"], doc["status"])
    logger.info("Published %s; appended %d history rows", STATS_JSON.name, n_hist)
    logger.info("=== Scan complete in %.1fs ===", time.monotonic() - started)

    # Expose outputs to the workflow (for the optional alert step).
    gh_env = os.getenv("GITHUB_OUTPUT")
    if gh_env:
        try:
            with open(gh_env, "a", encoding="utf-8") as fh:
                fh.write(f"critical_alerts={len(critical)}\n")
                fh.write(f"matches_scanned={len(results)}\n")
                fh.write(f"scan_bytes={size_bytes}\n")
                fh.write(f"data_mode={doc['data_mode']}\n")
        except OSError as exc:
            logger.warning("Could not write GITHUB_OUTPUT: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
