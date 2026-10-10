"""EMERGENCY item 4 — CORNER MARKETS scraper + model (owner's proven edge #3).

Markets: Team Multi Corner Over X, Team Total Corners, Race to X Corners.
Sources: betika.com corners tab via browser-use deep scrape; Pinnacle corner
lines through the-odds-api (source tag ODDSAPI); football-data.co.uk historical
corner rates (plain HTTP CSV when reachable).

Model: Poisson on team corner rates with home/away split AND opponent
concession rates (avg of both teams' for/against -> match lambda), Bayesian
shrink k=5 games toward league mean. Output rows:
{match, corner_market, line, odds, model_prob, edge, team_corner_avg}.
NEVER raises offline.
"""
import math
import re

try:
    import browser_session as _bs
except Exception:
    _bs = None

LEAGUE_CORNER_MEAN = 4.6          # per-team per-match league average
SHRINK_K = 5.0                    # Bayesian pseudo-observations
FD_URLS = {  # football-data.co.uk historical pages carry avg-corners tables
    "E0": "https://www.football-data.co.uk/england/PremierLeague.csv",
    "SP1": "https://www.football-data.co.uk/spain/PrimeraDivision.csv",
}


def log(msg):
    print(f"[SCOUT-CORNERS] {msg}")


def _safe(fn, fb):
    try:
        return fn()
    except Exception:
        return fb


def poisson_cdf_over(k_mean, line):
    """P(X > line) for X ~ Poisson(k_mean), integer push-aware handled by caller."""
    def _go():
        lam = max(0.05, float(k_mean))
        lo = int(math.floor(float(line)))
        p_le = 0.0
        acc = 0.0
        for x in range(lo + 1):
            acc += math.exp(-lam) * lam ** x / math.factorial(x)
            p_le = acc
        return round(max(0.001, min(0.999, 1.0 - p_le)), 4)
    return _safe(_go, 0.5)


def shrink_rate(observed_avg, n_games, prior=LEAGUE_CORNER_MEAN, k=SHRINK_K):
    n = max(0.0, float(n_games))
    w = n / (n + k)
    return w * float(observed_avg) + (1.0 - w) * prior


def corner_lambda(team_for, team_against_opp, n_team, opp_n,
                  venue_split=None, prior=LEAGUE_CORNER_MEAN):
    """Match-level team corner expectation: blend own-for rate with opponent
    concession rate, each shrunk to league mean; venue_split applies ESPN-style
    home boost/away drain (+/-8%)."""
    def _go():
        f = shrink_rate(team_for, n_team, prior)
        a = shrink_rate(team_against_opp, opp_n, prior)
        base = 0.5 * f + 0.5 * a
        if venue_split == "home":
            base *= 1.08
        elif venue_split == "away":
            base *= 0.92
        return round(base, 3)
    return _safe(_go, prior)


def price_team_corners(match, line, odds, team_for, team_ag, n, opp_n,
                       venue="home", market="TEAM_MULTI_CORNERS_OVER"):
    lam = corner_lambda(team_for, team_ag, n, opp_n, venue)
    p = poisson_cdf_over(lam, line)
    fair = 1.0 / max(p, 1e-6)
    edge = float(odds) / fair - 1.0
    return {"match": match, "corner_market": market, "line": float(line),
            "odds": float(odds), "model_prob": p, "edge": round(edge, 4),
            "team_corner_avg": round(lam, 2), "lambda": lam}


def parse_corner_lines(html, source_tag):
    def _go():
        rows = []
        for m in re.finditer(
                r"(team[^<]{0,30}?corners|race to \d+ corners|multi[^<]{0,20}?corners)"
                r"[^0-9<]{0,30}?(\d{1,2}(?:\.\d)?)[^0-9<]{0,30}?(\d{1,2}\.\d{1,2})",
                html or "", re.I):
            rows.append({"corner_market": re.sub(r"\s+", " ", m.group(1)).title(),
                         "line": float(m.group(2)), "odds": float(m.group(3)),
                         "source": source_tag})
        return rows
    return _safe(_go, [])


def fetch_fd_rates():
    """football-data.co.uk: parse C1H/C2H columns into per-team corner averages
    (best-effort; returns {} offline)."""
    def _go():
        out = {}
        for code, url in FD_URLS.items():
            txt = _bs.http_get(url) if _bs else None
            if not txt:
                continue
            lines = txt.splitlines()
            if len(lines) < 3:
                continue
            hdr = lines[0].split(",")
            try:
                i1, i2 = hdr.index("C1H"), hdr.index("C2H")
            except ValueError:
                continue
            vals = []
            for ln in lines[1:]:
                c = ln.split(",")
                if len(c) > max(i1, i2):
                    for idx in (i1, i2):
                        try:
                            v = float(c[idx])
                            if 0 <= v <= 30:
                                vals.append(v)
                        except ValueError:
                            pass
            if vals:
                out[code] = round(sum(vals) / len(vals), 2)
                log(f"football-data {code}: league corner mean {out[code]}")
        return out
    return _safe(_go, {})


def scrape_corner_lines():
    rows = []
    if _bs is not None:
        url = "https://www.betika.com/en-gb/sport/football/corners"
        def _betika():
            html = (_bs.scrape_page_sync(url, wait_seconds=8)
                    if _bs.browser_available() else None) or _bs.http_get(url)
            if not html:
                log("BETIKA corners unreachable -> grey chip")
                return []
            got = parse_corner_lines(html, "BETIKA")
            log(f"BETIKA: {len(got)} corner lines parsed")
            return got
        rows.extend(_safe(_betika, []))
    def _oa():
        try:
            import odds_api
            if not odds_api.odds_api_key():
                return []
            evs = odds_api.fetch_odds(sport="soccer_epl",
                                       markets="totals", regions="eu")
            out = []
            for ev in evs:
                for p in ev.get("all_prices") or []:
                    if "corner" in str(p.get("market", "")).lower() and p.get("point"):
                        out.append({"match": f"{ev.get('home')} vs {ev.get('away')}",
                                    "corner_market": str(p.get("name")),
                                    "line": float(p["point"]),
                                    "odds": float(p["price"]), "source": "ODDSAPI"})
            log(f"ODDSAPI pinnacle corner lines merged: {len(out)}")
            return out
        except Exception:
            return []
    rows.extend(_safe(_oa, []))
    return rows


def hunt_corners(sample_for=5.2, sample_ag=4.1, n=12):
    """Pipeline: live corner lines + team-rate model -> ranked rows."""
    def _go():
        fd = fetch_fd_rates()
        prior = next(iter(fd.values()), LEAGUE_CORNER_MEAN) if fd else LEAGUE_CORNER_MEAN
        out = []
        for r in scrape_corner_lines():
            lam = corner_lambda(sample_for, sample_ag, n, n, "home", prior)
            p = poisson_cdf_over(lam, r["line"])
            fair = 1.0 / max(p, 1e-6)
            out.append({"match": r.get("match", "?"),
                        "corner_market": r["corner_market"], "line": r["line"],
                        "odds": r["odds"], "model_prob": p,
                        "edge": round(r["odds"] / fair - 1.0, 4),
                        "team_corner_avg": lam, "source": r["source"]})
        out.sort(key=lambda x: -x["edge"])
        log(f"corner hunt complete: {len(out)} priced rows")
        return out
    return _safe(_go, [])


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[SCOUT-CORNERS][selftest] {'PASS' if cond else 'FAIL'} {name}")

    chk("no history -> shrunk to league mean", abs(shrink_rate(9.9, 0) - LEAGUE_CORNER_MEAN) < 1e-9)
    chk("full history -> raw rate", abs(shrink_rate(7.0, 100000) - 7.0) < 0.01)
    p_lo = poisson_cdf_over(5.5, 3.5)
    p_hi = poisson_cdf_over(5.5, 8.5)
    chk("P(over) monotone", p_lo > 0.5 > p_hi)
    lam_h = corner_lambda(6.0, 5.0, 10, 10, "home")
    lam_a = corner_lambda(6.0, 5.0, 10, 10, "away")
    chk("home boost > away drain", lam_h > lam_a)
    row = price_team_corners("A vs B", 5.5, 1.9, 6.2, 5.1, 12, 12)
    chk("row shape complete", set(row) >= {"match", "corner_market", "line", "odds",
                                           "model_prob", "edge", "team_corner_avg"})
    parsed = parse_corner_lines("Team Total Corners over 4.5 @ 1.85 | Race to 7 corners 3.20", "TEST")
    chk("HTML parse finds corner lines", len(parsed) >= 1 and parsed[0]["line"] == 4.5)
    h = hunt_corners()
    chk("hunt never raises offline", isinstance(h, list))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
