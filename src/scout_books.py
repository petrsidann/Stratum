"""SOURCE SURVIVOR — plain-HTTP odds scouts that don't need a browser.

Evidence (hunt #3, Czechia vs England): ESPN returned the fixture with ZERO
odds lines; Betika/Odibets headless both crash(nav) on GitHub runners.
Probed candidates from the sandbox:
  (a) betexplorer.com match pages   -> HTTP 200, server-rendered 1X2 decimals  WINNER
  (b) sofascore unofficial API      -> HTTP 403 dead
  (c) flashscore mobile JSON        -> HTTP 404 (obfuscated slugs)
  (d) the-odds-api                  -> implemented, gated on ODDS_API_KEY env
  (e) betika plain HTTP             -> SPA shell, zero odds numbers

Contract (mirrors scout_kenya exactly):
  scout_fixture(home, away, timeout_s=25) -> (rows, status)
    rows   : list of {market, selection, decimal_odds, source}
    status : "ok(N)" | "blocked_by_waf" | "timeout" | "not_found" | "crash(msg)"
NEVER raises. stdlib only (urllib + re); no guarded third-party import needed.
"""

import json
import os
import re
import time
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BE_BASE = "https://www.betexplorer.com"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

_STOPWORDS = {"vs", "v", "the", "and", "fc", "cf", "sc", "ac", "as", "ss",
              "if", "bk", "club", "team", "home", "away"}


def _norm(s):
    words = [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]
    return [w for w in words if w not in _STOPWORDS]


def _match_words(needle, hay):
    """All tokens of needle appear (in order) inside hay's token stream."""
    n, h = _norm(needle), _norm(hay)
    if not n:
        return False
    i = 0
    for w in h:
        if i < len(n) and w == n[i]:
            i += 1
    return i == len(n)


def _fetch(url, timeout=20):
    req = urlreq.Request(url, headers={"User-Agent": UA,
                                       "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), None
    except HTTPError as e:
        if e.code in (403, 429):
            return None, "blocked_by_waf"
        return None, f"http_{e.code}"
    except (URLError, socket_timeout_err()):
        return None, "timeout"
    except Exception as e:  # noqa: BLE001
        return None, f"crash({type(e).__name__})"


def socket_timeout_err():
    import socket
    return (TimeoutError, socket.timeout)


def _valid_odd(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 3) if 1.01 <= v <= 100.0 else None


# ---------------------------------------------------------------------------
# betexplorer (candidate a — the survivor)
# ---------------------------------------------------------------------------

def _be_find_match_url(home, away, timeout_s):
    """Walk sport boards (newest first) looking for a row containing BOTH team
    names whose href is a /match/ page. Returns (url, status)."""
    budget = max(5.0, timeout_s - 8.0)
    started = time.time()
    boards = [f"{BE_BASE}/football/", f"{BE_BASE}/football/all/"]
    for board in boards:
        if time.time() - started > budget:
            return None, "timeout"
        html, err = _fetch(board, timeout=min(15, int(budget)))
        if html is None:
            return None, err
        rows = re.findall(
            r'<tr[^>]*class="table-main__(?:detail|overview)[^"]*"[^>]*>(.*?)</tr>',
            html, re.S)
        cands = []
        for row in rows:
            m = re.search(r'href="(/match/[^"]+)"', row)
            if not m:
                continue
            text = re.sub(r"<[^>]+>", " ", row)
            links = re.findall(r'>([^<>]{2,60})</a>', row)
            pair = " ".join(links[:4]) or text
            score_between = bool(re.search(r"\d+\s*-\s*\d+", pair))
            if _match_words(home, pair) and _match_words(away, pair):
                cands.append((m.group(1), score_between))
        if cands:
            # prefer live/upcoming rows (no final score rendered between teams)
            cands.sort(key=lambda c: c[1])
            return BE_BASE + cands[0][0], "found"
    return None, "not_found"


_ODD_TOKEN_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")


def _be_extract_odds(html):
    """Pull every inline my_selections_click(...) odd from a match page.

    Handler arg shape observed on betexplorer.com:
      my_selections_click('id','sel_id','<odd>','<bet>/','<url>','<sel>')
    Selection may be 'Home Team'/'Away Team'/'Draw' (1X2) or numeric-ish ids
    for totals ('Over 2.5' etc. arrive via the visible link text fallback).
    """
    rows = []
    seen = set()
    handlers = re.findall(r"my_selections_click\((.*?)\)", html)
    # map sel ids to their anchor text for readable selections
    anchor_text = {}
    for m in re.finditer(r'onclick="my_selections_click\((.*?)\)">([^<]{1,60})</', html):
        anchor_text[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()
    for h in handlers:
        parts = [p.strip().strip("'\"") for p in h.split(",")]
        odd = sel = None
        for p in parts:
            if odd is None and _ODD_TOKEN_RE.match(p):
                odd = _valid_odd(p)
        txt = anchor_text.get(h, "")
        low = txt.lower()
        if odd is None:
            continue
        if "over" in low:
            market, sel = "Total Goals Over/Under", "Over " + re.sub(r"[^0-9.]", "", low).split(".")[0] + (".5" if ".5" in low else "")
        elif "under" in low:
            market, sel = "Total Goals Over/Under", "Under " + re.sub(r"[^0-9.]", "", low).split(".")[0] + (".5" if ".5" in low else "")
        elif "home" in low or "1" == low:
            market, sel = "Match Winner (1X2)", "Home"
        elif "away" in low or "2" == low:
            market, sel = "Match Winner (1X2)", "Away"
        elif "draw" in low or "x" == low:
            market, sel = "Match Winner (1X2)", "Draw"
        elif "btts" in low or "both" in low:
            market, sel = "Both Teams to Score (BTTS)", "Yes" if "yes" in low else "No"
        else:
            market, sel = "Match Winner (1X2)", txt or "?"
        key = (market, sel, odd)
        if key in seen or sel in ("?", ""):
            continue
        seen.add(key)
        rows.append({"market": market, "selection": sel,
                     "decimal_odds": odd, "source": "BETEXPLORER"})
    return rows


def _scout_betexplorer(home, away, timeout_s):
    url, status = _be_find_match_url(home, away, timeout_s)
    if url is None:
        return [], status
    html, err = _fetch(url, timeout=min(20, max(5, int(timeout_s) - 8)))
    if html is None:
        return [], err
    rows = _be_extract_odds(html)
    if not rows:
        return [], "not_found"
    return rows, f"ok({len(rows)})"


# ---------------------------------------------------------------------------
# the-odds-api (candidate d — gated on env key, never called without one)
# ---------------------------------------------------------------------------

def _scout_oddsapi(home, away, timeout_s=15):
    if not ODDS_API_KEY:
        return [], "no_key"
    url = (f"https://api.the-odds-api.com/v4/sports/soccer_alliance/"
           f"scores/?daysFrom=1&apiKey={ODDS_API_KEY}")
    body, err = _fetch(url, timeout=timeout_s)
    if body is None:
        return [], err
    try:
        events = json.loads(body)
    except Exception:  # noqa: BLE001
        return [], "crash(badjson)"
    rows = []
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        if not (_match_words(home, ev.get("home_team") or "")
                and _match_words(away, ev.get("away_team") or "")):
            continue
        for bk in ev.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                if mk.get("key") != "h2h":
                    continue
                for o in mk.get("outcomes") or []:
                    price = _valid_odd(o.get("price"))
                    name = (o.get("name") or "").lower()
                    if price is None:
                        continue
                    sel = ("Home" if name == (ev.get("home_team") or "").lower()
                           else "Away" if name == (ev.get("away_team") or "").lower()
                           else "Draw")
                    rows.append({"market": "Match Winner (1X2)", "selection": sel,
                                 "decimal_odds": price,
                                 "source": (bk.get("title") or "ODDS-API").upper()[:12]})
    return (rows, f"ok({len(rows)})") if rows else ([], "not_found")


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def scout_fixture(home, away, timeout_s=25):
    """Betexplorer first; the-odds-api fallback when ODDS_API_KEY exists."""
    try:
        rows, status = _scout_betexplorer(home, away, timeout_s)
        if rows:
            return rows, status
        if ODDS_API_KEY:
            rows2, st2 = _scout_oddsapi(home, away)
            if rows2:
                return rows2, st2
            return [], f"betexplorer={status};oddsapi={st2}"
        return [], status
    except Exception as e:  # noqa: BLE001 — contract: never raises
        return [], f"crash({type(e).__name__})"


def scout_both(home, away, betika_timeout_s=25, odibets_timeout_s=25):
    """Kenyan-contract shim: returns (rows, statuses_dict) where the single
    survivor source reports under BETEXPLORER (plus THE-ODDS-API when keyed)."""
    statuses = {}
    rows, st = scout_fixture(home, away, betika_timeout_s)
    statuses["BETEXPLORER"] = st
    return rows, statuses


if __name__ == "__main__":  # tiny live smoke demo
    r, s = scout_fixture("Czechia", "England")
    print(s)
    print(json.dumps(r[:6], indent=1))
