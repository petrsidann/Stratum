#!/usr/bin/env python3
"""Stratum standalone slate scanner. Always writes data/market_universe.json."""
import json, os, time, datetime as dt
from urllib import request as urlreq
from urllib import error as urlerr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

HEADER_SETS = [
    {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json"},
]

LEAGUES = {
    "soccer": ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "usa.1",
               "uefa.champions", "uefa.europa", "uefa.nations", "fifa.world", "fifa.friendly"],
    "basketball": ["nba"],
    "football": ["nfl"],
    "baseball": ["mlb"],
    "hockey": ["nhl"],
}
SPORT_KEY = {"soccer": "soccer", "basketball": "nba", "football": "nfl",
             "baseball": "mlb", "hockey": "nhl"}


def http_get(url, timeout=15):
    last = None
    for headers in HEADER_SETS:
        try:
            req = urlreq.Request(url, headers=headers)
            with urlreq.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urlerr.HTTPError as e:
            last = f"http_{e.code}"
        except Exception as e:
            last = f"error_{type(e).__name__}"
    return None, last


def am_to_dec(ml):
    try:
        ml = float(ml)
    except Exception:
        return None
    if ml == 0:
        return None
    return round(1 + ml / 100, 3) if ml > 0 else round(1 + 100 / abs(ml), 3)


def best_and_avg(vals):
    vals = [v for v in vals if v]
    if not vals:
        return None, None
    return max(vals), sum(vals) / len(vals)


def add_rows(rows, market_name, sel_odds, prov_count):
    sels = [s for s in sel_odds if sel_odds[s]]
    if len(sels) < 2:
        return
    avgs = {}
    for s in sels:
        b, a = best_and_avg(sel_odds[s])
        if not a:
            return
        avgs[s] = (b, a)
    implied_sum = sum(1 / avgs[s][1] for s in sels)
    if implied_sum <= 0:
        return
    for s in sels:
        b, a = avgs[s]
        p = (1 / a) / implied_sum
        fair = 1 / p
        edge = (b / fair - 1) * 100
        kelly = 0.0
        if edge > 0 and b > 1:
            k = (p * (b - 1) - (1 - p)) / (b - 1)
            kelly = round(max(0.0, k) * 25, 2)
        rows.append({
            "market": market_name,
            "selection": s,
            "book_odds": b,
            "fair_odds": round(fair, 3),
            "ev_percent": round(edge, 2),
            "confidence_score": int(max(1, min(99, round(p * 100)))),
            "kelly_stake_pct": kelly,
            "reasoning_summary": (
                f"{prov_count} provider(s); vig {round((implied_sum - 1) * 100, 1)}% removed; "
                f"consensus hit prob {round(p * 100, 1)}%; cross-book edge {round(edge, 2)}%."
            ),
        })


def markets_for_comp(comp):
    rows = []
    odds_list = comp.get("odds") or []
    provs = []
    for o in odds_list:
        if not isinstance(o, dict):      # ARMOR: skip null/garbage odds entries
            continue
        h = o.get("homeTeamOdds") or {}
        a = o.get("awayTeamOdds") or {}
        d = o.get("drawOdds") or {}
        if not isinstance(h, dict):
            h = {}
        if not isinstance(a, dict):
            a = {}
        if not isinstance(d, dict):
            d = {}
        provs.append({
            "home_ml": am_to_dec(h.get("moneyLine")),
            "away_ml": am_to_dec(a.get("moneyLine")),
            "draw_ml": am_to_dec(d.get("moneyLine")),
            "spread": o.get("spread"),
            "home_sp": am_to_dec(h.get("spreadOdds")),
            "away_sp": am_to_dec(a.get("spreadOdds")),
            "total": o.get("overUnder"),
            "over": am_to_dec(o.get("overOdds")),
            "under": am_to_dec(o.get("underOdds")),
        })
    if not provs:
        return rows
    ml = {"home": [p["home_ml"] for p in provs], "away": [p["away_ml"] for p in provs]}
    if any(p["draw_ml"] for p in provs):
        ml["draw"] = [p["draw_ml"] for p in provs]
        add_rows(rows, "Match Winner (1X2)", ml, len(provs))
    else:
        add_rows(rows, "Moneyline", ml, len(provs))
    sp_prov = [p for p in provs if p["spread"] is not None and p["home_sp"] and p["away_sp"]]
    if sp_prov:
        line = sp_prov[0]["spread"]
        try:
            line_s = f"{float(line):+.1f}"
        except Exception:
            line_s = str(line)
        add_rows(rows, f"Spread / Handicap ({line_s})",
                 {f"Home {line_s}": [p["home_sp"] for p in sp_prov],
                  f"Away {line_s}": [p["away_sp"] for p in sp_prov]}, len(sp_prov))
    tot_prov = [p for p in provs if p["total"] and p["over"] and p["under"]]
    if tot_prov:
        line = tot_prov[0]["total"]
        add_rows(rows, f"Total Over/Under ({line})",
                 {f"Over {line}": [p["over"] for p in tot_prov],
                  f"Under {line}": [p["under"] for p in tot_prov]}, len(tot_prov))
    return rows


def parse_event(ev, sport, league):
    try:                                   # ARMOR: one corrupt match can never kill the scan
        if not isinstance(ev, dict):
            return None
        comps = ev.get("competitions") or []
        comp = comps[0] if comps else None
        if not isinstance(comp, dict):
            return None
        home = away = "?"
        for c in (comp.get("competitors") or []):
            if not isinstance(c, dict):
                continue
            name = ((c.get("team") or {}) or {}).get("displayName") or "?"
            if c.get("homeAway") == "home":
                home = name
            else:
                away = name
        rows = markets_for_comp(comp)
        if not rows:
            return None
        rows.sort(key=lambda r: r["confidence_score"], reverse=True)
        ts = int(time.time())
        return {
            "fixture_id": str(ev.get("id") or f"{home}-{away}"),
            "home": home,
            "away": away,
            "sport": sport,
            "league": league,
            "kickoff_utc": ev.get("date") or "",
            "markets_scanned": len(rows),
            "top_edges": rows[:12],
            "visual_reports_png": [],
            "agent_trace_lines": [
                {"agent": "SCOUT", "text": f"parsed {home} vs {away} ({league}): {len(rows)} market lines", "ts": ts},
                {"agent": "ACTUARY", "text": f"de-vigged {len(rows)} lines; ranked by consensus hit probability", "ts": ts},
            ],
        }
    except Exception as e:
        print(f"[SCOUT] skipped corrupt event: {type(e).__name__}: {e}")
        return None


def main():
    os.makedirs(DATA, exist_ok=True)
    lines = []
    fixtures = []
    sources = {}
    now = dt.datetime.now(dt.timezone.utc)
    dates = [(now + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    for sport_path, leagues in LEAGUES.items():
        for lg in leagues:
            n_ev = 0
            status = "no_fixture_in_board"
            for d in dates:
                url = (f"https://site.api.espn.com/apis/site/v2/sports/"
                       f"{sport_path}/{lg}/scoreboard?dates={d}")
                payload, err = http_get(url)
                if payload is None:
                    status = "blocked_by_waf" if err == "http_403" else (err or status)
                    continue
                events = payload.get("events") or []
                for ev in events:
                    fx = parse_event(ev, SPORT_KEY[sport_path], lg)
                    if fx:
                        fixtures.append(fx)
                        n_ev += 1
                if events:
                    status = "ok"
            sources[f"{sport_path}/{lg}"] = f"ok({n_ev})" if status == "ok" else status
            lines.append({"agent": "SCOUT", "text": f"{sport_path}/{lg}: {sources[f'{sport_path}/{lg}']}", "ts": int(time.time())})
            print(f"[SCOUT] {sport_path}/{lg} -> {sources[f'{sport_path}/{lg}']}")
    lines.append({"agent": "STRATEGIST", "text": f"universe assembled: {len(fixtures)} fixtures", "ts": int(time.time())})
    universe = {
        "generated_at_utc": now.isoformat(),
        "next_refresh_estimate_utc": (now + dt.timedelta(minutes=15)).isoformat(),
        "sources_status": sources,
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
    }
    with open(os.path.join(DATA, "market_universe.json"), "w") as f:
        json.dump(universe, f, indent=2)
    with open(os.path.join(DATA, "hunt_status.json"), "w") as f:
        json.dump({"status": "complete" if fixtures else "no_results",
                   "stage": "done", "progress": 1.0,
                   "lines": lines[-200:],
                   "result": {"fixture_count": len(fixtures)}}, f, indent=2)
    print(f"[STRATEGIST] Wrote data/market_universe.json with {len(fixtures)} fixtures.")


if __name__ == "__main__":
    main()
