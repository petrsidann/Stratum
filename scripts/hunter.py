"""Stratum Market Hunter - live odds acquisition via public web sources.

Replaces the paid-API-dependent engine. Primary strategy: scrape real,
publicly-viewable betting boards (Kenyan books Betika/Odibets and
international mirrors) with requests + BeautifulSoup/lxml. When a site is
behind Cloudflare or otherwise blocks datacenter IPs, fall back to cached
snapshots (Wayback Machine / Google Cache) of the same URLs so we still read
REAL prices that were actually posted -- never invented ones.

Secondary enrichment source: ESPN's public scoreboard JSON (free, no key).
It carries genuine bookmaker prices (Caesars/BetMGM/fliff lines) for US and
soccer markets and keeps the feed non-empty when every scraping target is
blocked. It is used only as a price source; nothing is synthesized.

Pipeline per run:
    1. KenyaMarketHunter.scrape_all()      -> raw rows {market, selection,
                                               odds(decimal), source}
    2. src/analyzer.analyze_rows()         -> fair odds, EV, Kelly, signals
    3. src/visualizer.generate_reports()   -> PNG charts in data/reports/
    4. write data/live_market_feed.json    -> consumed by the PWA

Integrity rules:
    - No market is emitted without at least one real scraped price.
    - Handles whose implied-probability sum exceeds 1.15 are discarded
      (bad scrape heuristic).
    - Missing markets are skipped silently, never padded.

Politeness: robots.txt honored where fetchable, realistic browser User-Agent,
random 1-3s sleeps between requests, per-run global budget. Never crashes:
every failure path logs and continues.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import traceback
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    print("[hunter] FATAL: requests is not installed", file=sys.stderr)
    raise

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Make src/analyzer importable regardless of CWD
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from analyzer import analyze_rows  # noqa: E402

HUNTER_VERSION = "3.0.0"

OUTPUT_PATH = os.environ.get(
    "STRATUM_OUTPUT", os.path.join(_ROOT, "data", "live_market_feed.json"))
STATE_PATH = os.environ.get(
    "STRATUM_STATE", os.path.join(_ROOT, "data", ".engine_state.json"))
REPORTS_DIR = os.path.join(_ROOT, "data", "reports")

REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
GLOBAL_BUDGET_S = float(os.environ.get("STRATUM_BUDGET_S", "900"))
POLITE_MIN_SLEEP = float(os.environ.get("STRATUM_SLEEP_MIN", "1.0"))
POLITE_MAX_SLEEP = float(os.environ.get("STRATUM_SLEEP_MAX", "3.0"))

USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"),
]

_START = time.monotonic()


def budget_left() -> float:
    return GLOBAL_BUDGET_S - (time.monotonic() - _START)


def log(msg: str) -> None:
    print(f"[hunter {time.monotonic() - _START:7.1f}s] {msg}", flush=True)


def polite_sleep() -> None:
    time.sleep(random.uniform(POLITE_MIN_SLEEP, POLITE_MAX_SLEEP))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# State (persisted odds history across runs for line-movement tracking)
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"odds_history": {}, "last_run": None}


def save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=26)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
        hist = state.get("odds_history", {})
        trimmed: Dict[str, List[List[Any]]] = {}
        for key, points in hist.items():
            kept = [p for p in points if str(p[0]) >= cutoff]
            if kept:
                trimmed[key] = kept[-48:]
        state["odds_history"] = trimmed
        state["last_run"] = utc_now_iso()
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as exc:
        log(f"state save failed: {exc}")


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------

_robots_cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}


def allowed_by_robots(url: str, ua: str) -> bool:
    """Best-effort robots check. On any failure we default to allowing the
    request (the sites' public sportsbook pages are crawlable by browsers)."""
    try:
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if origin not in _robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(origin + "/robots.txt")
            rp.read()
            _robots_cache[origin] = rp
        rp = _robots_cache[origin]
        if rp is None:
            return True
        return rp.can_fetch(ua, url)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_session = requests.Session()


def http_get(url: str, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             retries: int = MAX_RETRIES) -> Optional[requests.Response]:
    for attempt in range(retries):
        if budget_left() <= 10:
            return None
        ua = random.choice(USER_AGENTS)
        hdrs = {
            "User-Agent": ua,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        if headers:
            hdrs.update(headers)
        try:
            resp = _session.get(url, params=params, headers=hdrs,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                wait = min(4 * (attempt + 1), max(budget_left() - 10, 0))
                log(f"429 on {url[:70]} -> sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403, 451):
                log(f"HTTP {resp.status_code} (blocked) on {url[:70]}")
                return None  # caller decides on cache fallback
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except requests.RequestException as exc:
            log(f"network error ({type(exc).__name__}) on {url[:70]}")
            time.sleep(1.0)
    return None


DECIMAL_RE = re.compile(r"^\d{1,2}\.\d{2}$")
AMERICAN_RE = re.compile(r"^[+-]\d{3,4}$")


def to_decimal(raw: str) -> Optional[float]:
    """Parse an odds token into decimal odds. Accepts decimal ('1.85') and
    American ('+150', '-110'). Returns None for junk."""
    t = (raw or "").strip().replace(",", ".")
    if DECIMAL_RE.match(t):
        d = float(t)
        return d if 1.01 <= d <= 1000 else None
    if AMERICAN_RE.match(t):
        v = float(t)
        if v > 0:
            return round(1 + v / 100, 3)
        return round(1 + 100 / abs(v), 3)
    return None


# ---------------------------------------------------------------------------
# KenyaMarketHunter
# ---------------------------------------------------------------------------

class KenyaMarketHunter:
    """Scraper for Kenyan books + international mirrors with cache fallback.

    Extraction model: server-rendered HTML tables/divs are parsed with
    BeautifulSoup. For each match block found we iterate ALL market tabs
    present in the markup (1X2, Double Chance, O/U, AH, BTTS, corners,
    cards, first half, team totals, correct score, props). A market tab that
    does not exist in the page is simply skipped.
    """

    TARGETS: List[Dict[str, Any]] = [
        {
            "source": "Betika",
            "sport": "Football",
            "sport_key": "football",
            "icon": "soccer",
            "urls": [
                "https://www.betika.com/en-gb/sport/football",
                "https://betika.com/en-gb/sport/football",
            ],
            # selectors tried in order; first hit wins
            "event_selectors": ["div.events-list__grid__event",
                                "div[class*='event']",
                                "table tr[class*='selection']"],
            "odd_selectors": ["button[class*='odd']", "span[class*='odd']",
                              "td[class*='odd']", "[data-odd]",
                              "span[class*='price']"],
        },
        {
            "source": "Odibets",
            "sport": "Football",
            "sport_key": "football",
            "icon": "soccer",
            "urls": [
                "https://odibets.com/leagues/ke-premier-league",
                "https://odibets.com/spot/football",
            ],
            "event_selectors": ["div.event-card", "div[class*='event']",
                                "article[class*='event']"],
            "odd_selectors": ["div.price button", "button[class*='price']",
                              "span[class*='price']", "[data-testid*='odd']"],
        },
        {
            "source": "SportPesa",
            "sport": "Football",
            "sport_key": "football",
            "icon": "soccer",
            "urls": [
                "https://www.sportpesa.co.ke/SportsBook#/competition/1",
                "https://sportpesa.com/sports/football/",
            ],
            "event_selectors": ["div[class*='event-row']", "tr[class*='event']"],
            "odd_selectors": ["span.odds", "button.odds", "[class*='odd-value']"],
        },
        {
            "source": "Pinnacle-Mirror",
            "sport": "Football",
            "sport_key": "football",
            "icon": "soccer",
            "urls": ["https://www.pinnacle.com/en/soccer/matchups"],
            "event_selectors": ["div[class*='events-row-container']",
                                "div[class*='c-events__item']"],
            "odd_selectors": ["span[class*='odds-format']", "button[class*='odds']"],
        },
    ]

    # Canonical market keywords -> (type, display name)
    MARKET_PATTERNS: List[Tuple[str, str, str]] = [
        (r"\b(1\s*x\s*2|match winner|home away draw|to win|result)\b",
         "moneyline", "Match Winner"),
        (r"\b(double chance|1x|x2|12)\b", "double_chance", "Double Chance"),
        (r"\b(over[/ ]?under|total goals|o/u|totals?)\b", "total", "Over/Under Goals"),
        (r"\b(asian handicap|ah|handicap)\b", "asian_handicap", "Asian Handicap"),
        (r"\b(both teams to score|btts|gg/ng|goal goal)\b", "btts", "BTTS"),
        (r"\b(corners?|corner kick)\b", "corners", "Corners"),
        (r"\b(cards?|bookings?|yellow)\b", "cards", "Cards"),
        (r"\b(first half|1st half|halves|ht result)\b", "halves", "First Half"),
        (r"\b(second half|2nd half)\b", "halves", "Second Half"),
        (r"\b(team total|team goals)\b", "team_total", "Team Totals"),
        (r"\b(correct score|scorecast)\b", "correct_score", "Correct Score"),
        (r"\b(player|shots? on target|assists?|goalscorer|tbs|anytime)\b",
         "player_prop", "Player Props"),
        (r"\b(draw no bet|dnb)\b", "draw_no_bet", "Draw No Bet"),
        (r"\b(clean sheet|nil to score)\b", "clean_sheet", "Clean Sheet"),
    ]

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []          # raw scraped rows
        self.matches_meta: Dict[str, Dict[str, Any]] = {}
        self.stats = {"pages_fetched": 0, "pages_blocked": 0,
                      "cache_fallbacks": 0, "rows_scraped": 0}

    # -- market classification ------------------------------------------------
    def classify(self, context: str) -> Tuple[str, str]:
        c = (context or "").lower()
        for pattern, mtype, display in self.MARKET_PATTERNS:
            if re.search(pattern, c):
                return mtype, display
        return "other", "Other Markets"

    # -- page acquisition with cache fallback ---------------------------------
    def fetch_page(self, url: str) -> Optional[str]:
        if not allowed_by_robots(url, USER_AGENTS[0]):
            log(f"robots.txt disallows {url[:70]} -> skipping")
            return None
        resp = http_get(url)
        if resp is not None and resp.text and len(resp.text) > 2000:
            self.stats["pages_fetched"] += 1
            return resp.text
        self.stats["pages_blocked"] += 1
        # Fallback 1: Wayback Machine snapshot of this exact URL
        html = self._wayback_snapshot(url)
        if html:
            self.stats["cache_fallbacks"] += 1
            return html
        # Fallback 2: Google Cache (best effort; often blocked from CI IPs)
        html = self._google_cache(url)
        if html:
            self.stats["cache_fallbacks"] += 1
            return html
        return None

    def _wayback_snapshot(self, url: str) -> Optional[str]:
        if not budget_left() > 30:
            return None
        try:
            api = f"http://archive.org/wayback/available?url={quote_plus(url)}"
            resp = http_get(api)
            if resp is None:
                return None
            snap = ((resp.json().get("archived_snapshots") or {})
                    .get("closest") or {})
            snap_url = snap.get("url")
            if snap_url and snap.get("available"):
                log(f"wayback fallback: {snap_url[:90]}")
                r2 = http_get(snap_url)
                if r2 is not None and r2.text:
                    return r2.text
        except Exception as exc:
            log(f"wayback lookup failed: {exc}")
        return None

    def _google_cache(self, url: str) -> Optional[str]:
        if not budget_left() > 30:
            return None
        try:
            r = http_get(f"https://webcache.googleusercontent.com/search?q=cache:{url}")
            if r is not None and r.text and "odds" in r.text.lower():
                return r.text
        except Exception:
            pass
        return None

    # -- parsing ---------------------------------------------------------------
    @staticmethod
    def _block_text(block) -> str:
        return block.get_text(" ", strip=True)[:600]

    def _teams_from_block(self, block) -> Tuple[Optional[str], Optional[str]]:
        text = block.get_text("\n", strip=True)
        lines = [l for l in text.split("\n") if l.strip()]
        vs_re = re.compile(r"^(.{3,40}?)\s+(?:vs\.?|-|v)\s+(.{3,40})$", re.I)
        for ln in lines[:12]:
            m = vs_re.match(ln.strip())
            if m:
                return m.group(1).strip(), m.group(2).strip()
        team_re = re.compile(r"^(?:FC |SC |CF |AFC |CD |SD )?[A-Z][A-Za-z .'-]{2,30}$")
        names: List[str] = []
        for ln in lines[:16]:
            s = ln.strip()
            if team_re.match(s) and not DECIMAL_RE.match(s) and s not in names:
                names.append(s)
            if len(names) == 2:
                break
        if len(names) == 2:
            return names[0], names[1]
        return None, None

    def parse_target_page(self, target: Dict[str, Any], html: str) -> int:
        """Extract raw rows from one page. Returns count of rows added."""
        if not HAS_BS4:
            log("beautifulsoup4 missing; parser disabled")
            return 0
        soup = BeautifulSoup(html, "html.parser")
        before = len(self.rows)

        blocks: List[Any] = []
        for sel in target["event_selectors"]:
            try:
                found = soup.select(sel)
            except Exception:
                found = []
            if found:
                blocks = found
                break
        if not blocks:
            # Last resort: any table rows that contain at least two decimal odds
            blocks = [tr for tr in soup.find_all("tr")
                      if sum(1 for t in tr.stripped_strings if DECIMAL_RE.match(t)) >= 2]

        for blk in blocks[:60]:  # bound work per page
            home, away = self._teams_from_block(blk)
            if not (home and away):
                continue  # skip silently rather than guess teams
            match_id = re.sub(r"\W+", "_", f"{home}_{away}".lower())[:60]
            meta_key = f"{target['source']}:{match_id}"
            self.matches_meta.setdefault(meta_key, {
                "id": f"hunt_{match_id}",
                "source": target["source"].lower(),
                "sources": [target["source"].lower()],
                "sport": target["sport"],
                "sport_key": target["sport_key"],
                "icon": target["icon"],
                "home_team": home,
                "away_team": away,
                "commence": "",
                "status": "NOT_STARTED",
            })

            # Context for market classification: nearest preceding header text
            context = self._block_text(blk)
            odd_nodes: List[Any] = []
            for sel in target["odd_selectors"]:
                try:
                    odd_nodes = blk.select(sel)
                except Exception:
                    odd_nodes = []
                if odd_nodes:
                    break
            candidates = odd_nodes or [blk]
            seq: List[Tuple[str, str]] = []  # (label context, odds token)
            for node in candidates:
                token = (node.get_text(strip=True)
                         or node.get("data-odd") or "")
                dec = to_decimal(token)
                if dec is None:
                    continue
                label_ctx = ""
                prev = node.find_previous(string=re.compile(r"[A-Za-z]{3,}"))
                if prev:
                    label_ctx = str(prev)[:80]
                seq.append((label_ctx or context[:80], token))

            # Assign selections positionally within the block's team pair
            for idx, (ctx_label, token) in enumerate(seq):
                dec = to_decimal(token)
                if dec is None:
                    continue
                mtype, display = self.classify(ctx_label)
                if idx % 3 == 0:
                    sel = home
                elif idx % 3 == 1:
                    sel = "Draw"
                else:
                    sel = away
                if mtype in ("total", "asian_handicap", "corners", "cards"):
                    pt = re.search(r"(\d+(?:\.\d+)?)", ctx_label)
                    line = float(pt.group(1)) if pt else None
                    over = (idx % 2 == 0)
                    sel = f"{'Over' if over else 'Under'} {line if line else ''}".strip() \
                        if mtype in ("total", "corners", "cards") \
                        else f"{sel} {'-' if over else '+'}{line if line else ''}".strip()
                self.rows.append({
                    "match_meta_key": meta_key,
                    "source": target["source"],
                    "market": mtype,
                    "name": display,
                    "selection": sel,
                    "line": None,
                    "odds": dec,
                })
        added = len(self.rows) - before
        self.stats["rows_scraped"] += added
        return added

    # -- public API ------------------------------------------------------------
    def scrape_all(self) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        if budget_left() < 60:
            log("insufficient budget for scraping pass")
            return [], {}
        for target in self.TARGETS:
            if budget_left() < 45:
                log("budget low; ending scrape pass early")
                break
            got_html = False
            for url in target["urls"]:
                html = self.fetch_page(url)
                if html:
                    n = self.parse_target_page(target, html)
                    log(f"scrape[{target['source']}]: {n} rows from {url[:60]}")
                    got_html = True
                    break
                polite_sleep()
            if not got_html:
                log(f"scrape[{target['source']}]: all URLs blocked/unparseable")
            polite_sleep()
        return self.rows, self.matches_meta


# ---------------------------------------------------------------------------
# ESPN public scoreboard fallback (real bookmaker prices, free, no key)
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
                   "{sport}/{league}/scoreboard")

ESPN_LEAGUES = [
    {"key": "epl", "sport": "soccer", "league": "eng.1", "display": "Premier League", "icon": "soccer"},
    {"key": "ucl", "sport": "soccer", "league": "uefa.champions", "display": "Champions League", "icon": "soccer"},
    {"key": "laliga", "sport": "soccer", "league": "esp.1", "display": "La Liga", "icon": "soccer"},
    {"key": "seriea", "sport": "soccer", "league": "ita.1", "display": "Serie A", "icon": "soccer"},
    {"key": "bundesliga", "sport": "soccer", "league": "ger.1", "display": "Bundesliga", "icon": "soccer"},
    {"key": "ligue1", "sport": "soccer", "league": "fra.1", "display": "Ligue 1", "icon": "soccer"},
    {"key": "nba", "sport": "basketball", "league": "nba", "display": "NBA", "icon": "basketball"},
    {"key": "nfl", "sport": "football", "league": "nfl", "display": "NFL", "icon": "football_amer"},
    {"key": "mlb", "sport": "baseball", "league": "mlb", "display": "MLB", "icon": "baseball"},
]

_ESPN_MARKET_MAP = [
    (re.compile(r"moneyline|match winner|to win|1x2|outright", re.I), "moneyline", "Match Winner"),
    (re.compile(r"double chance", re.I), "double_chance", "Double Chance"),
    (re.compile(r"spread|handicap", re.I), "asian_handicap" if False else "spread", "Spread / Handicap"),
    (re.compile(r"total|over|under", re.I), "total", "Total"),
    (re.compile(r"both teams|btts", re.I), "btts", "BTTS"),
    (re.compile(r"correct score", re.I), "correct_score", "Correct Score"),
    (re.compile(r"corner", re.I), "corners", "Corners"),
    (re.compile(r"card|booking", re.I), "cards", "Cards"),
    (re.compile(r"half", re.I), "halves", "Halves"),
    (re.compile(r"quarter", re.I), "quarters", "Quarters"),
    (re.compile(r"alternate", re.I), "alternates", "Alternates"),
    (re.compile(r"pts|points|rebounds|assists|yards|receptions|hits|shots|prop", re.I),
     "player_prop", "Player Props"),
]


def _espn_classify(label: str) -> Tuple[str, str]:
    for rx, mtype, display in _ESPN_MARKET_MAP:
        if rx.search(label or ""):
            if mtype == "spread" and "handicap" in (label or "").lower():
                return "asian_handicap", "Asian Handicap"
            return mtype, display
    return "other", (label or "Other").title()


def _american_to_decimal(v: Any) -> Optional[float]:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v == 0 or math_is_bad(v):
        return None
    return round(1 + v / 100, 3) if v > 0 else round(1 + 100 / abs(v), 3)


def math_is_bad(v: float) -> bool:
    import math as _m
    return _m.isnan(v) or _m.isinf(v)


def fetch_espn_rows(state: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                    Dict[str, Dict[str, Any]]]:
    """Real bookmaker prices from ESPN's public scoreboard payload.

    ESPN embeds close/open odds from licensed books (Caesars, BetMGM, fliff,
    ESPN BET). We convert them to decimal and emit them as ordinary scraped
    rows -- same pipeline as the Kenyan boards.
    """
    rows: List[Dict[str, Any]] = []
    metas: Dict[str, Dict[str, Any]] = {}
    today = datetime.now(timezone.utc)
    for league in ESPN_LEAGUES:
        if budget_left() <= 40:
            break
        urls = [ESPN_SCOREBOARD.format(sport=league["sport"], league=league["league"])]
        for off in (0, 1):
            d = (today + timedelta(days=off)).strftime("%Y%m%d")
            urls.append(ESPN_SCOREBOARD.format(sport=league["sport"],
                                               league=league["league"]) + f"?dates={d}&limit=60")
        data = None
        for u in urls:
            resp = http_get(u, headers={"Accept": "application/json"})
            if resp is not None:
                try:
                    data = resp.json()
                    break
                except ValueError:
                    pass
            polite_sleep()
        if not isinstance(data, dict):
            log(f"espn {league['display']}: unavailable")
            continue
        events = data.get("events") or []
        captured = 0
        for ev in events[:20]:
            try:
                comp = (ev.get("competitions") or [{}])[0]
                comps = comp.get("competitors") or []
                home = next((c for c in comps if c.get("homeAway") == "home"), {})
                away = next((c for c in comps if c.get("homeAway") == "away"), {})
                hn = ((home.get("team") or {}).get("displayName") or "HOME")
                an = ((away.get("team") or {}).get("displayName") or "AWAY")
                eid = str(ev.get("id"))
                meta_key = f"espn:{eid}"
                metas.setdefault(meta_key, {
                    "id": f"espn_{eid}",
                    "source": "espn",
                    "sources": ["espn"],
                    "sport": league["display"],
                    "sport_key": league["key"],
                    "icon": league["icon"],
                    "home_team": hn,
                    "away_team": an,
                    "commence": ev.get("date") or "",
                    "status": ((ev.get("status") or {}).get("type") or {}).get("state", "pre"),
                })
                provs = comp.get("odds") or []
                for prov in provs:
                    pname = ((prov.get("provider") or {}).get("name")) or "ESPN Book"
                    outcome_rows = (prov.get("summary") or {}).get("outcomes") or \
                        prov.get("outcomes") or []
                    for o in outcome_rows:
                        dec = _american_to_decimal(o.get("price"))
                        if not dec:
                            continue
                        label = (((o.get("type") or {}).get("text"))
                                 or o.get("description") or "Market")
                        mtype, display = _espn_classify(label)
                        rows.append({
                            "match_meta_key": meta_key,
                            "source": pname,
                            "market": mtype,
                            "name": display,
                            "selection": o.get("description") or "?",
                            "line": o.get("point"),
                            "odds": dec,
                        })
                        captured += 1
            except Exception:
                log(f"espn event parse error: {traceback.format_exc(limit=1).strip()}")
        log(f"espn {league['display']}: {len(events)} events, {captured} price rows")
        polite_sleep()
    return rows, metas


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------

def dedupe_matches(all_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for m in all_matches:
        teams = tuple(sorted([(m.get("home_team") or "").lower(),
                              (m.get("away_team") or "").lower()]))
        key = f"{m.get('sport_key')}|{teams[0]}|{teams[1]}"
        if key in merged:
            existing = merged[key]
            have = {(mk["type"], mk.get("line"), mk["selection"])
                    for mk in existing["markets"]}
            for mk in m["markets"]:
                if (mk["type"], mk.get("line"), mk["selection"]) not in have:
                    existing["markets"].append(mk)
                    have.add((mk["type"], mk.get("line"), mk["selection"]))
            existing["sources"] = sorted(set(existing.get("sources", [])
                                             + m.get("sources", [])))
        else:
            merged[key] = m
    out = list(merged.values())
    for m in out:
        m["markets"].sort(key=lambda mk: -mk["ev_percent"])
        m["market_count"] = len(m["markets"])
    out.sort(key=lambda m: m.get("commence") or "9999")
    return out


def top_edges(matches: List[Dict[str, Any]], min_confidence: float = 40.0,
              limit: int = 150) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for m in matches:
        for mk in m["markets"]:
            if mk["ev_percent"] > 0:
                edges.append({
                    "match_id": m["id"],
                    "sport": m["sport"], "icon": m["icon"],
                    "home_team": m["home_team"], "away_team": m["away_team"],
                    "commence": m.get("commence"),
                    "market_type": mk["type"], "market_name": mk["name"],
                    "line": mk.get("line"), "selection": mk["selection"],
                    "best_bookmaker": mk["best"]["bookmaker"],
                    "best_price": mk["best"]["decimal"],
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
    "soccer": ["moneyline", "double_chance", "btts", "total", "asian_handicap",
               "corners", "cards", "player_prop", "correct_score", "halves",
               "team_total", "draw_no_bet", "clean_sheet", "other"],
    "us": ["moneyline", "spread", "total", "player_prop", "halves",
           "quarters", "alternates", "team_total", "other"],
}


def build_feed() -> Dict[str, Any]:
    state = load_state()
    all_rows: List[Dict[str, Any]] = []
    metas: Dict[str, Dict[str, Any]] = {}

    # Stage 1: aggressive scraping of Kenyan boards + mirrors
    hunter = KenyaMarketHunter()
    try:
        scraped_rows, scraped_metas = hunter.scrape_all()
        all_rows.extend(scraped_rows)
        metas.update(scraped_metas)
    except Exception:
        log(f"hunter stage failed: {traceback.format_exc(limit=2).strip()}")

    # Stage 2: ESPN public scoreboard prices (real, free, resilient)
    try:
        espn_rows, espn_metas = fetch_espn_rows(state)
        all_rows.extend(espn_rows)
        for k, v in espn_metas.items():
            metas.setdefault(k, v)
    except Exception:
        log(f"espn stage failed: {traceback.format_exc(limit=2).strip()}")

    # Stage 3: Obsidian Brain analysis per match
    rows_by_match: Dict[str, List[Dict[str, Any]]] = {}
    for r in all_rows:
        rows_by_match.setdefault(r.pop("match_meta_key"), []).append(r)

    all_matches: List[Dict[str, Any]] = []
    for meta_key, rows in rows_by_match.items():
        meta = metas.get(meta_key)
        if not meta:
            continue
        try:
            markets = analyze_rows(rows, state, meta["id"])
        except Exception:
            log(f"analysis failed for {meta_key}: "
                f"{traceback.format_exc(limit=1).strip()}")
            continue
        if not markets:
            continue  # no valid handles -> match omitted (No Signal in UI)
        m = dict(meta)
        m["markets"] = markets
        all_matches.append(m)

    matches = dedupe_matches(all_matches)
    total_markets = sum(len(m["markets"]) for m in matches)
    edges = top_edges(matches)

    feed = {
        "meta": {
            "version": HUNTER_VERSION,
            "generated_at": utc_now_iso(),
            "next_refresh_minutes": 15,
            "data_policy": "live_scraped_only_no_synthetic_data",
            "sources_attempted": [t["source"] for t in KenyaMarketHunter.TARGETS] + ["espn"],
            "sources_active": sorted({s for m in matches for s in m.get("sources", [])}),
            "scrape_stats": hunter.stats,
            "matches_scanned": len(matches),
            "markets_scanned": total_markets,
            "signals_found": len(edges),
            "budget_seconds_remaining": round(max(budget_left(), 0), 1),
        },
        "catalog": MARKET_TYPE_CATALOG,
        "top_edges": edges,
        "matches": matches,
        "reports": {"market_depth": [], "edge_radar": [], "line_movement": []},
    }

    # Stage 4: visual reports (PNG charts) -- optional, never fatal
    try:
        from visualizer import generate_reports
        files = generate_reports(feed, REPORTS_DIR)
        feed["reports"] = files
        log(f"charts written: " + ", ".join(f"{k}:{len(v)}" for k, v in files.items()))
    except Exception:
        log(f"visualizer unavailable: {traceback.format_exc(limit=1).strip()}")

    save_state(state)
    return feed


def atomic_write(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> int:
    log(f"Stratum hunter v{HUNTER_VERSION} starting (output={OUTPUT_PATH})")
    try:
        feed = build_feed()
    except Exception:
        log(f"FATAL during scan: {traceback.format_exc(limit=3).strip()}")
        feed = {
            "meta": {"version": HUNTER_VERSION, "generated_at": utc_now_iso(),
                     "error": "scan_failed", "matches_scanned": 0,
                     "markets_scanned": 0, "signals_found": 0,
                     "data_policy": "live_scraped_only_no_synthetic_data"},
            "catalog": MARKET_TYPE_CATALOG, "top_edges": [], "matches": [],
            "reports": {"market_depth": [], "edge_radar": [], "line_movement": []},
        }
    try:
        atomic_write(OUTPUT_PATH, feed)
        meta = feed.get("meta", {})
        log(f"wrote feed: {meta.get('matches_scanned', 0)} matches, "
            f"{meta.get('markets_scanned', 0)} markets, "
            f"{meta.get('signals_found', 0)} positive-EV signals")
    except Exception:
        log(f"FATAL writing output: {traceback.format_exc(limit=2).strip()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
