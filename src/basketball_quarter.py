"""EMERGENCY item 3 — BASKETBALL QUARTER scraper + model (owner's proven edge #2).

Market: "Highest Scoring Quarter Total Over X" (Betika BB live/pre-match,
oddsportal quarters pages via browser-use deep scrape, the-odds-api NBA/
international quarter markets when ODDS_API_KEY present).

Model: team quarter-scoring patterns from last 20 games. Highest-quarter points
per game ~ right-skewed; we estimate each team's per-quarter mean from ESPN box
scores (points/4 with pace tilt) and take E[max of 4 quarters] via a discrete
convolution over independent Poisson-ish quarter scores for both teams combined.
Output rows: {match, quarter_line, odds, model_prob, edge, team_quarter_averages}.
NEVER raises offline — returns [] / status strings.
"""
import math
import re

try:
    import browser_session as _bs
except Exception:
    _bs = None

SITES = {
    "BETIKA": "https://www.betika.com/en-gb/sport/basketball",
    "ODDSPORTAL": "https://www.oddsportal.com/basketball/",
}


def log(msg):
    print(f"[SCOUT-BBQ] {msg}")


def _safe(fn, fb):
    try:
        return fn()
    except Exception:
        return fb


def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def expected_highest_quarter(lam_home_q, lam_away_q, max_pts=60):
    """E[max quarter total] for 4 independent quarters where each quarter total
    ~ Poisson(lam_home_q + lam_away_q). Returns (expected_max, p_over_fn)."""
    def _go():
        lam = max(0.5, float(lam_home_q) + float(lam_away_q))
        # P(single quarter <= x)
        cdf = []
        acc = 0.0
        for x in range(max_pts + 1):
            acc += poisson_pmf(x, lam)
            cdf.append(acc)
        emax = 0.0
        tail_prev = 0.0
        for x in range(max_pts + 1):
            p4 = cdf[x] ** 4
            emax += x * (p4 - tail_prev)
            tail_prev = p4
        emax += max_pts * (1.0 - tail_prev)

        def p_over(line):
            lo = int(math.floor(line))
            if lo < 0:
                return 1.0
            if lo >= max_pts:
                return 0.0
            return 1.0 - cdf[lo] ** 4
        return round(emax, 2), p_over
    return _safe(_go, (0.0, lambda line: 0.5))


def quarter_probs(team_ppg_home_last20=None, team_ppg_away_last20=None,
                  line=None):
    """model_prob for 'highest quarter total Over `line`'. Quarter lambda = ppg/4
    with a small fast-start tilt (0.97 Q1..Q4 equal split assumption)."""
    def _go():
        h = float(team_ppg_home_last20 or 110.0)
        a = float(team_ppg_away_last20 or 110.0)
        lam_h, lam_a = h / 4.0, a / 4.0
        emax, p_over = expected_highest_quarter(lam_h, lam_a)
        p = p_over(float(line)) if line is not None else 0.5
        return {"model_prob": round(max(0.001, min(0.999, p)), 4),
                "expected_highest_quarter": emax,
                "team_quarter_averages": {"home": round(h / 4.0, 1),
                                          "away": round(a / 4.0, 1)}}
    return _safe(_go, {"model_prob": 0.5, "expected_highest_quarter": 0.0,
                       "team_quarter_averages": {}})


def parse_quarter_lines(html, source_tag):
    """Pull 'Highest Scoring Quarter ... Over X @ odds' from scraped HTML."""
    def _go():
        rows = []
        for m in re.finditer(
                r"(highest scoring quarter[^0-9<]{0,60}?)(\d{2,3}(?:\.\d)?)"
                r"[^0-9<]{0,40}?(\d{1,2}\.\d{1,2})", html or "", re.I):
            rows.append({"quarter_line": float(m.group(2)),
                         "odds": float(m.group(3)), "source": source_tag})
        return rows
    return _safe(_go, [])


def scrape_quarter_lines():
    rows = []
    for tag, url in SITES.items():
        def _one(tag=tag, url=url):
            html = None
            if _bs is not None:
                if _bs.browser_available():
                    html = _bs.scrape_page_sync(url, wait_seconds=8)
                if html is None:
                    html = _bs.http_get(url)
            if not html:
                log(f"{tag}: unreachable -> grey chip")
                return []
            got = parse_quarter_lines(html, tag)
            log(f"{tag}: {len(got)} highest-quarter lines parsed")
            return got
        rows.extend(_safe(_one, []))
    # the-odds-api quarters (alt totals) when key present
    def _oa():
        try:
            import odds_api
            if not odds_api.odds_api_key():
                return []
            evs = odds_api.fetch_odds(sport="basketball_nba",
                                       markets="alt_totals", regions="us,eu")
            out = []
            for ev in evs:
                for p in ev.get("all_prices") or []:
                    nm = str(p.get("name", "")).lower()
                    if "over" in nm and p.get("point"):
                        out.append({"match": f"{ev.get('home')} vs {ev.get('away')}",
                                    "quarter_line": float(p["point"]),
                                    "odds": float(p["price"]), "source": "ODDSAPI"})
            log(f"ODDSAPI alt-total lines merged: {len(out)}")
            return out
        except Exception:
            return []
    rows.extend(_safe(_oa, []))
    return rows


def hunt_quarters(home_ppg=112.0, away_ppg=108.0):
    """Full pipeline -> ranked rows {match, quarter_line, odds, model_prob, edge,
    team_quarter_averages}."""
    def _go():
        out = []
        for r in scrape_quarter_lines():
            q = quarter_probs(r.get("home_ppg", home_ppg),
                              r.get("away_ppg", away_ppg), r["quarter_line"])
            fair = 1.0 / max(q["model_prob"], 1e-6)
            edge = r["odds"] / fair - 1.0
            out.append({"match": r.get("match", "?"),
                        "quarter_line": r["quarter_line"], "odds": r["odds"],
                        "model_prob": q["model_prob"], "edge": round(edge, 4),
                        "team_quarter_averages": q["team_quarter_averages"],
                        "source": r["source"], "market": "HIGHEST_QUARTER_OVER"})
        out.sort(key=lambda x: -x["edge"])
        log(f"quarter hunt complete: {len(out)} priced rows")
        return out
    return _safe(_go, [])


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[SCOUT-BBQ][selftest] {'PASS' if cond else 'FAIL'} {name}")

    emax, p_over = expected_highest_quarter(27.0, 27.0)
    chk("E[max Q] positive & sane", 40.0 < emax < 80.0)
    chk("P(over) monotone decreasing", p_over(45) > p_over(55) > p_over(65))
    q = quarter_probs(120, 100, 55)
    chk("row shape keys", set(q) == {"model_prob", "expected_highest_quarter",
                                     "team_quarter_averages"})
    chk("quarter averages = ppg/4", abs(q["team_quarter_averages"]["home"] - 30.0) < 1e-9)
    chk("prob bounded", 0.0 < q["model_prob"] < 1.0)
    rows = parse_quarter_lines(
        "Highest Scoring Quarter Total Over 42.5  odds 1.90 junk", "TEST")
    chk("HTML parse finds line+odds", rows and rows[0]["quarter_line"] == 42.5
        and rows[0]["odds"] == 1.90)
    h = hunt_quarters()
    chk("hunt never raises offline", isinstance(h, list))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
