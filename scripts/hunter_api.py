#!/usr/bin/env python3
"""Hunter Mode trigger -- real-time, query-scoped market scan.

Unlike the cron-fed engine (scripts/engine.py) this script NEVER waits for a
schedule and NEVER loads pre-baked data. One invocation = one hunt for ONE
match query across multiple sources:

    1. Normalize the query ("al hilal vs al nassr" -> team tokens).
    2. Parallel scrape targets: Betika.co.ke, Odibets.co.ke, Flashscore.com
       (+ ESPN public scoreboard JSON as a genuine cross-book price mirror so
       edge detection always has >= 2 independent books when the match is
       listed there).
    3. Extract every available market: 1X2, Asian Handicap, BTTS/Glory,
       Over/Under goals, corners, cards, player props.
    4. Pass raw decimal prices to the quant layer (src/analyzer.py -- the
     "quant_engine": vig removal, fair odds, EV %, Kelly, confidence,
     steam/movement signals).
    5. Emit the top picks sorted by Edge %.

INTEGRITY RULES (hard):
    - NO FAKE DATA. If a source returns nothing parseable, it contributes an
      empty list. Odds are only ever emitted if a real page/JSON response
      contained them. Nothing is synthesized, rounded-up, or hallucinated.
    - TIMEOUT GUARD: every HTTP request is hard-killed at SOURCE_TIMEOUT_S
      (default 10s); each scraper additionally runs inside a worker thread
      joined with a deadline so a hung socket can never stall the hunt.
    - RATE LIMITING: >= RATE_LIMIT_S (default 1.0s) between requests to the
      same domain, process-wide.

Usage (CLI -- designed for GitHub Actions workflow_dispatch):
    python3 scripts/hunter_api.py --query "Al Hilal vs Al Nassr" \
        --sport soccer --output hunt-result.json

Usage (local dev server, lets the PWA hunt without CI round-trips):
    python3 scripts/hunter_api.py serve --port 8787
    GET http://127.0.0.1:8787/hunt?query=Al+Hilal+vs+Al+Nassr&sport=soccer

Output JSON schema:
    {
      "status": "success" | "no_results",
      "query": "...", "sport": "soccer",
      "generated_at": "ISO8601",
      "sources": {"betika": "ok|empty|blocked|error|timeout", ...},
      "markets_scanned": N,
      "picks": [ {market fields incl. edge_percent, ev_percent, ...} ],
      "edges": [ same list, alias required by the frontend contract ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    print("[hunt] FATAL: requests is not installed", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from analyzer import analyze_rows  # noqa: E402  (the quant engine)

HUNTER_API_VERSION = "1.0.0"

# --- Constraints from the spec -------------------------------------------------
SOURCE_TIMEOUT_S = float(os.environ.get("HUNT_TIMEOUT_S", "10"))   # kill >10s
RATE_LIMIT_S = float(os.environ.get("HUNT_RATE_LIMIT_S", "1.0"))   # 1s/domain
SCRAPER_DEADLINE_S = float(os.environ.get("HUNT_SCRAPER_DEADLINE_S", "25"))
MAX_PICKS = int(os.environ.get("HUNT_MAX_PICKS", "5"))

USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
]

_T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[hunt {time.monotonic() - _T0:5.1f}s] {msg}", flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Rate limiter: minimum RATE_LIMIT_S between requests to the SAME domain.
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = self.min_interval - (now - last)
            # Reserve the slot while holding the lock so parallel workers
            # serialize per-domain instead of stampeding.
            self._last[host] = max(now, last) + self.min_interval
        if delay > 0:
            time.sleep(delay)


_LIMITER = DomainRateLimiter(RATE_LIMIT_S)


class FetchError(Exception):
    """Typed HTTP fetch failure so callers can distinguish WAF blocks, dead
    routes and timeouts instead of collapsing everything into 'empty'."""

    def __init__(self, kind: str, url: str, status: int = 0):
        super().__init__(f"{kind} for {url}")
        self.kind = kind          # blocked_waf | not_found | timeout | error
        self.url = url
        self.status = status


# ESPN's Akamai front door fingerprints the *browser-style* header set
# (HTML-first Accept + Accept-Language) that we use for bookmaker pages and
# answers it with HTTP 403 -- while identical curl/requests calls carrying
# ONLY `Accept: application/json` (or no extra headers at all) get 200.
# Verified live during the Austria-vs-Kosovo diagnostic: same URL, same IP,
# same second: browser headers -> 403, minimal JSON headers -> 200.
JSON_HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Accept": "application/json",
}


def http_get(url: str, timeout: float = SOURCE_TIMEOUT_S,
             headers: Optional[Dict[str, str]] = None,
             strict: bool = False) -> Optional[requests.Response]:
    """Single-shot GET with per-domain rate limiting and a hard timeout.

    Default mode returns None on failure (legacy behaviour). With
    ``strict=True`` a typed FetchError is raised instead so scouts can label
    sources BLOCKED_BY_WAF / TIMEOUT_ON_LOAD / NOT_FOUND rather than a
    generic 'empty'. Pass ``headers=JSON_HEADERS`` for JSON APIs whose WAF
    rejects browser-style headers (see note above)."""
    _LIMITER.wait(url)
    if headers is not None:
        hdrs = dict(headers)   # exact control for API endpoints
    else:
        hdrs = {
            "User-Agent": USER_AGENTS[0],
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": ("text/html,application/xhtml+xml,application/json;"
                       "q=0.9,*/*;q=0.8"),
        }
    try:
        resp = requests.get(url, timeout=timeout, headers=hdrs)
        if resp.status_code == 200:
            return resp
        if resp.status_code in (401, 403, 429):
            log(f"HTTP {resp.status_code} [BLOCKED_BY_WAF] {url}")
            if strict:
                raise FetchError("blocked_waf", url, resp.status_code)
        elif resp.status_code in (404, 410):
            log(f"HTTP {resp.status_code} [NOT_FOUND] {url}")
            if strict:
                raise FetchError("not_found", url, resp.status_code)
        else:
            log(f"HTTP {resp.status_code} {url}")
            if strict:
                raise FetchError("error", url, resp.status_code)
    except requests.exceptions.Timeout:
        log(f"TIMEOUT (> {timeout:.0f}s, killed) [TIMEOUT_ON_LOAD] {url}")
        if strict:
            raise FetchError("timeout", url)
    except FetchError:
        raise
    except Exception as exc:
        log(f"fetch error {url}: {type(exc).__name__}")
        if strict:
            raise FetchError("error", url)
    return None


# ---------------------------------------------------------------------------
# Query normalization
# ---------------------------------------------------------------------------

_STOPWORDS = {"vs", "v", "-", "the", "fc", "sc", "cf", "afc", "club", "de"}


def normalize_query(query: str) -> List[str]:
    """'Al Hilal vs Al Nassr' -> ['hilal', 'nassr'] (team-distinctive tokens)."""
    text = unicodedata.normalize("NFKD", query or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
    distinctive = [t for t in tokens if t not in _STOPWORDS and len(t) > 1]
    return distinctive or tokens


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def matches_query(text: str, tokens: List[str]) -> bool:
    """A listing matches the hunt if ALL distinctive tokens appear in it."""
    hay = _norm_ws(text)
    return all(tok in hay for tok in tokens)


# ---------------------------------------------------------------------------
# Decimal-odds parsing helpers (shared by all scrapers)
# ---------------------------------------------------------------------------

_DEC_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3})?$")


def parse_decimal_odds(raw: str) -> Optional[float]:
    """Parse a decimal odds token ('1.85', '13.0'). Returns None if the value
    is not a plausible decimal price -- we never coerce/guess prices."""
    s = (raw or "").strip().replace(",", ".")
    if not _DEC_RE.match(s):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if 1.01 <= val <= 1000.0:
        return round(val, 3)
    return None


# ---------------------------------------------------------------------------
# Scraper 1: Betika.co.ke (Kenyan bookmaker public board)
# ---------------------------------------------------------------------------

USE_BROWSER_FALLBACK = (os.getenv("STRATUM_ENABLE_BROWSER_SCRAPING",
                                  "false").lower() == "true")

# SPA-shell detector: Betika/Odibets boards are Vue apps served as a tiny
# prerender shell (<div id=app> + webpack bundles); decimal odds only exist
# after JS hydration, so static HTML parsing can NEVER see them. Verified
# live: betika soccer board = HTTP 200, 7.4 KB, zero odd strings.
_SPA_SHELL_RE = re.compile(r'<div[^>]+id=["\']?app["\']?', re.I)


def _is_spa_shell(html: str) -> bool:
    return len(html or "") < 60_000 and bool(_SPA_SHELL_RE.search(html or ""))


def scrape_betika(tokens: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    """STEP-2 FINDING (Austria-vs-Kosovo repair): no hidden JSON endpoint is
    reachable without a browser. Every guessed /api/* route on betika.com
    returns the SAME 7,422-byte SPA shell (soft-404 to text/html), and the
    hashed webpack bundles referenced by the prerenderer are also replaced by
    that shell for non-browser clients -- there is nothing to reverse-engineer
    server-side. Marked DEFERRED_TO_PLAYWRIGHT_PHASE_B.

    Statuses now distinguish failure modes explicitly:
      blocked_waf | timeout | spa_shell_deferred_to_pw | empty | ok."""
    urls = [
        "https://www.betika.com/en-int/sport/soccer",
        "https://betika.com/en/sport/football",
    ]
    last_status = "blocked_waf"
    for url in urls:
        try:
            resp = http_get(url, strict=True)
        except FetchError as fe:
            last_status = fe.kind
            continue
        if resp is None:
            last_status = "error"
            continue
        html = resp.text
        if _looks_blocked(html):
            log("betika: anti-bot challenge page -> BLOCKED_BY_WAF")
            last_status = "blocked_waf"
            continue
        if _is_spa_shell(html):
            log("betika: JS-rendered SPA shell (no odds in static HTML) "
                "-> DEFERRED_TO_PLAYWRIGHT_PHASE_B")
            last_status = "spa_shell_deferred_to_pw"
            continue
        if not HAS_BS4:
            return [], "error"
        rows = _parse_book_html(resp, tokens, "Betika")
        return (rows, "ok") if rows else ([], "empty")
    return [], last_status


# ---------------------------------------------------------------------------
# Scraper 2: Odibets.com (Kenyan bookmaker public board)
# ---------------------------------------------------------------------------

def scrape_odibets(tokens: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Same situation as Betika: `<div id=app>` Vue shell, odds hydrated
    client-side; `/today` additionally 404s. DEFERRED_TO_PLAYWRIGHT_PHASE_B."""
    urls = [
        "https://odibets.com/league/117-INT-Friendly-Club",
        "https://odibets.com/today",
    ]
    last_status = "blocked_waf"
    for url in urls:
        try:
            resp = http_get(url, strict=True)
        except FetchError as fe:
            last_status = fe.kind
            continue
        if resp is None:
            last_status = "error"
            continue
        if _looks_blocked(resp.text):
            log("odibets: anti-bot challenge page -> BLOCKED_BY_WAF")
            last_status = "blocked_waf"
            continue
        if _is_spa_shell(resp.text):
            log("odibets: JS-rendered SPA shell (no odds in static HTML) "
                "-> DEFERRED_TO_PLAYWRIGHT_PHASE_B")
            last_status = "spa_shell_deferred_to_pw"
            continue
        if not HAS_BS4:
            return [], "error"
        rows = _parse_book_html(resp, tokens, "Odibets")
        return (rows, "ok") if rows else ([], "empty")
    return [], last_status


def _looks_blocked(html: str) -> bool:
    head = (html or "")[:4000].lower()
    markers = ("just a moment", "cf-challenge", "attention required",
               "enable javascript and cookies", "access denied",
               "ray id", "checking your browser")
    return any(m in head for m in markers)


def _parse_book_html(resp: requests.Response, tokens: List[str],
                     book: str) -> List[Dict[str, Any]]:
    """Generic Kenyan-book HTML parse: find event blocks whose heading
    contains every query token, then harvest selection/decimal-price pairs
    across all visible markets (1X2, AH, BTTS, totals, corners, cards)."""
    soup = BeautifulSoup(resp.text, "lxml")
    rows: List[Dict[str, Any]] = []

    # Market type inference from surrounding section/element labels.
    def infer_market(context_el) -> Tuple[str, str, Optional[float]]:
        label = ""
        el = context_el
        for _ in range(6):
            if el is None:
                break
            label = _norm_ws(el.get_text(" ", strip=True))[:400]
            if any(k in label for k in ("handicap", "ah")):
                break
            el = el.parent
        mtype, name = "moneyline", f"{book} Match Winner (1X2)"
        line = None
        low = label
        if "handicap" in low or re.search(r"\bah\b", low):
            mtype, name = "spread", "Asian Handicap"
        elif "both teams to score" in low or "glory" in low or "btts" in low:
            mtype, name = "btts", "Both Teams To Score"
        elif "corners" in low:
            mtype, name = "total_corners", "Total Corners"
        elif "cards" in low:
            mtype, name = "total_cards", "Total Cards"
        elif "over" in low and "under" in low:
            mtype, name = "total", "Over/Under Goals"
        m = re.search(r"(?:over|under|handicap|total)\s*([+-]?\d+(?:\.\d+)?)", low)
        if m and mtype in ("spread", "total", "total_corners", "total_cards"):
            line = float(m.group(1))
        return mtype, name, line

    # Walk elements that look like event containers.
    for el in soup.find_all(["div", "section", "article"]):
        classes = " ".join(el.get("class", []))
        if not re.search(r"event|match|game|selection|odd", classes, re.I):
            continue
        text = el.get_text(" ", strip=True)
        if not matches_query(text, tokens):
            continue
        mtype, name, line = infer_market(el)
        # Selection buttons: short text ending in a decimal price.
        for btn in el.find_all(["button", "a", "span"]):
            bt = _norm_ws(btn.get_text(" ", strip=True))
            m = re.match(r"^([a-z0-9 .'+_-]{1,40}?)\s+(\d{1,3}\.\d{1,2})$", bt)
            if not m:
                continue
            sel_raw, odds_raw = m.group(1), m.group(2)
            dec = parse_decimal_odds(odds_raw)
            if dec is None:
                continue
            sel = _clean_selection(sel_raw, book)
            if sel is None:
                continue
            rows.append({"source": book, "market": mtype, "name": name,
                         "selection": sel, "line": line, "odds": dec})
        if rows:
            break  # first matching event block is the hunted match
    return rows


def _clean_selection(raw: str, book: str) -> Optional[str]:
    s = _norm_ws(raw)
    if not s or len(s) > 40:
        return None
    mapping = {
        "1": "Home", "x": "Draw", "2": "Away",
        "home": "Home", "draw": "Draw", "away": "Away",
        "yes": "Yes", "no": "No",
        "over": "Over", "under": "Under",
        "btts yes": "Yes", "btts no": "No",
        "odd": "Odd", "even": "Even",
    }
    if s in mapping:
        return mapping[s]
    m = re.match(r"^(over|under)\s*([+-]?\d+(?:\.\d+)?)$", s)
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    m = re.match(r"^(\d+)\s*(?:\+\d+(?:\.\d+)?|\-\d+(?:\.\d+)?)\s*$", s)
    if m:
        return {"1": "Home", "2": "Away"}.get(m.group(1))
    # Team-name selections are kept verbatim-cased below.
    if re.fullmatch(r"[a-z0-9 .'-]+", s):
        return raw.strip().title()
    return None


# ---------------------------------------------------------------------------
# Scraper 3: Flashscore.com (odds mirror pages)
# ---------------------------------------------------------------------------

def scrape_flashscore(tokens: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Flashscore renders live data via JS/WebSocket, but its static HTML
    still ships bookmaker odds mirrors on match preview pages. We search the
    public site for the fixture and parse whatever REAL prices come back.
    Typed failure statuses: blocked_waf / not_found / timeout / empty."""
    try:
        search = http_get(f"https://www.flashscore.com/search/?searchTerm="
                          f"{quote_plus(' '.join(tokens))}", strict=True)
    except FetchError as fe:
        return [], fe.kind
    if search is None:
        return [], "error"
    soup = BeautifulSoup(search.text, "lxml") if HAS_BS4 else None
    href = None
    if soup:
        for a in soup.find_all("a", href=True):
            if matches_query(a.get_text(" ", strip=True), tokens) and "/match/" in a["href"]:
                href = "https://www.flashscore.com" + a["href"].split("#")[0]
                break
    if not href:
        return [], "empty"
    try:
        page = http_get(href, strict=True)
    except FetchError as fe:
        return [], fe.kind
    if page is None:
        return [], "error"
    rows: List[Dict[str, Any]] = []
    if HAS_BS4:
        psoup = BeautifulSoup(page.text, "lxml")
        for el in psoup.find_all(attrs={"class": re.compile("odds|Odds", re.I)}):
            for btn in el.find_all(["button", "a", "span", "td"]):
                bt = _norm_ws(btn.get_text(" ", strip=True))
                m = re.match(r"^([a-z0-9 .'+_-]{1,40}?)\s+(\d{1,3}\.\d{1,2})$", bt)
                if m:
                    dec = parse_decimal_odds(m.group(2))
                    sel = _clean_selection(m.group(1), "Flashscore")
                    if dec and sel:
                        rows.append({"source": "Flashscore", "market": "moneyline",
                                     "name": "Match Winner (1X2)",
                                     "selection": sel, "line": None, "odds": dec})
    return (rows, "ok") if rows else ([], "empty")


# ---------------------------------------------------------------------------
# Cross-book mirror: ESPN public scoreboard JSON (genuine bookmaker prices)
# ---------------------------------------------------------------------------

# ESPN public scoreboard leagues (keyless, genuine bookmaker prices). The
# league list is DISCOVERED at runtime from the ESPN sports endpoint so new
# competitions (e.g. the Saudi league for Al Hilal/Al Nassr) are covered
# without hardcoding -- we never invent fixtures, we only look them up.
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_espn_leagues_cache: List[str] = []

# Continental competitions where non-domestic fixtures (nations cups, WC
# qualifiers, friendlies like Austria vs Kosovo) live. Appended to the
# discovered list and used as the fallback set when league discovery itself
# is WAF-blocked -- previously these were missing entirely, so international
# matches could never be found even with a healthy connection.
ESPN_CONTINENTAL_LEAGUES = ["uefa.nations", "fifa.worldq", "uefa.euroq",
                            "caf.champions", "concacaf.league"]


def _espn_get(url: str) -> Optional[requests.Response]:
    """GET an ESPN public API endpoint with headers Akamai accepts.

    IMPORTANT (Austria-vs-Kosovo repair): the default browser-style header
    set in http_get() (HTML-first Accept + Accept-Language) is fingerprinted
    by ESPN's Akamai WAF and answered with HTTP 403 from datacenter IPs,
    while the same client with ONLY `Accept: application/json` gets 200.
    So we pass the minimal JSON_HEADERS verbatim here."""
    return http_get(url, headers=JSON_HEADERS)


def espn_soccer_leagues() -> List[str]:
    global _espn_leagues_cache
    if _espn_leagues_cache:
        return _espn_leagues_cache
    resp = _espn_get(_ESPN_BASE)
    leagues: List[str] = []
    if resp is not None:
        try:
            for lg in resp.json().get("leagues", []):
                key = lg.get("key")
                if key:
                    leagues.append(key)
        except Exception:
            leagues = []
    # Continental keys FIRST: internationals are exactly what the domestic
    # discovery list kept missing. Dedupe, keep order.
    ordered: List[str] = []
    for key in ESPN_CONTINENTAL_LEAGUES + leagues:
        if key not in ordered:
            ordered.append(key)
    if len(ordered) <= len(ESPN_CONTINENTAL_LEAGUES):
        log("espn_mirror: league discovery unavailable [BLOCKED_BY_WAF?] "
            "-> using continental+major fallback set")
        ordered += ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1",
                    "uefa.champions", "uefa.europa", "ksa.1"]
    _espn_leagues_cache = ordered[:28]
    return _espn_leagues_cache


def scrape_espn_mirror(tokens: List[str], sport: str) -> Tuple[List[Dict[str, Any]], str]:
    """Not a scraping target per se, but a real, keyless price feed used to
    guarantee multi-book handles for the quant layer. Same integrity rule:
    only prices actually present in the JSON become rows.

    Failure modes are reported distinctly (never collapsed into 'empty'):
      blocked_waf        -- ESPN refused us (header/IP fingerprinting)
      timeout            -- board fetch exceeded SOURCE_TIMEOUT_S
      no_fixture         -- boards fetched fine, match not listed anywhere
      fixture_no_odds    -- fixture located but bookmaker lines absent/expired
      ok                 -- genuine prices harvested"""
    if sport not in ("soccer", "auto"):
        return [], "skipped"
    rows: List[Dict[str, Any]] = []
    hit = False
    saw_blocked = saw_timeout = False
    for league in espn_soccer_leagues():
        url = f"{_ESPN_BASE}/{league}/scoreboard?limit=100"
        try:
            resp = http_get(url, headers=JSON_HEADERS, strict=True)
        except FetchError as fe:
            if fe.kind == "blocked_waf":
                saw_blocked = True
            elif fe.kind == "timeout":
                saw_timeout = True
            continue
        if resp is None:
            continue
        try:
            events = resp.json().get("events", [])
        except Exception:
            continue
        for ev in events:
            name = (f"{ev.get('name', '')} {ev.get('shortName', '')}").lower()
            competitors = (ev.get("competitions") or [{}])[0]
            for team in competitors.get("competitors", []):
                name += " " + str(team.get("team", {}).get("displayName", ""))
            if not matches_query(name, tokens):
                continue
            hit = True
            rows.extend(_espn_event_rows(ev))
        if hit:
            break
    if rows:
        return rows, "ok"
    if hit:
        return [], "fixture_no_odds"
    if saw_blocked and not saw_timeout:
        return [], "blocked_waf"
    if saw_timeout:
        return [], "timeout"
    return [], "no_fixture"


def _as_american(v: Any) -> Optional[float]:
    """ESPN betting payloads mix floats (-360) with strings ('+135')."""
    try:
        f = float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None
    return f


def _line_float(v: Any) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", str(v if v is not None else ""))
    return float(m.group(0)) if m else None


def _espn_event_rows(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Harvest every REAL price from an ESPN scoreboard event.

    Two payload shapes exist in the wild and both are handled:
      * legacy flat keys: homeOpeningMoneyLine / overOpeningPrice / ...
      * current nested objects: moneyline.home.close.odds = "-360",
        total.over.close.{line,odds}, pointSpread.home.close.{line,odds},
        drawOdds.moneyLine  (verified live on uefa.champions boards; the old
        flat-key reader found ZERO prices there -- root cause of silent
        single-source hunts even when ESPN answered 200)."""
    rows: List[Dict[str, Any]] = []
    comp = (ev.get("competitions") or [{}])[0]
    for odd in comp.get("odds") or []:
        if not isinstance(odd, dict):
            continue   # ESPN emits `odds:[null]` once lines expire
        provider = (odd.get("provider") or {}).get("name", "ESPN-Mirror")
        details = _norm_ws(str(odd.get("details") or ""))

        ml = odd.get("moneyline") or {}
        home_ml = (_as_american(((ml.get("home") or {}).get("close") or {})
                                .get("odds"))
                   or _as_american(((ml.get("home") or {}).get("open") or {})
                                   .get("odds")))
        away_ml = (_as_american(((ml.get("away") or {}).get("close") or {})
                                .get("odds"))
                   or _as_american(((ml.get("away") or {}).get("open") or {})
                                   .get("odds")))
        draw_ml = _as_american((odd.get("drawOdds") or {}).get("moneyLine"))
        # legacy flat keys still honoured when present
        home_ml = home_ml or _as_american(odd.get("homeOpeningMoneyLine")
                                          or odd.get("homeMoneyLine"))
        away_ml = away_ml or _as_american(odd.get("awayOpeningMoneyLine")
                                          or odd.get("awayMoneyLine"))
        draw_ml = draw_ml or _as_american(odd.get("drawMoneyLine"))
        if home_ml:
            rows.extend(_american_pair(provider, "Home", home_ml))
        if away_ml:
            rows.extend(_american_pair(provider, "Away", away_ml))
        if draw_ml:
            rows.extend(_american_pair(provider, "Draw", draw_ml))

        tot = odd.get("total") or {}
        line = _line_float(tot.get("over", {}).get("close", {})
                           .get("line") if isinstance(tot.get("over"), dict)
                           else None) \
            or _line_float((tot.get("under") or {}).get("close", {})
                           .get("line") if isinstance(tot.get("under"), dict)
                           else None) \
            or _as_american(odd.get("overUnder"))
        ov = _as_american(((tot.get("over") or {}).get("close") or {})
                          .get("odds")) \
            or _as_american(((tot.get("over") or {}).get("open") or {})
                            .get("odds")) \
            or _as_american(odd.get("overOpeningPrice")
                            or odd.get("overCurrentPrice"))
        un = _as_american(((tot.get("under") or {}).get("close") or {})
                          .get("odds")) \
            or _as_american(((tot.get("under") or {}).get("open") or {})
                            .get("odds")) \
            or _as_american(odd.get("underOpeningPrice")
                            or odd.get("underCurrentPrice"))
        if line and ov:
            rows.append({"source": provider, "market": "total",
                         "name": "Over/Under Goals",
                         "selection": f"Over {line}", "line": line,
                         "odds": _to_decimal(ov)})
        if line and un:
            rows.append({"source": provider, "market": "total",
                         "name": "Over/Under Goals",
                         "selection": f"Under {line}", "line": line,
                         "odds": _to_decimal(un)})

        ps = odd.get("pointSpread") or {}
        for side, label in (("home", "Home"), ("away", "Away")):
            close = (ps.get(side) or {}).get("close") or {}
            opne = (ps.get(side) or {}).get("open") or {}
            ah = _as_american(close.get("odds")) or _as_american(opne.get("odds"))
            ah_line = _line_float(close.get("line")) \
                or _line_float(opne.get("line"))
            if ah and ah_line is not None:
                rows.extend(_american_pair(
                    provider, f"{label} {ah_line}", ah,
                    market="spread", name="Asian Handicap", line=ah_line))
        # last-ditch legacy AH keys
        ah_legacy = _as_american(odd.get("homeOpeningAgainstTheSpreadOdds"))
        ah_line_legacy = _as_american(odd.get("againstTheSpreadOpeningLine"))
        if ah_legacy and ah_line_legacy is not None and \
                not any(r["market"] == "spread" for r in rows):
            rows.extend(_american_pair(provider,
                                       f"Home {ah_line_legacy}", ah_legacy,
                                       market="spread",
                                       name="Asian Handicap",
                                       line=float(ah_line_legacy)))
    return [r for r in rows if r.get("odds") and r["odds"] > 1.0]


def _american_pair(provider: str, selection: str, american: Any,
                   market: str = "moneyline",
                   name: str = "Match Winner (1X2)",
                   line: Optional[float] = None) -> List[Dict[str, Any]]:
    try:
        am = float(american)
    except (TypeError, ValueError):
        return []
    return [{"source": provider, "market": market, "name": name,
             "selection": selection, "line": line, "odds": _to_decimal(am)}]


def _to_decimal(price: float) -> float:
    p = float(price)
    if p >= 1.01 and p <= 1000 and not float(p).is_integer():
        return round(p, 3)  # already decimal
    if p >= 1.0 and p <= 1.01:
        return round(p, 3)
    if abs(p) < 100 and "." in str(price):
        return round(p, 3)
    if p > 0:
        return round(1 + p / 100.0, 3)
    return round(1 + 100.0 / abs(p), 3)


# ---------------------------------------------------------------------------
# Parallel hunt orchestration with per-source hard deadlines
# ---------------------------------------------------------------------------

def run_hunt(query: str, sport: str = "auto",
             on_source_done=None) -> Dict[str, Any]:
    """Execute one query-scoped hunt.

    `on_source_done(name, row_count, status)` is an optional live callback
    invoked from worker threads as each source finishes — used by the V3.0
    agent swarm to stream SCOUT progress to the console UI. It must be
    thread-safe and never raise; failures are swallowed.
    """
    tokens = normalize_query(query)
    log(f"hunting '{query}' -> tokens {tokens} (sport={sport})")

    def _notify(name: str, n: int, status: str) -> None:
        if on_source_done is None:
            return
        try:
            on_source_done(name, n, status)
        except Exception:
            pass

    jobs = {
        "betika": lambda: scrape_betika(tokens),
        "odibets": lambda: scrape_odibets(tokens),
        "flashscore": lambda: scrape_flashscore(tokens),
        "espn_mirror": lambda: scrape_espn_mirror(tokens, sport),
    }
    results: Dict[str, Tuple[List[Dict[str, Any]], str]] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        deadline = SCRAPER_DEADLINE_S
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=deadline)
            except TimeoutError:
                log(f"{name}: exceeded {deadline:.0f}s deadline -> killed, 0 rows")
                results[name] = ([], "timeout")
            except Exception as exc:
                log(f"{name}: crashed ({type(exc).__name__}) -> 0 rows")
                results[name] = ([], "error")

    raw_rows: List[Dict[str, Any]] = []
    statuses: Dict[str, str] = {}
    for name, (rows, status) in results.items():
        statuses[name] = status
        raw_rows.extend(rows)
        log(f"{name}: {len(rows)} raw price rows [{status}]")
        _notify(name, len(rows), status)

    # Quant engine: vig removal, fair odds, EV, Kelly, confidence, signals.
    state: Dict[str, Any] = {"odds_history": {}, "last_run": None}
    event_id = re.sub(r"[^a-z0-9]+", "-", _norm_ws(query)).strip("-") or "hunt"
    markets = analyze_rows(raw_rows, state, event_id) if raw_rows else []

    # Top picks by edge (EV %), positive-edge first, capped at MAX_PICKS.
    picks = sorted(markets, key=lambda m: (-m["ev_percent"], -m["confidence"]))
    top = picks[:MAX_PICKS] if any(m["ev_percent"] > 0 for m in picks) else []

    edges = [_shape_edge(m, query) for m in top]
    status = "success" if edges else "no_results"
    payload = {
        "status": status,
        "version": HUNTER_API_VERSION,
        "query": query,
        "normalized_tokens": tokens,
        "sport": sport,
        "generated_at": utc_now_iso(),
        "sources": statuses,
        "raw_prices_seen": len(raw_rows),
        "markets_scanned": len(markets),
        "picks": top,
        "edges": edges,
        # "_raw_rows" is an internal hand-off consumed by the V3.0 agent swarm
        # (Actuary re-runs the quant engine on it, Scout pops it before emit).
        "_raw_rows": raw_rows,
        "data_policy": "real scraped prices only; empty result means no data, "
                       "never fabricated odds",
    }
    log(f"done: {len(edges)} edges over {len(markets)} analyzed markets")
    return payload


def _shape_edge(m: Dict[str, Any], query: str) -> Dict[str, Any]:
    """Flatten an analyzed market into the frontend TopEdge contract."""
    parts = _norm_ws(query).replace(" vs ", "|").split("|")
    home = parts[0].strip().title() if len(parts) >= 1 else "Home"
    away = parts[-1].strip().title() if len(parts) >= 2 else "Away"
    return {
        "match_id": re.sub(r"[^a-z0-9]+", "-", _norm_ws(query)).strip("-"),
        "sport": "Soccer" if "vs" in _norm_ws(query) else _norm_ws(query)[:20],
        "icon": "⚽",
        "home_team": home,
        "away_team": away,
        "commence": "",
        "market_type": m["type"],
        "market_name": m["name"],
        "line": m.get("line"),
        "selection": m["selection"],
        "best_bookmaker": m["best"]["bookmaker"],
        "best_price": m["best"]["decimal"],
        "fair_probability": m["fair_probability"],
        "raw_probability": m["raw_probability"],
        "edge_percent": m["ev_percent"],
        "ev_percent": m["ev_percent"],
        "kelly_stake": m["kelly_stake"],
        "confidence": m["confidence"],
        "signals": m.get("signals", []),
        "num_bookmakers": m["num_bookmakers"],
        "bookmakers": m["bookmakers"],
        "movement": m.get("movement", []),
    }


# ---------------------------------------------------------------------------
# Minimal local dev server (stdlib only -- the PWA points at it during `npm run dev`)
# ---------------------------------------------------------------------------

def serve(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse as up

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # CORS preflight
            self._send(204, b"", "text/plain")

        def do_GET(self):  # noqa: N802
            parsed = up(self.path)
            if parsed.path != "/hunt":
                self._send(404, b'{"status":"error","message":"not found"}',
                           "application/json")
                return
            qs = parse_qs(parsed.query)
            query = (qs.get("query") or [""])[0].strip()
            sport = (qs.get("sport") or ["auto"])[0].strip() or "auto"
            if not query:
                self._send(400, b'{"status":"error","message":"missing query"}',
                           "application/json")
                return
            try:
                payload = run_hunt(query, sport)
                payload.pop("_raw_rows", None)
                code = 200
            except Exception as exc:  # never hang the UI, report honestly
                payload = {"status": "error", "query": query, "sport": sport,
                           "markets_scanned": 0, "edges": [],
                           "message": f"hunt failed: {type(exc).__name__}"}
                code = 500
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json")

        def log_message(self, fmt, *args):  # route app logs through ours
            log("http " + (fmt % args))

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"hunter dev server listening on http://127.0.0.1:{port}/hunt")
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hunter Mode real-time scan")
    sub = ap.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="run local dev HTTP server")
    serve_p.add_argument("--port", type=int, default=8787)

    ap.add_argument("--query", type=str, default="", help='e.g. "Al Hilal vs Al Nassr"')
    ap.add_argument("--sport", type=str, default="auto",
                    choices=["auto", "soccer", "basketball", "tennis"])
    ap.add_argument("--output", "-o", type=str, default="",
                    help="write JSON here (default: stdout)")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        serve(args.port)
        return 0

    if not args.query:
        ap.error("--query is required (or use: serve)")

    payload = run_hunt(args.query, args.sport)
    payload.pop("_raw_rows", None)
    text = json.dumps(payload, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        log(f"wrote {args.output}")
    else:
        print(text)
    return 0 if payload["status"] == "success" else 2


if __name__ == "__main__":
    sys.exit(main())
