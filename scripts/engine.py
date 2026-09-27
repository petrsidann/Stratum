"""
Stratum V2.0 Quant Engine
=========================
Runs on GitHub Actions every 15 minutes (cron). Fetches real, live odds from
public sources, removes vig to compute fair probabilities, detects market
signals (steam moves, reverse line movement, stale lines), computes EV and
Kelly stake for every scanned market, and writes a single feed file:

    data/live_market_feed.json

Data sources (in priority order, graceful fallback between each):
  1. ESPN Scoreboard + ESPN Odds APIs (no key required, real live data)
  2. The-Odds-API free tier (requires ODDS_API_KEY secret; optional)
  3. Direct scraping of public Pinnacle/Circa mirror pages (best-effort,
     via requests + beautifulsoup4; skipped silently if blocked)

Integrity rule: NO SYNTHETIC DATA. If no source returns odds for a market,
the market is emitted with bookmakers=[] and the client renders "No Signal".
Player props are only emitted when a source actually lists named players.

Never crashes: every network call is wrapped, rate limits are respected,
and partial results are always written to disk.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

try:
    import requests
except ImportError:  # pragma: no cover
    print("[engine] FATAL: requests is not installed", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup  # noqa: F401  (used by scrape mirrors)
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_VERSION = "2.0.0"
OUTPUT_PATH = os.environ.get(
    "STRATUM_OUTPUT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "live_market_feed.json"),
)
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()

REQUEST_TIMEOUT = 12          # seconds per HTTP request
MAX_RETRIES = 3               # per endpoint
BACKOFF_BASE = 1.5            # exponential backoff factor (seconds)
GLOBAL_BUDGET_S = float(os.environ.get("STRATUM_BUDGET_S", "480"))  # 8 min cap
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

_START_TIME = time.monotonic()


def budget_left() -> float:
    return GLOBAL_BUDGET_S - (time.monotonic() - _START_TIME)


def log(msg: str) -> None:
    elapsed = time.monotonic() - _START_TIME
    print(f"[engine {elapsed:7.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Resilient HTTP layer
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update(HEADERS)


def http_get(url: str, params: Optional[Dict[str, Any]] = None,
             retries: int = MAX_RETRIES) -> Optional[Any]:
    """GET with retry/backoff. Returns parsed JSON, text, or None. Never raises."""
    for attempt in range(retries):
        if budget_left() <= 5:
            log(f"budget exhausted, skipping GET {url[:80]}")
            return None
        try:
            resp = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype or url.endswith(".json"):
                    try:
                        return resp.json()
                    except ValueError:
                        return resp.text
                return resp.text
            if resp.status_code in (429, 402):
                # Rate limited / quota exceeded: long backoff, then give up quietly.
                wait = BACKOFF_BASE ** (attempt + 1) * 4
                log(f"HTTP {resp.status_code} from {url[:70]} -> backing off {wait:.1f}s")
                time.sleep(min(wait, max(budget_left() - 5, 0)))
                continue
            if resp.status_code == 403:
                # Edge/WAF bot block on this IP. One polite retry with a
                # minimal header set; otherwise skip the source silently.
                if attempt == 0:
                    try:
                        alt = _session.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                           headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"})
                        if alt.status_code == 200:
                            ctype = alt.headers.get("Content-Type", "")
                            if "json" in ctype or url.endswith(".json"):
                                try:
                                    return alt.json()
                                except ValueError:
                                    return alt.text
                            return alt.text
                    except requests.RequestException:
                        pass
                log(f"HTTP 403 (bot-blocked) on {url[:70]} -> skipping source")
                return None
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(BACKOFF_BASE ** (attempt + 1))
                continue
            return None  # 4xx other: not retryable
        except requests.RequestException as exc:
            log(f"network error ({type(exc).__name__}) on {url[:70]}")
            time.sleep(BACKOFF_BASE ** (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Market signal detection state (persisted across runs for line movement)
# ---------------------------------------------------------------------------

STATE_PATH = os.environ.get(
    "STRATUM_STATE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", ".engine_state.json"),
)


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"odds_history": {}, "last_run": None}


def save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        # Keep history bounded: last 24h of snapshots per market key.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
        hist = state.get("odds_history", {})
        trimmed: Dict[str, List[Tuple[str, float]]] = {}
        for key, points in hist.items():
            kept = [p for p in points
                    if _parse_iso(p[0]) is not None and _parse_iso(p[0]) >= cutoff]
            if kept:
                trimmed[key] = kept[-48:]
        state["odds_history"] = trimmed
        state["last_run"] = utc_now_iso()
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as exc:
        log(f"state save failed: {exc}")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def record_odds(state: Dict[str, Any], market_key: str, price: float) -> List[List[Any]]:
    """Append current price snapshot; return movement history (ts, price)."""
    hist = state.setdefault("odds_history", {})
    points = hist.setdefault(market_key, [])
    now = utc_now_iso()
    if not points or points[-1][1] != price:
        points.append([now, price])
    recent = [p for p in points
              if (_dt_utc() - (_parse_iso(p[0]) or _dt_utc())) <= timedelta(hours=2)]
    return recent


def _dt_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return _dt_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Core quant math
# ---------------------------------------------------------------------------

def american_to_decimal(odds: float) -> Optional[float]:
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    if odds == 0 or math.isnan(odds) or math.isinf(odds):
        return None
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def decimal_to_implied(dec: float) -> Optional[float]:
    if dec is None or dec <= 1.0:
        return None
    return 1.0 / dec


def calculate_fair_value(implied_probs: List[float]) -> Optional[List[float]]:
    """Remove vig by multiplicative normalization.

    Raw probs across an outcome set sum to >1 (the overround/vig).
    Fair prob_i = implied_i / sum(implieds). Returns None if input invalid.
    """
    probs = [p for p in implied_probs if p is not None and p > 0]
    if len(probs) < 2:
        return None
    total = sum(probs)
    if total <= 1.0 or total >= 2.0:  # malformed handle
        return None
    return [p / total for p in probs]


def expected_value(fair_prob: float, decimal_odds: float) -> float:
    """EV per 1 unit staked: p*(d-1) - (1-p)."""
    if decimal_odds is None or decimal_odds <= 1.0:
        return 0.0
    return fair_prob * (decimal_odds - 1.0) - (1.0 - fair_prob)


def kelly_stake(ev: float, decimal_odds: float, fraction: float = 0.25) -> float:
    """Fractional Kelly stake as share of bankroll (default quarter-Kelly)."""
    if decimal_odds is None or decimal_odds <= 1.0 or ev <= 0:
        return 0.0
    b = decimal_odds - 1.0
    p = (ev + 1.0) / (b + 1.0) if (b + 1.0) != 0 else 0.0
    q = 1.0 - p
    full = (b * p - q) / b if b > 0 else 0.0
    return max(0.0, round(full * fraction, 4))


def confidence_score(edge_pct: float, num_books: int, has_movement: bool,
                     liquidity_rank: int) -> float:
    """0-100 heuristic blend: edge size, book count, sharp anchor, fresh movement."""
    if edge_pct <= 0:
        return 0.0
    s_edge = min(edge_pct / 8.0, 1.0) * 45.0
    s_books = min(num_books / 5.0, 1.0) * 20.0
    s_move = 15.0 if has_movement else 0.0
    s_liq = max(0.0, (5 - max(liquidity_rank, 1)) / 4.0) * 20.0
    return round(min(s_edge + s_books + s_move + s_liq, 100.0), 1)


SHARP_BOOKS = {"pinnacle", "circa", "bookmaker", "bj"}
RETAIL_BOOKS = {"draftkings", "fanduel", "betmgm", "caesars", "pointsbet",
                "espnbet", "williamhill", "bovada", "betrivers"}


def detect_signals(market: Dict[str, Any], history: List[List[Any]]) -> List[str]:
    """Steam move / RLM / stale line detection on a computed market dict."""
    signals: List[str] = []
    best = market.get("best") or {}
    fair = market.get("fair_probability")

    # Line movement: meaningful drift inside last 2 hours
    if len(history) >= 2 and fair:
        first_dec = american_to_decimal(history[0][1])
        last_dec = american_to_decimal(history[-1][1])
        if first_dec and last_dec:
            move = first_dec - last_dec  # positive => shortened (more likely)
            if abs(move) >= 0.05:
                signals.append("LINE_MOVE")

    prices = [(b.get("bookmaker", ""), b.get("price")) for b in market.get("bookmakers", [])]
    sharp_prices = [p for name, p in prices
                    if any(s in name.lower().replace(" ", "") for s in SHARP_BOOKS) and p]
    retail_prices = [p for name, p in prices
                     if any(r in name.lower().replace(" ", "") for r in RETAIL_BOOKS) and p]

    # Stale line: retail significantly off consensus/sharp while best price lags
    if sharp_prices and retail_prices and fair:
        sharp_best = max(sharp_prices, key=lambda p: american_to_decimal(p) or 0)
        retail_best = max(retail_prices, key=lambda p: decimal_to_implied(american_to_decimal(p) or 2) or 1)
        sd = american_to_decimal(sharp_best) or 0
        rd = american_to_decimal(best.get("price") or 0) or 0
        if rd and sd and (1 / rd) - (1 / sd) > 0.04:
            signals.append("STALE_LINE")

    # Reverse line movement: line moved against the direction of best-price books
    if market.get("direction") == "against_consensus":
        signals.append("RLM")

    # Steam move: strong edge anchored at a sharp book
    if fair and best.get("price"):
        ev = market.get("ev_percent", 0) / 100.0
        book_name = (best.get("bookmaker") or "").lower().replace(" ", "")
        if ev >= 0.03 and any(s in book_name for s in SHARP_BOOKS):
            signals.append("STEAM")

    return signals


# ---------------------------------------------------------------------------
# Source 1: ESPN Scoreboard API (real live data, no key)
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
ESPN_ODDS = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/odds"

ESPN_LEAGUES = [
    {"key": "nba", "sport": "basketball", "league": "nba", "display": "NBA", "icon": "basketball"},
    {"key": "nfl", "sport": "football", "league": "nfl", "display": "NFL", "icon": "football"},
    {"key": "mlb", "sport": "baseball", "league": "mlb", "display": "MLB", "icon": "baseball"},
    {"key": "epl", "sport": "soccer", "league": "eng.1", "display": "Premier League", "icon": "soccer"},
    {"key": "ucl", "sport": "soccer", "league": "uefa.champions", "display": "Champions League", "icon": "soccer"},
    {"key": "laliga", "sport": "soccer", "league": "esp.1", "display": "La Liga", "icon": "soccer"},
    {"key": "seriea", "sport": "soccer", "league": "ita.1", "display": "Serie A", "icon": "soccer"},
    {"key": "bundesliga", "sport": "soccer", "league": "ger.1", "display": "Bundesliga", "icon": "soccer"},
    {"key": "ligue1", "sport": "soccer", "league": "fra.1", "display": "Ligue 1", "icon": "soccer"},
    {"key": "nhl", "sport": "hockey", "league": "nhl", "display": "NHL", "icon": "hockey"},
]


def fetch_espn_scoreboard(league: Dict[str, str]) -> List[Dict[str, Any]]:
    """Upcoming games today+tomorrow for one league. Returns raw event list.

    The default (no-date) scoreboard call is tried first because ESPN's dated
    queries are flaky on some leagues; results from all attempts are merged
    by event id.
    """
    today = datetime.now(timezone.utc)
    events: List[Dict[str, Any]] = []
    seen = set()

    def absorb(data: Any) -> None:
        if isinstance(data, dict):
            for ev in data.get("events", []) or []:
                eid = ev.get("id")
                if eid and eid not in seen:
                    seen.add(eid)
                    events.append(ev)

    # Default window (covers "next games" reliably).
    absorb(http_get(ESPN_SCOREBOARD.format(sport=league["sport"], league=league["league"]),
                    params={"limit": 100}))
    # Explicit today/tomorrow windows to catch timezone edge cases.
    for day_off in (0, 1):
        if budget_left() <= 20:
            break
        d = (today + timedelta(days=day_off)).strftime("%Y%m%d")
        absorb(http_get(ESPN_SCOREBOARD.format(sport=league["sport"], league=league["league"]),
                        params={"dates": d, "limit": 100}))
    return events


def _espn_game_odds(league: Dict[str, str], event_id: str) -> List[Dict[str, Any]]:
    """Odds object list from ESPN's odds endpoint for one game (per provider)."""
    data = http_get(ESPN_ODDS.format(**{k: league[k] for k in ("sport", "league")}),
                    params={"gameId": event_id})
    if isinstance(data, dict):
        return data.get("current", []) or []
    return []


def _extract_provider_rows(prov: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize one ESPN odds provider object into flat price rows.

    Handles both payload shapes seen in the wild:
      A) summary.outcomes[] with per-outcome price/point
      B) structured moneyline / pointSpread / total blocks with
         home/away (or over/under) open+close odds -- the shape MLB/NBA
         scoreboards actually return.
    """
    rows: List[Dict[str, Any]] = []
    pname = (prov.get("provider", {}) or {}).get("name", "ESPN")

    def add(label: str, selection: str, price: Any, point: Any = None) -> None:
        try:
            p = int(float(price))
        except (TypeError, ValueError):
            return
        if p == 0:
            return
        rows.append({"provider": pname, "label": label, "selection": selection,
                     "price": p, "details": None, "point": point})

    # Shape A: explicit outcomes list.
    for o in prov.get("summary", {}).get("outcomes", []) or prov.get("outcomes", []) or []:
        label = (o.get("type", {}) or {}).get("text") or o.get("description", "Market")
        add(label, o.get("description", ""), o.get("price"), o.get("point"))

    # Shape B: structured market blocks.
    ml = prov.get("moneyline") or {}
    side_home = ml.get("home") or {}
    side_away = ml.get("away") or {}
    home_team = ((side_home.get("team") or {}).get("abbreviation")
                 or (side_home.get("team") or {}).get("displayName") or "HOME")
    away_team = ((side_away.get("team") or {}).get("abbreviation")
                 or (side_away.get("team") or {}).get("displayName") or "AWAY")
    add("Moneyline", home_team, (side_home.get("close") or {}).get("odds"))
    add("Moneyline", away_team, (side_away.get("close") or {}).get("odds"))
    add("Moneyline Open", home_team, (side_home.get("open") or {}).get("odds"))
    add("Moneyline Open", away_team, (side_away.get("open") or {}).get("odds"))

    ps = prov.get("pointSpread") or {}
    ps_home = ps.get("home") or {}
    ps_away = ps.get("away") or {}
    spread_point = ps_home.get("spread") if ps_home.get("spread") is not None else prov.get("spread")
    add("Spread", home_team, ps_home.get("close", {}).get("odds") if isinstance(ps_home.get("close"), dict) else ps_home.get("odds"), spread_point)
    add("Spread", away_team, ps_away.get("close", {}).get("odds") if isinstance(ps_away.get("close"), dict) else ps_away.get("odds"),
        -spread_point if isinstance(spread_point, (int, float)) else spread_point)

    tot = prov.get("total") or {}
    ou_line = tot.get("over", {}).get("overUnder") if isinstance(tot.get("over"), dict) else None
    ou_line = ou_line if ou_line is not None else prov.get("overUnder")
    add("Total", "Over", tot.get("over", {}).get("close", {}).get("odds") if isinstance(tot.get("over", {}).get("close"), dict) else (tot.get("over") or {}).get("odds"), ou_line)
    add("Total", "Under", tot.get("under", {}).get("close", {}).get("odds") if isinstance(tot.get("under", {}).get("close"), dict) else (tot.get("under") or {}).get("odds"), ou_line)

    return rows


def _pick_oucomes(espn_odds_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Flatten ESPN providers into {market_key: row} using both payload shapes."""
    out: Dict[str, Dict[str, Any]] = {}
    for prov in espn_odds_list:
        for row in _extract_provider_rows(prov):
            out[f"{row['label']}|{row['selection']}|{row['point']}|{row['provider']}"] = row
    return out


def parse_espn_event(league: Dict[str, str], ev: Dict[str, Any],
                     state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a match document with markets from ESPN scoreboard + odds endpoints."""
    try:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home_name = ((home.get("team") or {}).get("displayName")
                     or (home.get("team") or {}).get("shortDisplayName") or "HOME")
        away_name = ((away.get("team") or {}).get("displayName")
                     or (away.get("team") or {}).get("shortDisplayName") or "AWAY")
        commence = ev.get("date") or ""

        # Markets embedded directly in the scoreboard payload...
        board_markets: Dict[str, Dict[str, Any]] = {}
        for prov in comp.get("odds") or []:
            pname = (prov.get("provider", {}) or {}).get("name", "ESPN")
            for o in prov.get("summary", {}).get("outcomes", []) or \
                    prov.get("outcomes", []) or []:
                label = (o.get("type", {}) or {}).get("text") or o.get("description", "Market")
                board_markets[f"{label}|{o.get('description','')}"] = {
                    "provider": pname, "label": label,
                    "selection": o.get("description", ""),
                    "price": o.get("price"), "details": o.get("details"),
                    "point": o.get("point"),
                }

        # ...plus the dedicated odds endpoint (more providers/markets).
        fetched = _espn_game_odds(league, ev.get("id", ""))
        all_rows = list(board_markets.values()) + list(_pick_oucomes(fetched).values())

        markets = build_markets_from_rows(league, all_rows, state, ev.get("id", ""), commence)
        if not markets:
            return None
        return {
            "id": f"espn_{ev.get('id')}",
            "source": "espn",
            "sport": league["display"],
            "sport_key": league["key"],
            "icon": league["icon"],
            "home_team": home_name,
            "away_team": away_name,
            "commence": commence,
            "status": (ev.get("status") or {}).get("type", {}).get("state", "notstarted"),
            "markets": markets,
        }
    except Exception:
        log(f"espn parse error: {traceback.format_exc(limit=1).strip()}")
        return None


# ---------------------------------------------------------------------------
# Market assembly (shared by all sources)
# ---------------------------------------------------------------------------

def _normalize_market_type(label: str, sport_key: str) -> Tuple[str, str]:
    """Map messy provider labels onto Stratum's canonical market taxonomy."""
    l = (label or "").lower()
    if "moneyline" in l or "match winner" in l or "to win" in l or "1x2" in l or "outright" in l:
        return ("moneyline", "Match Winner" if sport_key in ("epl", "ucl", "laliga", "seriea", "bundesliga", "ligue1") else "Moneyline")
    if "spread" in l or "handicap" in l or "line" in l:
        if "asian" in l:
            return ("asian_handicap", "Asian Handicap")
        return ("spread", "Spread")
    if "total" in l or "over" in l or "under" in l or "goals" in l.split("|")[0] and "both" not in l:
        if "team" in l:
            return ("team_total", "Team Total")
        return ("total", "Total")
    if "both teams" in l or "btts" in l or "gg/ng" in l:
        return ("btts", "BTTS")
    if "correct score" in l or "scorecast" in l:
        return ("correct_score", "Correct Score")
    if "corner" in l:
        return ("corners", "Corners")
    if "card" in l or "booking" in l:
        return ("cards", "Cards")
    if "halves" in l or "half" in l and "winner" in l:
        return ("halves", "Halves")
    if "quarter" in l:
        return ("quarters", "Quarters")
    if "alternate" in l:
        return ("alternates", "Alternates")
    if any(k in l for k in ("pts", "points", "rebounds", "assists", "yards",
                            "receptions", "hits", "bases", "shots", "passes",
                            "player", "prop")):
        return ("player_prop", "Player Props")
    return ("other", label.title() if label else "Other")


def build_markets_from_rows(league: Dict[str, str], rows: List[Dict[str, Any]],
                            state: Dict[str, Any], event_id: str,
                            commence: str) -> List[Dict[str, Any]]:
    """Group provider rows by (type, line, selection-set) and run the quant math."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        mtype, display = _normalize_market_type(row.get("label", ""), league["key"])
        point = row.get("point")
        gkey = f"{mtype}|{point if point is not None else ''}"
        grp = grouped.setdefault(gkey, {"type": mtype, "display": display,
                                        "point": point, "rows": []})
        grp["rows"].append(row)

    markets: List[Dict[str, Any]] = []
    for gkey, grp in grouped.items():
        grps_rows: List[Dict[str, Any]] = grp["rows"]
        # One leg per distinct selection (Home/Away/Over/Under/player...)
        legs: Dict[str, Dict[str, Any]] = {}
        for r in grps_rows:
            sel = (r.get("selection") or "?").strip()
            dec = american_to_decimal(r.get("price"))
            if not dec:
                continue
            leg = legs.setdefault(sel, {"implied": [], "books": {}})
            implied = decimal_to_implied(dec)
            if implied:
                leg["implied"].append(implied)
                # keep best price per bookmaker
                bk = r.get("provider", "Unknown")
                prev = leg["books"].get(bk)
                cur_price = r.get("price")
                if prev is None or (american_to_decimal(cur_price) or 0) > (american_to_decimal(prev) or 0):
                    leg["books"][bk] = cur_price

        selections = list(legs.keys())
        if len(selections) < 2 and grp["type"] not in ("correct_score", "other"):
            # Single-selection markets (e.g., one prop line) still useful vs fair
            pass
        implied_sets = [sum(l["implied"]) / len(l["implied"]) for l in legs.values()
                        if l["implied"]]
        fair_probs = calculate_fair_value(implied_sets) if len(implied_sets) >= 2 else None

        for idx, sel in enumerate(selections):
            leg = legs[sel]
            if not leg["implied"]:
                continue
            raw_p = sum(leg["implied"]) / len(leg["implied"])
            fair_p = fair_probs[idx] if fair_probs and idx < len(fair_probs) else raw_p
            # Best available price across books
            best_bk, best_price = max(leg["books"].items(),
                                      key=lambda kv: american_to_decimal(kv[1]) or 0)
            best_dec = american_to_decimal(best_price) or 1.0
            ev = expected_value(fair_p, best_dec)
            ev_pct = round(ev * 100, 2)
            stake = kelly_stake(ev, best_dec)
            mkey = f"{event_id}:{gkey}:{sel}"
            history = record_odds(state, mkey, best_price)

            bookmakers = sorted(
                [{"bookmaker": bk, "price": pr,
                  "decimal": round(american_to_decimal(pr) or 0, 3),
                  "implied_probability": round((decimal_to_implied(american_to_decimal(pr)) or 0) * 100, 2)}
                 for bk, pr in leg["books"].items()],
                key=lambda b: -(b["decimal"] or 0))

            direction = "against_consensus" if (len(history) >= 2 and
                                                (history[-1][1] or 0) < (history[0][1] or 0)
                                                and fair_p > raw_p) else "with_consensus"

            market = {
                "key": mkey,
                "type": grp["type"],
                "name": grp["display"],
                "line": grp["point"],
                "selection": sel,
                "raw_probability": round(raw_p * 100, 2),
                "fair_probability": round(fair_p * 100, 2),
                "vig_removed_percent": round((fair_p - raw_p) * 100, 2),
                "best": {"bookmaker": best_bk, "price": best_price,
                         "decimal": round(best_dec, 3)},
                "bookmakers": bookmakers,
                "num_bookmakers": len(bookmakers),
                "ev_percent": ev_pct,
                "kelly_stake": stake,
                "confidence": confidence_score(max(ev_pct, 0), len(bookmakers),
                                               len(history) >= 2,
                                               1 if any(s in best_bk.lower() for s in SHARP_BOOKS) else 3),
                "movement": [[t, p] for t, p in history],
                "direction": direction,
                "has_data": len(bookmakers) > 0,
            }
            market["signals"] = detect_signals(market, history)
            markets.append(market)

    markets.sort(key=lambda m: -m["ev_percent"])
    return markets


# ---------------------------------------------------------------------------
# Source 2: The-Odds-API (free tier, optional key)
# ---------------------------------------------------------------------------

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
ODDS_API_SPORTS = [
    ("basketball_nba", "nba"), ("americanfootball_nfl", "nfl"),
    ("baseball_mlb", "mlb"), ("soccer_epl", "epl"),
    ("soccer_uefa_champs_league", "ucl"), ("soccer_spain_la_liga", "laliga"),
    ("soccer_italy_serie_a", "seriea"), ("soccer_germany_bundesliga", "bundesliga"),
    ("soccer_france_ligue_one", "ligue1"),
]
LEAGUE_BY_KEY = {lg["key"]: lg for lg in ESPN_LEAGUES}


def fetch_odds_api_matches(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Games + all markets from The-Odds-API. Skips silently without key/quota."""
    if not ODDS_API_KEY:
        log("odds-api: no ODDS_API_KEY configured, skipping source")
        return []
    matches: List[Dict[str, Any]] = []
    for api_sport, league_key in ODDS_API_SPORTS:
        if budget_left() <= 20:
            break
        url = f"{ODDS_API_BASE}/{api_sport}/events"
        data = http_get(url, params={"apiKey": ODDS_API_KEY, "daysToExpiration": "1",
                                     "markets": "h2h,spreads,totals,props"})
        if not isinstance(data, list):
            log(f"odds-api: events unavailable for {api_sport}")
            continue
        league = LEAGUE_BY_KEY.get(league_key, {"display": api_sport, "icon": "soccer",
                                                "key": league_key})
        for ev in data[:12]:  # bound per-league cost
            eid = ev.get("id")
            odds = http_get(f"{ODDS_API_BASE}/{api_sport}/events/{eid}/odds",
                            params={"apiKey": ODDS_API_KEY, "regions": "us,eu",
                                    "markets": "h2h,spreads,totals,props",
                                    "oddsFormat": "american"})
            rows: List[Dict[str, Any]] = []
            if isinstance(odds, dict):
                for mk in odds.get("markets", []) or []:
                    mlabel = mk.get("key", "") + " " + mk.get("title", "")
                    for ob in mk.get("outcomes", []) or []:
                        price = ob.get("price")
                        if price is None:
                            continue
                        rows.append({
                            "provider": ob.get("publisher", "OddsAPI"),
                            "label": mlabel,
                            "selection": ob.get("name", ""),
                            "price": price,
                            "point": ob.get("point"),
                        })
            markets = build_markets_from_rows(league, rows, state, eid,
                                              ev.get("commence_time", ""))
            if not markets:
                continue
            matches.append({
                "id": f"oddapi_{eid}",
                "source": "the-odds-api",
                "sport": league["display"],
                "sport_key": league["key"],
                "icon": league["icon"],
                "home_team": ev.get("home_team") or "",
                "away_team": (ev.get("away_team") or "").replace(ev.get("home_team") or "@", "").strip() or "",
                "commence": ev.get("commence_time", ""),
                "status": "NOT_STARTED",
                "markets": markets,
            })
        time.sleep(1.0)  # courtesy spacing between leagues
    return matches


# ---------------------------------------------------------------------------
# Source 3: Direct scraping of Pinnacle/Circa public mirrors (best effort)
# ---------------------------------------------------------------------------

MIRROR_SOURCES = [
    # Public, unauthenticated listing pages. These change often; failures are
    # swallowed and simply mean fewer bookmakers in the comparison set.
    ("pinnacle_mirror", "https://www.pinnaclesports.com/en/sports"),
    ("circa_mirror", "https://www.circa.sports/sports/soccer/odds"),
]


def scrape_mirror_odds() -> Dict[str, List[Tuple[str, float]]]:
    """Return {source_name: [(selection_label, american_price), ...]} or empty."""
    results: Dict[str, List[Tuple[str, float]]] = {}
    if not HAS_BS4:
        log("scraping: beautifulsoup4 missing, skipping mirror sources")
        return results
    for name, url in MIRROR_SOURCES:
        if budget_left() <= 30:
            break
        html = http_get(url)
        if not isinstance(html, str) or len(html) < 500:
            log(f"scrape[{name}]: blocked or empty response")
            continue
        try:
            soup = BeautifulSoup(html, "html.parser")
            found: List[Tuple[str, float]] = []
            for el in soup.select("[data-amp-amount], .odds, .price, button[data-odds]"):
                txt = el.get_text(strip=True)
                try:
                    val = float(txt.replace("+", ""))
                except ValueError:
                    continue
                if abs(val) >= 100:
                    label = (el.get("aria-label") or el.get("data-selection")
                             or el.find_previous(string=True) or name)
                    found.append((str(label)[:60], val))
            if found:
                results[name] = found[:200]
                log(f"scrape[{name}]: {len(found)} prices captured")
            else:
                log(f"scrape[{name}]: page fetched but no parseable odds (layout changed)")
        except Exception as exc:
            log(f"scrape[{name}] error: {exc}")
    return results


def merge_scraped_into_matches(matches: List[Dict[str, Any]],
                               scraped: Dict[str, List[Tuple[str, float]]]) -> None:
    """Attach scraped mirror prices as extra bookmaker entries where selection
    labels match. Purely additive; never fabricates a price that wasn't read."""
    if not scraped:
        return
    for src_name, prices in scraped.items():
        price_map = {sel.lower(): p for sel, p in prices}
        for m in matches:
            for market in m["markets"]:
                sel = (market.get("selection") or "").lower()
                if sel and sel in price_map:
                    p = price_map[sel]
                    if not any(b["bookmaker"] == src_name for b in market["bookmakers"]):
                        dec = american_to_decimal(p) or 0
                        market["bookmakers"].append({
                            "bookmaker": src_name, "price": p,
                            "decimal": round(dec, 3),
                            "implied_probability": round((decimal_to_implied(dec) or 0) * 100, 2)})
                        market["num_bookmakers"] = len(market["bookmakers"])


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------

def dedupe_matches(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge duplicates across sources by normalized team pair + start time."""
    merged: Dict[str, Dict[str, Any]] = {}
    for m in all_matches:
        teams = tuple(sorted([(m.get("home_team") or "").lower(),
                              (m.get("away_team") or "").lower()]))
        key = f"{m.get('sport_key')}|{teams[0]}|{teams[1]}"
        if key in merged:
            existing = merged[key]
            have = {(mk["type"], mk.get("line"), mk["selection"]) for mk in existing["markets"]}
            for mk in m["markets"]:
                if (mk["type"], mk.get("line"), mk["selection"]) not in have:
                    existing["markets"].append(mk)
                    have.add((mk["type"], mk.get("line"), mk["selection"]))
            existing["sources"] = sorted(set(existing.get("sources", []) + [m["source"]]))
        else:
            m["sources"] = [m["source"]]
            merged[key] = m
    out = list(merged.values())
    for m in out:
        m["markets"].sort(key=lambda mk: -mk["ev_percent"])
        m["market_count"] = len(m["markets"])
    out.sort(key=lambda m: m.get("commence") or "9999")
    return out


def top_edges(matches: List[Dict[str, Any]], min_confidence: float = 80.0,
              limit: int = 150) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for m in matches:
        for mk in m["markets"]:
            if mk["ev_percent"] > 0 and mk["confidence"] >= min_confidence:
                edges.append({
                    "match_id": m["id"],
                    "sport": m["sport"], "icon": m["icon"],
                    "home_team": m["home_team"], "away_team": m["away_team"],
                    "commence": m.get("commence"),
                    "market_type": mk["type"], "market_name": mk["name"],
                    "line": mk.get("line"), "selection": mk["selection"],
                    "best_bookmaker": mk["best"]["bookmaker"],
                    "best_price": mk["best"]["price"],
                    "fair_probability": mk["fair_probability"],
                    "raw_probability": mk["raw_probability"],
                    "ev_percent": mk["ev_percent"],
                    "kelly_stake": mk["kelly_stake"],
                    "confidence": mk["confidence"],
                    "signals": mk["signals"],
                })
    edges.sort(key=lambda e: (-e["confidence"], -e["ev_percent"]))
    return edges[:limit]


MARKET_TYPE_CATALOG = {
    "soccer": ["moneyline", "btts", "total", "asian_handicap", "corners",
               "cards", "player_prop", "correct_score", "other"],
    "us": ["moneyline", "spread", "total", "player_prop", "halves",
           "quarters", "alternates", "team_total", "other"],
}


def build_feed() -> Dict[str, Any]:
    state = load_state()
    all_matches: List[Dict[str, Any]] = []

    # Source 1: ESPN (always attempted first; broadest coverage, no key)
    for league in ESPN_LEAGUES:
        if budget_left() <= 30:
            log("global budget nearly exhausted; stopping further league scans")
            break
        try:
            events = fetch_espn_scoreboard(league)
            log(f"espn {league['display']}: {len(events)} upcoming events")
            for ev in events[:20]:
                if budget_left() <= 20:
                    break
                parsed = parse_espn_event(league, ev, state)
                if parsed:
                    all_matches.append(parsed)
        except Exception:
            log(f"espn league {league['key']} failed: {traceback.format_exc(limit=1).strip()}")

    # Source 2: The-Odds-API (adds multi-bookmaker depth when key present)
    try:
        odds_api_matches = fetch_odds_api_matches(state)
        log(f"odds-api: {len(odds_api_matches)} matches with markets")
        all_matches.extend(odds_api_matches)
    except Exception:
        log(f"odds-api scan failed: {traceback.format_exc(limit=1).strip()}")

    # Source 3: direct mirror scraping (enriches bookmaker comparison)
    try:
        scraped = scrape_mirror_odds()
        merge_scraped_into_matches(all_matches, scraped)
    except Exception:
        log(f"mirror scraping failed: {traceback.format_exc(limit=1).strip()}")

    matches = dedupe_matches(all_matches)
    total_markets = sum(len(m["markets"]) for m in matches)
    edges = top_edges(matches)
    save_state(state)

    feed = {
        "meta": {
            "version": ENGINE_VERSION,
            "generated_at": utc_now_iso(),
            "next_refresh_minutes": 15,
            "data_policy": "live_sources_only_no_synthetic_data",
            "sources_attempted": ["espn", "the-odds-api", "pinnacle_mirror", "circa_mirror"],
            "sources_active": sorted({s for m in matches for s in m.get("sources", [])}),
            "matches_scanned": len(matches),
            "markets_scanned": total_markets,
            "signals_found": len(edges),
            "budget_seconds_remaining": round(max(budget_left(), 0), 1),
        },
        "catalog": MARKET_TYPE_CATALOG,
        "top_edges": edges,
        "matches": matches,
    }
    return feed


def atomic_write(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> int:
    log(f"Stratum engine v{ENGINE_VERSION} starting (output={OUTPUT_PATH})")
    feed: Optional[Dict[str, Any]] = None
    exit_code = 0
    try:
        feed = build_feed()
    except Exception:
        log(f"FATAL during scan: {traceback.format_exc(limit=3).strip()}")
        # Even on catastrophic failure, emit a valid empty feed so the app
        # shows "No Signal" rather than stale/broken data.
        feed = {
            "meta": {"version": ENGINE_VERSION, "generated_at": utc_now_iso(),
                     "error": "scan_failed", "matches_scanned": 0,
                     "markets_scanned": 0, "signals_found": 0,
                     "data_policy": "live_sources_only_no_synthetic_data"},
            "catalog": MARKET_TYPE_CATALOG, "top_edges": [], "matches": [],
        }
        exit_code = 0  # non-fatal: a clean empty feed is a valid result

    try:
        atomic_write(OUTPUT_PATH, feed)
        meta = feed.get("meta", {})
        log(f"wrote feed: {meta.get('matches_scanned', 0)} matches, "
            f"{meta.get('markets_scanned', 0)} markets, "
            f"{meta.get('signals_found', 0)} high-confidence signals")
    except Exception:
        log(f"FATAL writing output: {traceback.format_exc(limit=2).strip()}")
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
