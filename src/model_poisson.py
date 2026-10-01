#!/usr/bin/env python3
"""Stratum QUANT BRAIN — Poisson/Dixon-Coles goal pricer (stdlib math only).

Turns the hunter from an *odds-reader* into an *odds-pricer*: for a football
fixture we ingest keyless ESPN team history, fit a Dixon-Coles-lite bivariate
goal model (lambda_home / lambda_away with a rho low-score correction) and
price every computable market in data/markets_taxonomy.csv for that sport.

Design rules
------------
* stdlib only (``math``, ``json``, ``csv``, ``urllib``) so it runs inside the
  GitHub-Actions hunt without pip installs.
* All network access is guarded: any failure degrades to league priors with a
  confidence penalty instead of raising.
* The model emits probabilities + fair odds. It never reads book odds here —
  edge computation lives in scripts/hunt_match.py (edge_vs_model =
  model_prob * book_odds - 1).
"""

from __future__ import annotations

import csv
import json
import math
import os
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Constants / priors
# --------------------------------------------------------------------------

RHO = 0.05                 # Dixon-Coles low-score dependency correction
FORM_WEIGHTS = [1.0, 0.9, 0.8, 0.7, 0.6]   # last-5 weighting (most recent first)
MAX_HISTORY_GAMES = 20     # last N completed games per team
HALF_SCALE = 0.45          # share of full-match expected goals scored in one half
CLAMP_LAMBDA = (0.15, 4.0)

LEAGUE_DEFAULTS = {        # (avg_home_goals, avg_away_goals, corners_h, corners_a, cards_h, cards_a)
    "default": {"lh": 1.50, "la": 1.15,
                "corner_home": 5.4, "corner_away": 4.4,
                "card_home": 2.1, "card_away": 2.4},
    "eng.1": {"lh": 1.55, "la": 1.20, "corner_home": 5.6, "corner_away": 4.6,
              "card_home": 2.0, "card_away": 2.3},
    "esp.1": {"lh": 1.50, "la": 1.15, "corner_home": 5.2, "corner_away": 4.3,
              "card_home": 2.4, "card_away": 2.6},
    "ita.1": {"lh": 1.40, "la": 1.10, "corner_home": 5.3, "corner_away": 4.4,
              "card_home": 2.6, "card_away": 2.8},
    "ger.1": {"lh": 1.65, "la": 1.25, "corner_home": 5.2, "corner_away": 4.3,
              "card_home": 2.2, "card_away": 2.5},
    "fra.1": {"lh": 1.45, "la": 1.15, "corner_home": 5.1, "corner_away": 4.2,
              "card_home": 2.5, "card_away": 2.7},
    "uefa.champions": {"lh": 1.60, "la": 1.30, "corner_home": 5.3, "corner_away": 4.5,
                       "card_home": 2.2, "card_away": 2.4},
}

HEADER_SETS = [
    {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json"},
]

SPORT_ALIASES = {"soccer": "soccer", "football_soccer": "soccer",
                 "nba": "basketball", "basketball": "basketball"}


# --------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def poisson_pmf(k, lam):
    """P(X = k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def dixon_coles_tau(h, a, lh, la, rho=RHO):
    """Dixon-Coles low-score correction factor (only 0/0, 0/1, 1/0, 1/1 differ)."""
    if h == 0 and a == 0:
        return 1.0 - lh * la * rho
    if h == 0 and a == 1:
        return 1.0 + lh * rho
    if h == 1 and a == 0:
        return 1.0 + la * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lh, la, rho=RHO, max_goals=10):
    """Bivariate DC-corrected score matrix rows=home goals, cols=away goals."""
    m = [[poisson_pmf(i, lh) * poisson_pmf(j, la) * dixon_coles_tau(i, j, lh, la, rho)
          for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    s = sum(sum(r) for r in m)
    if s > 0:
        m = [[v / s for v in r] for r in m]
    return m


def p_home_win(mat):
    return sum(mat[i][j] for i in range(len(mat)) for j in range(len(mat[0])) if i > j)


def p_draw(mat):
    return sum(mat[i][i] for i in range(len(mat)))


def p_away_win(mat):
    return sum(mat[i][j] for i in range(len(mat)) for j in range(len(mat[0])) if i < j)


def p_total_over(mat, line):
    """P(total goals > line) for half-integer or integer lines (over = strictly greater)."""
    tmax = len(mat) + len(mat[0]) - 2
    return sum(p_total_exactly(mat, t) for t in range(tmax + 1) if t > line)


def p_total_at_least(mat, n):
    tmax = len(mat) + len(mat[0]) - 2
    return sum(p_total_exactly(mat, t) for t in range(n, tmax + 1))


def p_total_exactly(mat, n):
    out = 0.0
    for i in range(len(mat)):
        j = n - i
        if 0 <= j < len(mat[0]):
            out += mat[i][j]
    return out


def p_btts(mat):
    return sum(mat[i][j] for i in range(1, len(mat)) for j in range(1, len(mat[0])))


def p_margin(mat, lo, hi):
    """P(lo <= home - away <= hi)."""
    return sum(mat[i][j] for i in range(len(mat)) for j in range(len(mat[0]))
               if lo <= i - j <= hi)


def p_asian_handicap(mat, line, side="home"):
    """Half-integer AH: no pushes. line given from home perspective."""
    win = 0.0
    for i in range(len(mat)):
        for j in range(len(mat[0])):
            diff = (i - j) if side == "home" else (j - i)
            if diff + line > 0:      # line already signed for that side
                win += mat[i][j]
    return win


def race_to_n(la, lb, n):
    """P(A reaches n events before B) with independent Poisson processes.

    Exact negative-binomial sum: P(A wins race) = sum_{k=n..inf} C(k+n-1,k)
    (la)^k (lb)^n e^{-(la+lb)} / k! ... computed iteratively to convergence.
    """
    if la <= 0:
        return 0.0
    if lb <= 0:
        return 1.0
    total = 0.0
    # iterate over number of A events k when B has exactly n-1 events at that time
    # closed form via binomial series: P = sum_{j=0}^{n-1} C(k+j, j) pA^k pB^(j+1)
    pa = la / (la + lb)
    pb = lb / (la + lb)
    for k in range(n, 400):
        # A scores its n-th event on trial k+n-1 (0-indexed k = A count-1), B has j<n
        term = 0.0
        comb = 1.0
        for j in range(n):
            # C((k-1)+j, j)
            comb = 1.0
            for c in range(j):
                comb = comb * (k + c) / (c + 1)
            term += comb * (pa ** k) * (pb ** j) * pb
        total += term
        if term < 1e-12 and k > n + 40:
            break
    return _clamp(total, 0.0, 1.0)


# --------------------------------------------------------------------------
# Keyless ESPN ingestion
# --------------------------------------------------------------------------

def http_json(url, timeout=15):
    last = None
    for headers in HEADER_SETS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last = f"http_{e.code}"
        except Exception as e:  # noqa: BLE001 - degrade, never raise
            last = f"error_{type(e).__name__}"
    return None, last


def _team_sport_league(fixture):
    sp = SPORT_ALIASES.get((fixture.get("sport") or "soccer").lower(), "soccer")
    lg = fixture.get("league") or "eng.1"
    return sp, lg


def _parse_completed_events(payload, team_id, now_iso=None):
    """Pull (goals_for, goals_against, was_home, date) from ESPN schedule payload."""
    games = []
    evs = payload.get("events") if isinstance(payload, dict) else None
    for ev in evs or []:
        try:
            if not isinstance(ev, dict):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            status = ((comp.get("status") or {}).get("type") or {}).get("abbrev") or ""
            if status not in ("FINAL", "FT"):
                continue
            date = ev.get("date") or ""
            home = away = None
            for c in comp.get("competitors") or []:
                if not isinstance(c, dict):
                    continue
                tid = str(((c.get("team") or {}).get("id")) or "")
                sc = c.get("score")
                try:
                    sc = int(sc)
                except (TypeError, ValueError):
                    continue
                rec = {"home": c.get("homeAway") == "home", "team_id": tid}
                if tid == str(team_id):
                    if c.get("homeAway") == "home":
                        home = (sc, None)
                    else:
                        away = (sc, None)
                else:
                    if c.get("homeAway") == "home":
                        home = (None, sc)
                    else:
                        away = (None, sc)
            if home is None or away is None:
                continue
            gf = home[0] if home[0] is not None else away[0]
            ga = home[1] if home[1] is not None else away[1]
            is_home = home[0] is not None
            if gf is None or ga is None:
                continue
            games.append({"gf": gf, "ga": ga, "home": is_home, "date": date})
        except Exception:  # noqa: BLE001
            continue
    # keep only games already completed (before ~now); protects the last-20 window
    import datetime as dt
    now_iso = (now_iso or dt.datetime.now(dt.timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%SZ"))
    games = [g for g in games if (g.get("date") or "") < now_iso]
    games.sort(key=lambda g: g.get("date") or "", reverse=True)
    return games[:MAX_HISTORY_GAMES]


def ingest_team_history(team_id, sport="soccer", league="eng.1", payload=None):
    """Last MAX_HISTORY_GAMES completed games for a team via keyless ESPN endpoints.

    Returns dict(games=[...], sample_games=int, standings=None|dict, source=str).
    Never raises — empty history on any failure. `payload` (dict of an ESPN
    schedule/board response) may be injected for offline tests.
    """
    out = {"games": [], "sample_games": 0, "standings": None, "source": "unavailable"}
    if not team_id and payload is None:
        return out
    sp = SPORT_ALIASES.get((sport or "soccer").lower(), "soccer")
    if payload is None:
        url = (f"https://site.api.espn.com/apis/site/v2/sports/{sp}/{league}/schedule"
               f"?team={team_id}&limit=100")
        payload, err = http_json(url)
        if payload is None:
            # fall back to the generic sports API
            url2 = f"https://sports.core.api.espn.com/v2/sports/{sp}/teams/{team_id}/events?limit=40"
            payload, err = http_json(url2)
            if payload is None:
                out["source"] = err or "no_payload"
                return out
    games = _parse_completed_events(payload, team_id)
    out["games"] = games
    out["sample_games"] = len(games)
    out["source"] = "espn_schedule"
    # standings when available (keyless)
    if team_id:
        st_url = f"https://site.api.espn.com/apis/v2/sports/{sp}/{league}/standings"
        st, _ = http_json(st_url)
        if isinstance(st, dict):
            try:
                for aggr in st.get("standings") or []:
                    for entry in (aggr.get("entries") or []):
                        if str(entry.get("team", {}).get("id")) == str(team_id):
                            stats = {s.get("name"): s.get("value") for s in entry.get("stats") or []}
                            out["standings"] = stats
                            raise StopIteration
            except StopIteration:
                pass
            except Exception:  # noqa: BLE001
                pass
    return out


def rest_days_from_games(games, kickoff_iso):
    """Whole days between the most recent completed game and the upcoming kickoff."""
    import datetime as dt

    def parse(s):
        s = (s or "").replace("Z", "+0000")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
        try:
            return dt.datetime.fromisoformat((s or "")[:19]).replace(tzinfo=dt.timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    if not games or not kickoff_iso:
        return None
    k = parse(kickoff_iso)
    g = parse(games[0].get("date"))
    if not k or not g:
        return None
    return max(0, int((k - g).total_seconds() // 86400))


def h2h_last5(home_hist, away_hist):
    """Approximate H2H by intersecting opponent ids/dates in both schedules."""
    seen = {}
    for g in (home_hist or []) + (away_hist or []):
        key = g.get("date", "")[:10]
        if key:
            seen[key] = seen.get(key, 0) + 1
    shared = sorted([k for k, v in seen.items() if v >= 2], reverse=True)[:5]
    return shared  # dates where both teams played each other (best-effort, keyless)


# --------------------------------------------------------------------------
# Feature extraction + Dixon-Coles-lite fit
# --------------------------------------------------------------------------

def _split_means(games):
    """Return (gf_home, ga_home, gf_away, ga_away, n_home, n_away) home/away split means."""
    hg = [g["gf"] for g in games if g["home"]]
    ha = [g["ga"] for g in games if g["home"]]
    ag = [g["gf"] for g in games if not g["home"]]
    aa = [g["ga"] for g in games if not g["home"]]
    f = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    return f(hg), f(ha), f(ag), f(aa), len(hg), len(ag)


def _form_weighted(games):
    """Weighted gf/ga using FORM_WEIGHTS over the most recent up-to-5 games."""
    recent = games[:len(FORM_WEIGHTS)]
    if not recent:
        return None, None, 0.0
    wsum = 0.0
    gf = ga = 0.0
    for i, g in enumerate(recent):
        w = FORM_WEIGHTS[i]
        gf += w * g["gf"]
        ga += w * g["ga"]
        wsum += w
    return gf / wsum, ga / wsum, wsum


def fit_lambdas(home_hist, away_hist, league="eng.1"):
    """Dixon-Coles-lite multiplicative fit.

    lambda_home = league_avg_home * attack_home * defense_away
    lambda_away = league_avg_away * attack_away * defense_home

    attack = team's scoring rate vs league average, defense = conceded rate vs
    league average. Home/away splits are used when present; last-5 form weights
    blend in; small samples shrink toward league priors.
    """
    prior = LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])
    lh_avg, la_avg = prior["lh"], prior["la"]

    def team_rates(hist, as_home):
        games = (hist or {}).get("games") or []
        if not games:
            return None, 0
        gf_h, ga_h, gf_a, ga_a, nh, na = _split_means(games)
        overall_gf = sum(g["gf"] for g in games) / len(games)
        overall_ga = sum(g["ga"] for g in games) / len(games)
        # pick home/away split matching the venue role, fall back to overall means
        if as_home:
            att = gf_h if gf_h is not None else overall_gf
            def_ = ga_h if ga_h is not None else overall_ga
        else:
            att = gf_a if gf_a is not None else overall_gf
            def_ = ga_a if ga_a is not None else overall_ga
        # blend with weighted recent form (last-5 weights)
        wf, wa, wsum = _form_weighted(games)
        if wf is not None:
            mix = min(0.5, wsum / sum(FORM_WEIGHTS) * 0.5)
            att = att * (1 - mix) + wf * mix
            def_ = def_ * (1 - mix) + wa * mix
        return (att, def_), len(games)

    hr, hn = team_rates(home_hist, as_home=True)
    ar, an = team_rates(away_hist, as_home=False)

    # shrinkage weight toward priors based on sample size
    def shrink(n):
        if n <= 0:
            return 0.0
        return min(1.0, n / 12.0)

    sh_h, sh_a = shrink(hn), shrink(an)

    if hr:
        att_h = hr[0] / max(0.35, (lh_avg + la_avg) / 2)
        def_a = ar[1] / max(0.35, (lh_avg + la_avg) / 2) if ar else 1.0
    else:
        att_h = def_a = 1.0
    if ar:
        att_a = ar[0] / max(0.35, (lh_avg + la_avg) / 2)
        def_h = hr[1] / max(0.35, (lh_avg + la_avg) / 2) if hr else 1.0
    else:
        att_a = def_h = 1.0

    att_h = 1.0 + (att_h - 1.0) * sh_h
    def_a = 1.0 + (def_a - 1.0) * sh_a
    att_a = 1.0 + (att_a - 1.0) * sh_a
    def_h = 1.0 + (def_h - 1.0) * sh_h

    lh = _clamp(lh_avg * att_h * def_a, *CLAMP_LAMBDA)
    la = _clamp(la_avg * att_a * def_h, *CLAMP_LAMBDA)

    # rest-day micro adjustment (fatigue bumps conceding slightly)
    return {"lambda_home": round(lh, 3), "lambda_away": round(la, 3),
            "attack_home": round(att_h, 3), "defense_home": round(def_h, 3),
            "attack_away": round(att_a, 3), "defense_away": round(def_a, 3),
            "sample_home": hn, "sample_away": an, "rho": RHO}


def team_event_rates(hist, kind, default_rate):
    """corners/cards per game from history when available, else league default.

    Returns (rate, sample_games, source). ESPN core summaries sometimes carry
    corner/card statistics for soccer; when absent we use the league default and
    the caller applies a confidence penalty.
    """
    vals = []
    for g in (hist or {}).get("games") or []:
        v = g.get(kind)
        if isinstance(v, (int, float)) and v >= 0:
            vals.append(float(v))
    if len(vals) >= 3:
        return sum(vals) / len(vals), len(vals), f"history_{kind}"
    return default_rate, 0, "league_default"


# --------------------------------------------------------------------------
# Market pricing against the taxonomy
# --------------------------------------------------------------------------

def load_taxonomy(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "markets_taxonomy.csv")
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            for r in rd:
                if len(r) >= 2:
                    rows.append((r[0].strip(), r[1].strip()))
    except Exception:  # noqa: BLE001
        rows = []
    return header or ["Sport", "Betting Market"], rows


def _fair(p):
    return None if p is None or p <= 0 else round(1.0 / p, 3)


def _tax_name(market, tax_set):
    """Map an emitted market name to its owner-taxonomy row (None if not a
    taxonomy row — those are bonus rows that don't count toward coverage)."""
    mk = (market or "").strip()
    if mk in tax_set:
        return mk
    low = mk.lower()
    # exact-ish family matches first
    for fam in ("Full-Time Result (1X2)", "Double Chance", "Draw No Bet",
                "Both Teams to Score (BTTS)", "Correct Score",
                "Half-Time/Full-Time", "1st Half Result", "2nd Half Result",
                "Exact Total Goals", "Winning Margin", "To Win to Nil",
                "Highest Scoring Half"):
        if fam.lower() == low:
            return fam
    if low.startswith("total goals over/under"):
        return "Over/Under Total Goals (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)"
    if low.startswith("team total goals"):
        return "Team Total Goals (Over/Under)"
    if "clean sheet" in low:
        return "Clean Sheet (Home/Away)"
    if low.startswith("correct score"):
        return "Correct Score"
    if low.startswith("asian handicap"):
        return "Asian Handicap"
    if low.startswith("european handicap"):
        return "European Handicap"
    if low.startswith("multi-goals") or low.startswith("multi-goal"):
        return "Multi-Goals (Bands e.g., 1-2, 2-3, 4-6)"
    if low.startswith("half time result") or low == "1st half result":
        return "1st Half Result"
    if low.startswith("2nd half result"):
        return "2nd Half Result"
    if low.startswith("1st half over/under"):
        return "1st Half Over/Under Total Goals"
    if low.startswith("2nd half over/under"):
        return "2nd Half Over/Under Total Goals"
    if low.startswith("full time / half time") or low.startswith("half-time/full-time"):
        return "Half-Time/Full-Time"
    if low.startswith("both teams score in both halves"):
        return "Both Teams to Score in Both Halves"
    if low.startswith("total corners over/under"):
        return "Total Match Corners (Over/Under)"
    if low.startswith("home team corners") or low.startswith("away team corners"):
        return "Team Total Corners"
    if low.startswith("corners 1x2") or low.startswith("most corners - 1st half"):
        return "1st Half Corners"
    if low.startswith("corner race to"):
        return "Race to X Corners"
    # --- full alias map (EDGE ENGINE item c): emitted name -> owner taxonomy row
    if low.startswith("match result") or low.startswith("full-time result"):
        return "Full-Time Result (1X2)"
    if low == "home win":
        return "Full-Time Result (1X2)"
    if low == "away win":
        return "Full-Time Result (1X2)"
    if low == "draw":
        return "Full-Time Result (1X2)"
    if low.startswith("half time result"):
        return "1st Half Result"
    if low.startswith("2nd half result"):
        return "2nd Half Result"
    if low.startswith("both teams to score"):
        return "Both Teams to Score (BTTS)"
    if low.startswith("btts & over") or low.startswith("btts & under"):
        return "Both Teams to Score (BTTS)"
    if low.startswith("exact total goals"):
        return "Exact Total Goals"
    if low.startswith("multi-goal"):
        return "Multi-Goals (Bands e.g., 1-2, 2-3, 4-6)"
    if low.startswith("team total goals"):
        return "Team Total Goals (Over/Under)"
    if "clean sheet" in low:
        return "Clean Sheet (Home/Away)"
    if low.startswith("to win to nil"):
        return "To Win to Nil"
    if low.startswith("correct score any other"):
        return "Correct Score"
    if low.startswith("correct score"):
        return "Correct Score"
    if low.startswith("european handicap"):
        return "European Handicap"
    if low.startswith("asian handicap"):
        return "Asian Handicap"
    if low.startswith("winning margin"):
        return "Winning Margin"
    if low.startswith("double chance"):
        return "Double Chance"
    if low.startswith("undefeated"):
        return "Double Chance"
    if low.startswith("draw no bet"):
        return "Draw No Bet"
    if low.startswith("1st half draw no bet"):
        return "Draw No Bet"
    if low.startswith("full time / half time") or low.startswith("half-time/full-time"):
        return "Half-Time/Full-Time"
    if low.startswith("total goals over/under"):
        return "Over/Under Total Goals (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)"
    if low.startswith("1st half over/under"):
        return "1st Half Over/Under Total Goals"
    if low.startswith("2nd half over/under"):
        return "2nd Half Over/Under Total Goals"
    if low.startswith("highest scoring half"):
        return "Highest Scoring Half"
    if low.startswith("1st half more goals"):
        return "Highest Scoring Half"
    if low.startswith("both teams score in both halves"):
        return "Both Teams to Score in Both Halves"
    if low.startswith("score at minute") or low.startswith("goal between"):
        return "Result after 15/30/60 Minutes"
    if low.startswith("lead at half-time"):
        return "1st Half Result"
    if low.startswith("total corners over/under"):
        return "Total Match Corners (Over/Under)"
    if low.startswith("home team corners") or low.startswith("away team corners"):
        return "Team Total Corners"
    if low.startswith("most corners - 1st half") \
       or low.startswith("home team over/under 6 corners in 1st half"):
        return "1st Half Corners"
    if low.startswith("corners 1x2"):
        return "Team Total Corners"
    if low.startswith("yellow card race to"):
        return "Total Match Cards / Booking Points"
    if low.startswith("total cards over/under"):
        return "Total Match Cards / Booking Points"
    if low.startswith("home team cards") or low.startswith("away team cards") \
       or low.startswith("most cards"):
        return "Team Total Cards"
    if low.startswith("red card shown"):
        return "Red Card in Match (Yes/No)"
    return None


def price_all_markets(lambda_home, lambda_away, corner_rates=None, card_rates=None,
                      league="eng.1", sample_games=None, taxonomy_path=None):
    """Price every computable football taxonomy market.

    corner_rates / card_rates: dict {home, away, sample_home, sample_away,
    source_home, source_away}; None → league defaults with confidence penalty.

    Returns list of row dicts:
      {market, selection, model_prob, model_fair_odds, sample_games, confidence}
    """
    _, tax_rows = load_taxonomy(taxonomy_path)
    tax_set = {m for (s, m) in tax_rows if s.lower().startswith("football")}

    def tax_name(market):
        return _tax_name(market, tax_set)

    lh = float(lambda_home)
    la = float(lambda_away)
    mat = score_matrix(lh, la, RHO, max_goals=10)
    half_mat = score_matrix(lh * HALF_SCALE, la * HALF_SCALE, RHO, max_goals=8)
    second_mat = half_mat  # identical distribution for 2nd half under constant-rate Poisson

    n_sample = int(sample_games if sample_games is not None
                   else 0)
    # "no-history" mode: price off league priors with an honest confidence penalty
    prior = LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"]) \
        if isinstance(LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"]), dict) \
        else LEAGUE_DEFAULTS["default"]
    if n_sample == 0:
        base_conf = 0.25
    else:
        base_conf = _clamp(0.35 + 0.05 * n_sample, 0.30, 0.90)

    cr = corner_rates or {}
    ka = card_rates or {}
    ch = float(cr.get("home", LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])["corner_home"]))
    ca = float(cr.get("away", LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])["corner_away"]))
    kh = float(ka.get("home", LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])["card_home"]))
    kv = float(ka.get("away", LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])["card_away"]))
    conf_pen = 0.0
    if cr.get("source_home", cr.get("source", "")) == "league_default" or \
       ka.get("source_home", ka.get("source", "")) == "league_default":
        conf_pen = 0.10
    conf = round(max(0.10, base_conf - conf_pen), 3)
    samp = n_sample if n_sample else 0

    rows = []

    def emit(market, selection, prob, conf_override=None, samp_override=None):
        tn = tax_name(market)
        if tn is None and market not in tax_set:
            return  # non-taxonomy guard (keeps output honest)
        rows.append({"market": market, "selection": selection,
                     "taxonomy_market": tn or market,
                     "model_prob": round(prob, 4) if prob is not None else None,
                     "model_fair_odds": _fair(prob),
                     "sample_games": samp if samp_override is None else samp_override,
                     "confidence": conf if conf_override is None else conf_override})

    # ---- Match outcome ----
    ph, pd, pa = p_home_win(mat), p_draw(mat), p_away_win(mat)
    if n_sample == 0:
        # No team history at all: emit ONLY the markets computable honestly from
        # λ priors (1X2 + O/U lines). Everything else would be fabricated rates.
        emit("Match Result (1X2)", "Home", ph)
        emit("Match Result (1X2)", "Draw", pd)
        emit("Match Result (1X2)", "Away", pa)
        for line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
            po = p_total_over(mat, line)
            emit(f"Total Goals Over/Under {line}", "Over", po)
            emit(f"Total Goals Over/Under {line}", "Under", 1.0 - po)
        coverage = {"priced": len({r.get("taxonomy_market") for r in rows
                                   if r.get("taxonomy_market")}),
                    "taxonomy_rows": len(tax_set) or 44,
                    "denominator": len(tax_set) or 44}
        return rows, {"lambda_home": lh, "lambda_away": la, "rho": RHO,
                      "half_scale": HALF_SCALE, "coverage": coverage,
                      "league": league,
                      "confidence": conf, "sample_games": 0,
                      "corner_rates": {"home": ch, "away": ca},
                      "card_rates": {"home": kh, "away": kv},
                      "_lh": lh, "_la": la, "_ch": ch, "_ca": ca,
                      "_kh": kh, "_kv": kv}
    emit("Match Result (1X2)", "Home", ph)
    emit("Match Result (1X2)", "Draw", pd)
    emit("Match Result (1X2)", "Away", pa)
    emit("Home Win", "Home", ph)
    emit("Away Win", "Away", pa)
    emit("Draw", "Draw", pd)
    emit("Double Chance (1X / 12 / X2)", "1X", ph + pd)
    emit("Double Chance (1X / 12 / X2)", "12", ph + pa)
    emit("Double Chance (1X / 12 / X2)", "X2", pd + pa)
    denom = ph + pa
    emit("Draw No Bet (Home/Away)", "Home", ph / denom if denom else None)
    emit("Draw No Bet (Home/Away)", "Away", pa / denom if denom else None)
    emit("Undefeated - Double Chance Extended", "Home Undefeated", ph + pd)
    emit("Undefeated - Double Chance Extended", "Away Undefeated", pa + pd)

    # ---- Totals O/U 0.5 .. 5.5 ----
    for line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
        po = p_total_over(mat, line)
        emit(f"Total Goals Over/Under {line}", "Over", po)
        emit(f"Total Goals Over/Under {line}", "Under", 1.0 - po)

    # ---- BTTS ----
    pbtts = p_btts(mat)
    emit("Both Teams To Score (Yes/No)", "Yes", pbtts)
    emit("Both Teams To Score (Yes/No)", "No", 1.0 - pbtts)
    emit("BTTS & Over 2.5 Goals", "Yes & Over",
         sum(mat[i][j] for i in range(1, len(mat)) for j in range(1, len(mat[0]))
             if i + j > 2.5))
    emit("BTTS & Under 2.5 Goals", "Yes & Under",
         sum(mat[i][j] for i in range(1, len(mat)) for j in range(1, len(mat[0]))
             if i >= 1 and j >= 1 and i + j < 2.5))

    # ---- Exact totals / multi-goal ----
    for t in range(0, 5):
        emit(f"Exact Total Goals ({t})", f"{t} Goals", p_total_exactly(mat, t))
    emit("Exact Total Goals (5+)", "5+ Goals", p_total_at_least(mat, 5))
    bands = {"Multi-Goal 1-2": (1, 2), "Multi-Goal 1-3": (1, 3), "Multi-Goal 2-3": (2, 3),
             "Multi-Goal 2-4": (2, 4), "Multi-Goal 3-4": (3, 4), "Multi-Goal 4-6": (4, 6)}
    for mk, (lo, hi) in bands.items():
        emit(mk, f"{lo}-{hi} Goals",
             sum(p_total_exactly(mat, t) for t in range(lo, hi + 1)))
    tot_even = sum(p_total_exactly(mat, t) for t in range(0, 21, 2))
    emit("Total Goals Odd/Even", "Even", tot_even)
    emit("Total Goals Odd/Even", "Odd", 1.0 - tot_even)

    # ---- Team totals ----
    for line in (0.5, 1.5, 2.5):
        pho = 1.0 - sum(poisson_pmf(k, lh) for k in range(int(line) + 1))
        emit(f"Team Total Goals - Home Over/Under {line}", "Home Over", pho)
        emit(f"Team Total Goals - Home Over/Under {line}", "Home Under", 1.0 - pho)
        pao = 1.0 - sum(poisson_pmf(k, la) for k in range(int(line) + 1))
        emit(f"Team Total Goals - Away Over/Under {line}", "Away Over", pao)
        emit(f"Team Total Goals - Away Over/Under {line}", "Away Under", 1.0 - pao)

    # ---- Clean sheets / win to nil ----
    p_h_cs = poisson_pmf(0, la)
    p_a_cs = poisson_pmf(0, lh)
    emit("Home Team Clean Sheet (Yes/No)", "Yes", p_h_cs)
    emit("Home Team Clean Sheet (Yes/No)", "No", 1.0 - p_h_cs)
    emit("Away Team Clean Sheet (Yes/No)", "Yes", p_a_cs)
    emit("Away Team Clean Sheet (Yes/No)", "No", 1.0 - p_a_cs)
    emit("To Win to Nil", "Home Win To Nil",
         sum(mat[i][0] for i in range(1, len(mat))))
    emit("To Win to Nil", "Away Win To Nil",
         sum(mat[0][j] for j in range(1, len(mat[0]))))

    # ---- Correct score top-20 matrix cells ----
    cells = [(i, j, mat[i][j]) for i in range(len(mat)) for j in range(len(mat[0]))]
    named = [c for c in cells if f"Correct Score {c[0]}-{c[1]}" in tax_set]
    named.sort(key=lambda c: c[2], reverse=True)
    for i, j, pv in named[:20]:
        emit(f"Correct Score {i}-{j}", f"{i}-{j}", pv)
    listed = {f"{i}-{j}" for i, j, _ in named[:20]}
    p_any_other = 1.0 - sum(pv for i, j, pv in named[:20])
    emit("Correct Score Any Other", "Any Other", max(0.0, p_any_other))

    # ---- Asian handicap ladder (taxonomy names are signed from that side's view) ----
    for line in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5):
        ph_ah = p_asian_handicap(mat, line, "home")
        emit(f"Asian Handicap {line:+.1f} (Home)", f"Home {line:+.1f}", ph_ah)
        pa_ah = p_asian_handicap(mat, line, "away")   # away gets mirrored line
        emit(f"Asian Handicap {-line:+.1f} (Away)".replace("-+", "+"),
             f"Away {-line:+.1f}", pa_ah)

    # European handicap (win by exactly n)
    emit("European Handicap 1-0 (Home)", "Home EH +1", p_margin(mat, 1, 1))
    emit("European Handicap 0-0 (Draw)", "Draw EH", p_margin(mat, 0, 0))
    emit("European Handicap 0-1 (Away)", "Away EH -1", p_margin(mat, -1, -1))

    # ---- Winning margins ----
    emit("Winning Margin 1 Goal", "Margin 1", p_margin(mat, 1, 1) + p_margin(mat, -1, -1))
    emit("Winning Margin 2 Goals", "Margin 2", p_margin(mat, 2, 2) + p_margin(mat, -2, -2))
    emit("Winning Margin 3 Goals", "Margin 3", p_margin(mat, 3, 3) + p_margin(mat, -3, -3))
    emit("Winning Margin 4+ Goals", "Margin 4+",
         p_margin(mat, 4, 99) + p_margin(mat, -99, -4))
    emit("Winning Margin - Home by 1", "Home +1", p_margin(mat, 1, 1))
    emit("Winning Margin - Home by 2", "Home +2", p_margin(mat, 2, 2))
    emit("Winning Margin - Home by 3+", "Home +3", p_margin(mat, 3, 99))
    emit("Winning Margin - Away by 1", "Away -1", p_margin(mat, -1, -1))
    emit("Winning Margin - Away by 2", "Away -2", p_margin(mat, -2, -2))
    emit("Winning Margin - Away by 3+", "Away -3", p_margin(mat, -99, -3))

    # ---- Halves (lambda scaled 0.45) ----
    hh, hd, ha_ = p_home_win(half_mat), p_draw(half_mat), p_away_win(half_mat)
    emit("Half Time Result (1X2)", "Home", hh)
    emit("Half Time Result (1X2)", "Draw", hd)
    emit("Half Time Result (1X2)", "Away", ha_)
    ht_dnb = hh + ha_
    emit("1st Half Draw No Bet", "Home", hh / ht_dnb if ht_dnb else None)
    emit("1st Half Draw No Bet", "Away", ha_ / ht_dnb if ht_dnb else None)
    emit("Lead at Half-Time (Home/Away/None)", "Home Lead", hh)
    emit("Lead at Half-Time (Home/Away/None)", "Away Lead", ha_)
    emit("Lead at Half-Time (Home/Away/None)", "No Lead", hd)
    emit("2nd Half Result (1X2)", "Home", p_home_win(second_mat))
    emit("2nd Half Result (1X2)", "Draw", p_draw(second_mat))
    emit("2nd Half Result (1X2)", "Away", p_away_win(second_mat))
    # HT/FT combos (independent halves approximation)
    for sel, p_ht, p_ft in (("Home/Home", hh, ph), ("Home/Draw", hh, pd),
                            ("Home/Away", hh, pa), ("Draw/Home", hd, ph),
                            ("Draw/Draw", hd, pd), ("Draw/Away", hd, pa),
                            ("Away/Home", ha_, ph), ("Away/Draw", ha_, pd),
                            ("Away/Away", ha_, pa)):
        emit("Full Time / Half Time Result", sel, p_ht * p_ft)
    for line in (0.5, 1.5):
        po1 = p_total_over(half_mat, line)
        emit(f"1st Half Over/Under {line}", "Over", po1)
        emit(f"1st Half Over/Under {line}", "Under", 1.0 - po1)
        po2 = p_total_over(second_mat, line)
        emit(f"2nd Half Over/Under {line}", "Over", po2)
        emit(f"2nd Half Over/Under {line}", "Under", 1.0 - po2)
    h1_more = sum(1.0 for _ in [0]) * 0.0  # computed below properly
    # P(goals in 1st half > goals in 2nd half) via convolution of two halves
    maxg = len(half_mat) + len(second_mat) - 2
    dist1 = [sum(half_mat[i][j] for i in range(len(half_mat))
                 for j in range(len(half_mat[0])) if i + j == t) for t in range(maxg + 1)]
    dist2 = [sum(second_mat[i][j] for i in range(len(second_mat))
                 for j in range(len(second_mat[0])) if i + j == t) for t in range(maxg + 1)]
    p_first_more = sum(dist1[a] * dist2[b] for a in range(len(dist1))
                       for b in range(len(dist2)) if a > b)
    p_second_more = sum(dist1[a] * dist2[b] for a in range(len(dist1))
                        for b in range(len(dist2)) if b > a)
    emit("1st Half More Goals Than 2nd Half", "1st Half", p_first_more)
    emit("1st Half More Goals Than 2nd Half", "2nd Half", p_second_more)
    emit("Highest Scoring Half (1st/2nd)", "1st Half", p_first_more)
    emit("Highest Scoring Half (1st/2nd)", "2nd Half", p_second_more)
    emit("To Win Both Halves", "Home", hh * p_home_win(second_mat))
    emit("To Win Both Halves", "Away", ha_ * p_away_win(second_mat))
    emit("Win Either Half (Home/Away)", "Home", 1.0 - (1.0 - hh) * (1.0 - p_home_win(second_mat)))
    emit("Win Either Half (Home/Away)", "Away", 1.0 - (1.0 - ha_) * (1.0 - p_away_win(second_mat)))
    # BTTS in both halves
    btts_h = p_btts(half_mat)
    emit("Both Teams Score In Both Halves", "Yes", btts_h * p_btts(second_mat))

    # ---- Timing proxies (uniform arrival within halves) ----
    p_no_goal_10 = math.exp(-(lh + la) * (10.0 / 90.0))
    emit("Goal Between 1-10 Minutes (Yes/No)", "Yes", 1.0 - p_no_goal_10)
    emit("Goal Between 1-10 Minutes (Yes/No)", "No", p_no_goal_10)
    p_no_goal_80 = math.exp(-(lh + la) * (10.0 / 90.0))
    emit("Goal Between 80-90 Minutes (Yes/No)", "Yes", 1.0 - p_no_goal_80)
    emit("Goal Between 80-90 Minutes (Yes/No)", "No", p_no_goal_80)
    p_00_15 = math.exp(-(lh + la) * (15.0 / 90.0))
    emit("Score at Minute 15 (0-0 Yes/No)", "0-0", p_00_15)
    emit("Score at Minute 15 (0-0 Yes/No)", "Not 0-0", 1.0 - p_00_15)
    p_00_30 = math.exp(-(lh + la) * (30.0 / 90.0))
    emit("Score at Minute 30 (0-0 Yes/No)", "0-0", p_00_30)
    emit("Score at Minute 30 (0-0 Yes/No)", "Not 0-0", 1.0 - p_00_30)
    p_first_home = lh / (lh + la) if (lh + la) else 0.5
    emit("First Goal - Home/Away/None", "Home", p_first_home * (1.0 - math.exp(-(lh + la))))
    emit("First Goal - Home/Away/None", "Away", (1.0 - p_first_home) * (1.0 - math.exp(-(lh + la))))
    emit("First Goal - Home/Away/None", "None", math.exp(-(lh + la)))
    emit("Home Team To Score First Goal", "Yes", p_first_home * (1.0 - math.exp(-(lh + la))))
    emit("Away Team To Score First Goal", "Yes", (1.0 - p_first_home) * (1.0 - math.exp(-(lh + la))))
    emit("Next Goal - Home/Away/None", "Home", p_first_home)
    emit("Next Goal - Home/Away/None", "Away", 1.0 - p_first_home)
    emit("Last Goal - Home/Away/None", "Home", p_first_home)
    emit("Last Goal - Home/Away/None", "Away", 1.0 - p_first_home)

    # ---- Corners (Poisson on team rates) ----
    cconf = round(conf * (0.8 if cr.get("source_home", cr.get("source", "")) == "league_default" else 1.0), 3)
    csamp = int(cr.get("sample_home", 0) or 0)
    ctot = ch + ca
    for line in (7.5, 8.5, 9.5, 10.5, 11.5, 12.5):
        po = 1.0 - sum(poisson_pmf(k, ctot) for k in range(int(line) + 1))
        emit(f"Total Corners Over/Under {line}", "Over", po, cconf, csamp)
        emit(f"Total Corners Over/Under {line}", "Under", 1.0 - po, cconf, csamp)
    for line in (4.5,):
        po_h = 1.0 - sum(poisson_pmf(k, ch) for k in range(int(line) + 1))
        emit(f"Home Team Corners Over/Under {line}", "Over", po_h, cconf, csamp)
        emit(f"Home Team Corners Over/Under {line}", "Under", 1.0 - po_h, cconf, csamp)
        po_a = 1.0 - sum(poisson_pmf(k, ca) for k in range(int(line) + 1))
        emit(f"Away Team Corners Over/Under {line}", "Over", po_a, cconf, csamp)
        emit(f"Away Team Corners Over/Under {line}", "Under", 1.0 - po_a, cconf, csamp)
    for n in (5, 7, 10):
        prh = race_to_n(ch, ca, n)
        emit(f"Corner Race to {n} Corners (Home)", "Home", prh, cconf, csamp)
        emit(f"Corner Race to {n} Corners (Away)", "Away", 1.0 - prh, cconf, csamp)
    # half corners: scale by 0.5
    p_hh = race_to_n(ch * 0.5, ca * 0.5, 3)
    emit("Most Corners - 1st Half (Home/Away/Equal)", "Home", p_hh, cconf, csamp)
    emit("Most Corners - 1st Half (Home/Away/Equal)", "Away", 1.0 - p_hh, cconf, csamp)
    emit("Most Corners - 2nd Half (Home/Away/Equal)", "Home", p_hh, cconf, csamp)
    emit("Most Corners - 2nd Half (Home/Away/Equal)", "Away", 1.0 - p_hh, cconf, csamp)
    po_h1 = 1.0 - sum(poisson_pmf(k, ch * 0.5) for k in range(7))
    emit("Home Team Over/Under 6 Corners in 1st Half", "Over", po_h1, cconf, csamp)
    emit("Home Team Over/Under 6 Corners in 1st Half", "Under", 1.0 - po_h1, cconf, csamp)
    # corners 1x2 approximated by difference of two Poissons via matrix
    cmat = score_matrix(ch, ca, 0.0, max_goals=15)
    emit("Corners 1X2 (Home/Away/Draw)", "Home", p_home_win(cmat), cconf, csamp)
    emit("Corners 1X2 (Home/Away/Draw)", "Draw", p_draw(cmat), cconf, csamp)
    emit("Corners 1X2 (Home/Away/Draw)", "Away", p_away_win(cmat), cconf, csamp)

    # ---- Cards (Poisson) ----
    kconf = round(conf * (0.8 if ka.get("source_home", ka.get("source", "")) == "league_default" else 1.0), 3)
    ksamp = int(ka.get("sample_home", 0) or 0)
    ktot = kh + kv
    for line in (2.5, 3.5, 4.5, 5.5):
        po = 1.0 - sum(poisson_pmf(kk, ktot) for kk in range(int(line) + 1))
        emit(f"Total Cards Over/Under {line}", "Over", po, kconf, ksamp)
        emit(f"Total Cards Over/Under {line}", "Under", 1.0 - po, kconf, ksamp)
    for line in (2.5,):
        po_h = 1.0 - sum(poisson_pmf(kk, kh) for kk in range(int(line) + 1))
        emit(f"Home Team Cards Over/Under {line}", "Over", po_h, kconf, ksamp)
        emit(f"Home Team Cards Over/Under {line}", "Under", 1.0 - po_h, kconf, ksamp)
        po_a = 1.0 - sum(poisson_pmf(kk, kv) for kk in range(int(line) + 1))
        emit(f"Away Team Cards Over/Under {line}", "Over", po_a, kconf, ksamp)
        emit(f"Away Team Cards Over/Under {line}", "Under", 1.0 - po_a, kconf, ksamp)
    kmat = score_matrix(kh, kv, 0.0, max_goals=12)
    emit("Most Cards - Home/Away/Equal", "Home", p_home_win(kmat), kconf, ksamp)
    emit("Most Cards - Home/Away/Equal", "Away", p_away_win(kmat), kconf, ksamp)
    emit("Most Cards - Home/Away/Equal", "Equal", p_draw(kmat), kconf, ksamp)
    pr3 = race_to_n(kh, kv, 3)
    emit("Yellow Card Race to 3 (Home)", "Home", pr3, kconf, ksamp)
    emit("Yellow Card Race to 3 (Away)", "Away", 1.0 - pr3, kconf, ksamp)
    p_red = 1.0 - math.exp(-max(0.0, ktot * 0.045))  # ~4.5% of cards are reds
    emit("Red Card Shown (Yes/No)", "Yes", p_red, kconf, ksamp)
    emit("Red Card Shown (Yes/No)", "No", 1.0 - p_red, kconf, ksamp)

    # structural markets we cannot compute honestly are NOT emitted
    # (coverage = priced taxonomy rows / football taxonomy rows)
    priced_tax = {r.get("taxonomy_market") for r in rows if r.get("taxonomy_market")}
    total_tax = len(tax_set) or 44
    coverage = {"priced": len(priced_tax),
                "taxonomy_rows": total_tax,
                "denominator": total_tax}
    return rows, {"lambda_home": lh, "lambda_away": la, "rho": RHO,
                  "half_scale": HALF_SCALE, "coverage": coverage,
                  "league": league,
                  "confidence": conf, "sample_games": samp,
                  "corner_rates": {"home": ch, "away": ca},
                  "card_rates": {"home": kh, "away": kv},
                  "_lh": lh, "_la": la, "_ch": ch, "_ca": ca,
                  "_kh": kh, "_kv": kv}


# --------------------------------------------------------------------------
# Convenience wrapper used by hunt_match.py
# --------------------------------------------------------------------------

def _event_rates_from_hist(hist, kind, default_rate):
    """EDGE ENGINE: mean corners/cards per game from history (any role), else None."""
    vals = [float(g[kind]) for g in (hist or {}).get("games") or []
            if isinstance(g.get(kind), (int, float)) and g.get(kind) >= 0]
    if len(vals) >= 3:
        return sum(vals) / len(vals)
    return default_rate


def feature_prob_outcome(home_hist, away_hist, meta, wind_mph=None):
    """EDGE ENGINE ensemble estimator #2: form-weighted logistic on features
    (home/away goal rates, last-5 form, rest days, H2H last-5, wind).

    stdlib-only; returns (p_home, p_draw, p_away) — never raises. With no
    history it collapses to the Poisson λ's (honest degenerate case)."""
    try:
        lh = float(meta.get("lambda_home", 1.4))
        la = float(meta.get("lambda_away", 1.1))
        h_gf_h, h_ga_h, h_gf_a, h_ga_a, nh, _ = _split_means(
            (home_hist or {}).get("games") or [])
        a_gf_h, a_ga_h, a_gf_a, a_ga_a, _, na = _split_means(
            (away_hist or {}).get("games") or [])
        hf, ha_ = _form_weighted((home_hist or {}).get("games") or [])
        af, aa = _form_weighted((away_hist or {}).get("games") or [])
        r_h = meta.get("rest_days_home")
        r_a = meta.get("rest_days_away")
        x = [lh - la,
             (hf - ha_) if hf is not None else 0.0,
             (af - aa) if af is not None else 0.0,
             ((h_gf_h or lh) - (a_ga_a or la)),
             ((a_gf_a or la) - (h_ga_h or la)),
             max(-2.0, min(2.0, ((r_h or 7) - (r_a or 7)) / 7.0)),
             max(-1.5, min(1.5, ((wind_mph or 0.0) - 8.0) / 10.0))]
        w = [1.15, 0.55, 0.45, 0.40, 0.35, 0.12, 0.05]
        b = -0.25                                   # league home-edge baseline
        z_home = b + sum(wi * xi for wi, xi in zip(w, x))
        z_away = -z_home
        p_h_raw = 1.0 / (1.0 + math.exp(-z_home))
        p_a_raw = 1.0 / (1.0 + math.exp(-z_away))
        # draw share anchored on the Poisson matrix draw probability
        p_d = max(0.05, min(0.35, la and (1.0 - abs(p_h_raw - p_a_raw)) * 0.35 or 0.25))
        p_d = max(0.06, min(0.32, p_d))
        s = p_h_raw + p_a_raw
        p_h = p_h_raw / s * (1.0 - p_d)
        p_a = p_a_raw / s * (1.0 - p_d)
        tot = p_h + p_d + p_a
        return p_h / tot, p_d / tot, p_a / tot
    except Exception:  # noqa: BLE001 — ensemble must never break a hunt
        try:
            mat = score_matrix(float(meta.get("lambda_home", 1.4)),
                               float(meta.get("lambda_away", 1.1)), RHO, max_goals=10)
            return p_home_win(mat), p_draw(mat), p_away_win(mat)
        except Exception:  # noqa: BLE001
            return None


def blend_weights(calibration=None):
    """Return (w_poisson, w_feature) from rolling calibration Briers.

    Default 60/40 toward Poisson; shifts toward whichever estimator has the
    better (lower) rolling Brier, clamped to [0.40, 0.75]."""
    wp = 0.60
    try:
        cal = calibration or {}
        bp = cal.get("brier_poisson")
        bf = cal.get("brier_feature")
        n = cal.get("graded_samples", 0)
        if isinstance(bp, (int, float)) and isinstance(bf, (int, float)) and n >= 20:
            # softmax-ish shift proportional to Brier advantage
            wp = 0.60 + 3.0 * (bf - bp)
            wp = max(0.40, min(0.75, wp))
    except Exception:  # noqa: BLE001
        pass
    return round(wp, 3), round(1.0 - wp, 3)


def load_calibration(path=None):
    """Read data/calibration.json if present (never raises)."""
    p = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "data", "calibration.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def build_fixture_model(fixture, team_ids=None, mock_histories=None):
    """Full pipeline for one fixture: ingest → fit → price.

    fixture: dict with home/away/league/kickoff_utc (+ optional team ids).
    mock_histories: optional {"home": [game dicts], "away": [...]} to run the
    whole pipeline fully offline (tests / blocked network).
    Returns (rows, meta). Never raises.
    """
    league = fixture.get("league") or "eng.1"
    sport, _ = _team_sport_league(fixture)
    ids = team_ids or {}
    mh = mock_histories or {}
    if mh.get("home") is not None:
        home_hist = {"games": mh["home"], "sample_games": len(mh["home"]),
                     "standings": None, "source": "mock"}
    else:
        home_hist = ingest_team_history(ids.get("home"), sport, league) if ids.get("home") \
            else {"games": [], "sample_games": 0, "source": "no_team_id"}
    if mh.get("away") is not None:
        away_hist = {"games": mh["away"], "sample_games": len(mh["away"]),
                     "standings": None, "source": "mock"}
    else:
        away_hist = ingest_team_history(ids.get("away"), sport, league) if ids.get("away") \
            else {"games": [], "sample_games": 0, "source": "no_team_id"}

    fit = fit_lambdas(home_hist, away_hist, league)
    prior = LEAGUE_DEFAULTS.get(league, LEAGUE_DEFAULTS["default"])

    ch, sn_h, src_h = team_event_rates(home_hist, "corners", prior["corner_home"])
    ca, sn_a, _ = team_event_rates(away_hist, "corners", prior["corner_away"])
    kh, cn_h, ksrc_h = team_event_rates(home_hist, "cards", prior["card_home"])
    kv, cn_a, _ = team_event_rates(away_hist, "cards", prior["card_away"])

    corner_rates = {"home": ch, "away": ca, "sample_home": sn_h, "sample_away": sn_a,
                    "source_home": src_h}
    card_rates = {"home": kh, "away": kv, "sample_home": cn_h, "sample_away": cn_a,
                  "source_home": ksrc_h}

    rows, meta = price_all_markets(fit["lambda_home"], fit["lambda_away"],
                                   corner_rates, card_rates, league=league,
                                   sample_games=min(home_hist.get("sample_games", 0),
                                                   away_hist.get("sample_games", 0))
                                   if home_hist.get("sample_games") and away_hist.get("sample_games")
                                   else max(home_hist.get("sample_games", 0),
                                            away_hist.get("sample_games", 0)))
    meta.update({
        "fit": fit,
        "rest_days_home": rest_days_from_games(home_hist.get("games"), fixture.get("kickoff_utc")),
        "rest_days_away": rest_days_from_games(away_hist.get("games"), fixture.get("kickoff_utc")),
        "h2h_dates": h2h_last5(home_hist.get("games"), away_hist.get("games")),
        "history_source": {"home": home_hist.get("source"), "away": away_hist.get("source")},
    })
    _ensemble_pass(fixture, rows, meta, home_hist, away_hist)
    return rows, meta


def _ensemble_pass(fixture, rows, meta, home_hist, away_hist, blended_override=None):
    """EDGE ENGINE item (b), FIXED per ENSEMBLE DERIVED-PROB SPEC.

    (bH,bD,bA) = blended 1X2 triple (sums to 1). Rules:
      1. Pure 1X2-function markets (Full-Time Result, Double Chance, Draw No
         Bet) are computed EXACTLY from the triple -> ensemble_kind "model".
      2. Side-tied matrix markets (Asian/European Handicap, Winning Margin,
         Team Totals, To Win To Nil, Clean Sheet, half/FT result families):
         ratio = (covered-set sum of blended triple for that side) / (same
         covered-set sum of Poisson triple); applied uniformly to every
         selection in the market -> ensemble_kind "derived".
      3. Non-side-tied matrix markets (O/U totals, BTTS, Correct Score,
         corners, cards, multi-goal bands) keep the Poisson prob unchanged
         -> ensemble_kind "poisson-only".
    Grader/Brier/calibration score ONLY rows with ensemble_kind "model"."""
    try:
        # price on the FINAL (context-adjusted) lambdas when present
        mat = score_matrix(meta.get("lambda_home"), meta.get("lambda_away"),
                           RHO, max_goals=10)
        pois = {"Home": p_home_win(mat), "Draw": p_draw(mat), "Away": p_away_win(mat)}
        if blended_override:
            # re-priced after context adjustments: reuse the SAME blended 1X2
            # triple so every row keeps one consistent ensemble across re-pricings
            bH, bD, bA = (float(blended_override[k]) for k in ("Home", "Draw", "Away"))
            blended = {"Home": bH, "Draw": bD, "Away": bA}
            meta["ensemble"] = {"w_poisson": None, "w_feature": None,
                                "calibration_samples": None,
                                "outcome_poisson": {k: round(v, 4) for k, v in pois.items()},
                                "outcome_blended": {"Home": round(bH, 4),
                                                    "Draw": round(bD, 4),
                                                    "Away": round(bA, 4)},
                                "note": "re-derived on adjusted lambdas"}
        else:
            feat = feature_prob_outcome(home_hist, away_hist, meta,
                                        wind_mph=(fixture.get("weather_wind_mph")))
            cal = load_calibration()
            wp, wf = blend_weights(cal)
            fdict = {"Home": feat[0], "Draw": feat[1], "Away": feat[2]} \
                if feat else dict(pois)
            bH = wp * pois["Home"] + wf * fdict["Home"]
            bD = wp * pois["Draw"] + wf * fdict["Draw"]
            bA = wp * pois["Away"] + wf * fdict["Away"]
            tsum = bH + bD + bA or 1.0
            bH, bD, bA = bH / tsum, bD / tsum, bA / tsum   # blended triple sums to 1
            blended = {"Home": bH, "Draw": bD, "Away": bA}
            meta["ensemble"] = {"w_poisson": wp, "w_feature": wf,
                                "calibration_samples": int(cal.get("graded_samples", 0)),
                                "outcome_poisson": {k: round(v, 4) for k, v in pois.items()},
                                "outcome_feature": {k: round(v, 4) for k, v in fdict.items()},
                                "outcome_blended": {"Home": round(bH, 4),
                                                    "Draw": round(bD, 4),
                                                    "Away": round(bA, 4)}}

        def covered_sets(market_l, sel_l):
            """Return the set of 1X2 outcomes a selection covers, plus its
            family tag ('dc', 'dnb', 'ft') or None."""
            s = sel_l.strip().lower()
            m = market_l
            if "full-time result" in m or "1x2" in m:
                if s in ("home", "1"):
                    return {"Home"}, "ft"
                if s in ("draw", "x"):
                    return {"Draw"}, "ft"
                if s in ("away", "2"):
                    return {"Away"}, "ft"
                return None, None
            if "double chance" in m or "undefeated" in m:
                if s in ("home undefeated",):
                    return {"Home", "Draw"}, "dc"
                if s in ("away undefeated",):
                    return {"Draw", "Away"}, "dc"
                if s in ("1x", "home/draw", "home or draw"):
                    return {"Home", "Draw"}, "dc"
                if s in ("12", "home/away", "home or away", "no draw", "any winner"):
                    return {"Home", "Away"}, "dc"
                if s in ("x2", "draw/away", "draw or away"):
                    return {"Draw", "Away"}, "dc"
                return None, None
            if "draw no bet" in m:
                if "home" in s:
                    return {"Home"}, "dnb"
                if "away" in s:
                    return {"Away"}, "dnb"
                return None, None
            return None, None

        SIDE_WORDS = (("home", "Home"), ("away", "Away"), ("draw", "Draw"))
        SIDE_TIED_FAMS = ("handicap", "winning margin", "win to nil", "clean sheet",
                          "result after", "highest scoring half")
        HALF_RESULT_FAMS = ("1st half result", "2nd half result",
                            "half-time/full-time", "ht/ft", "result ht/ft")

        def side_of(sel_l):
            for w, k in SIDE_WORDS:
                if w == "draw":
                    if sel_l == "draw" or "draw" in sel_l and "home" not in sel_l \
                            and "away" not in sel_l:
                        return k
                elif w in sel_l:
                    return k
            return None

        for r in rows:
            tn = (r.get("taxonomy_market") or "")
            mk = ((r.get("market") or "") + " " + tn).lower()
            sel = str(r.get("selection") or "")
            sel_l = sel.strip().lower()
            pp = r.get("model_prob")
            fp = None
            bp = None
            kind = None
            sets, fam = covered_sets(mk, sel_l)
            if sets is not None and fam == "ft":
                # Rule 2a: pure function of the triple
                fp = blended[next(iter(sets))]
                bp = fp
                kind = "model"
            elif sets is not None and fam == "dc":
                # Rule 2: Double Chance = exact pairwise sum of blended triple
                val = sum(blended[k] for k in sets)
                fp = val
                bp = val
                kind = "model"
            elif sets is not None and fam == "dnb":
                # Rule 2: DNB renormalizes over the two winner outcomes
                den = blended["Home"] + blended["Away"]
                val = (blended[next(iter(sets))] / den) if den > 0 else pp
                fp = val
                bp = val
                kind = "model"
            elif any(t in mk for t in HALF_RESULT_FAMS) or \
                    (("result" in mk) and ("half" in mk)):
                # Rule 3: side-tied (halves / HT-FT families) -> covered-set ratio
                side = side_of(sel_l)
                if side:
                    bset = {side}
                    if "full-time" in mk or "ht/ft" in mk:
                        pass  # HT-FT combos: use the named side only (approx)
                    bs = sum(blended[k] for k in bset)
                    ps = sum(pois[k] for k in bset) or 1e-9
                    bp = max(0.0, min(0.999, (pp or 0.0) * (bs / ps)))
                    kind = "derived"
            elif any(t in mk for t in SIDE_TIED_FAMS) or \
                    ("handicap" in mk) or ("margin" in mk) or \
                    ("team total" in mk) or ("to nil" in mk) or ("clean sheet" in mk):
                # Rule 3: side-tied matrix markets -> uniform covered-set ratio
                side = side_of(sel_l)
                # infer side from market name when selection lacks it
                # (e.g. "Team Total Goals — Home Over 1.5")
                if side is None:
                    if "home" in mk.split("—")[0] or "home" in (r.get("market") or "").lower():
                        side = "Home"
                    elif "away" in mk.split("—")[0] or "away" in (r.get("market") or "").lower():
                        side = "Away"
                if side:
                    bs = blended[side]
                    ps = pois[side] or 1e-9
                    bp = max(0.0, min(0.999, (pp or 0.0) * (bs / ps)))
                    kind = "derived"
                else:
                    # non-side-tied instance of a side-tied family (e.g.
                    # Winning Margin bands, No-Lead, minute-window Yes/No):
                    # Rule 4 — keep Poisson prob unchanged
                    bp = pp
                    kind = "poisson-only"
            else:
                # Rule 4: non-side-tied -> Poisson prob unchanged
                bp = pp
                kind = "poisson-only"
            if kind is None:
                bp = pp
                kind = "poisson-only"
            r["poisson_prob"] = round(pp, 4) if pp is not None else None
            r["feature_prob"] = round(fp, 4) if fp is not None else None
            r["blended_prob"] = round(bp, 4) if bp is not None else None
            r["ensemble_kind"] = kind
    except Exception as e:  # noqa: BLE001 — ensemble must never break pricing
        meta["ensemble"] = {"error": type(e).__name__}



def reprice_with_context(base_rows, base_meta, adj_meta, blended_triple,
                         home_hist=None, away_hist=None):
    """EDGE ENGINE item (b): re-price all taxonomy markets on context-adjusted
    lambdas/rates while keeping the SAME blended 1X2 triple from the original
    ensemble pass (so poisson/feature/blended stay coherent per row).

    Returns (rows, meta). Never raises — falls back to base rows on error."""
    try:
        lh = float(adj_meta.get("lambda_home", base_meta.get("lambda_home")))
        la = float(adj_meta.get("lambda_away", base_meta.get("lambda_away")))
        cr = adj_meta.get("corner_rates") or base_meta.get("corner_rates")
        ka = adj_meta.get("card_rates") or base_meta.get("card_rates")
        # league lives in meta["fit"]["league"] (price_all_markets nests it)
        _lg = base_meta.get("league") or (base_meta.get("fit") or {}).get("league")
        rows, meta = price_all_markets(lh, la, cr, ka,
                                       league=_lg,
                                       sample_games=base_meta.get("sample_games", 0))
        meta.update({k: base_meta.get(k) for k in
                     ("fit", "rest_days_home", "rest_days_away", "h2h_dates",
                      "history_source", "coverage") if k in base_meta})
        meta["context_applied"] = adj_meta.get("context_applied", [])
        _ensemble_pass({"home": "", "away": ""}, rows, meta,
                       home_hist or {"games": []}, away_hist or {"games": []},
                       blended_override=blended_triple)
        return rows, meta
    except Exception:  # noqa: BLE001
        return list(base_rows), dict(base_meta)


if __name__ == "__main__":  # tiny smoke demo
    rows, meta = price_all_markets(1.6, 1.1, None, None, league="eng.1", sample_games=14)
    print(json.dumps(meta["coverage"], indent=1))
    print(f"{len(rows)} priced rows; sample:", json.dumps(rows[:3], indent=1))
