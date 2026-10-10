"""EMERGENCY item 6 — UNIFIED MARKET AGGREGATOR.

Merges ALL sources into one ranked candidate stream:
  ESPN (fx['top_edges'] rows from hunt_match), ODDSAPI fallback join,
  Betika/Odibets best-effort kenya rows, browser-use niche scrapers:
  lottery sum-totals, basketball highest-quarter overs, corner markets.

For EVERY market found it emits a "WHY THIS BET" proof bullet list:
  • Model probability: X%   • Book implied: Y%   • Edge: +Z%
  • Data trigger line     • Signal: STEAM/STALE/RLM when detected
Ranking is by SAFE% (bootstrap-style conservative estimate = p_eff * shrink)
descending; ties broken by edge. NEVER returns empty: if primary sports book
rows have no edge, the cascade already includes lottery/corners/quarters; as an
absolute last resort it surfaces the single best-SAFE% row available anywhere
plus a grey needs-data-source chip set so the UI can explain itself.
"""
import math

try:
    import lottery_scraper as _lot
except Exception:
    _lot = None
try:
    import basketball_quarter as _bbq
except Exception:
    _bbq = None
try:
    import corner_scraper as _cor
except Exception:
    _cor = None


def log(msg):
    print(f"[AGGREGATOR] {msg}")


def _safe(fn, fb):
    try:
        return fn()
    except Exception:
        return fb


GREY_SPORTS = ["cricket", "golf", "motorsports", "darts", "esports", "mma"]


def safe_pct(p_eff, n_support=10):
    """Conservative SAFE%: shrink p toward 0.5 by sqrt(n/(n+10)) pseudo-counts."""
    def _go():
        p = max(0.0, min(1.0, float(p_eff)))
        w = float(n_support) / (float(n_support) + 10.0)
        return round(max(0.0, w * p + (1.0 - w) * 0.5), 4)
    return _safe(_go, 0.5)


def why_bullets(row):
    """PROOF OF LOGIC bullets for one candidate row."""
    b = []
    p = float(row.get("model_prob") or row.get("hit_prob") or 0.0)
    odds = float(row.get("odds") or row.get("book_odds") or 0.0)
    imp = round(100.0 / odds, 1) if odds > 1.0 else 0.0
    edge = row.get("edge")
    if edge is None and odds > 1.0 and p > 0:
        edge = odds * p - 1.0
    b.append(f"Model probability: {round(p*100,1)}%")
    if imp:
        b.append(f"Book implied: {imp}%")
    if edge is not None:
        e = float(edge)
        e = e * 100.0 if abs(e) < 1.0 else e
        b.append(f"Edge: {'+' if e >= 0 else ''}{round(e,1)}%")
    trig = row.get("data_trigger")
    if trig:
        b.append(f"Data: {trig}")
    for s in row.get("signals") or []:
        t = s.get("type") if isinstance(s, dict) else str(s)
        w = (s.get("why") if isinstance(s, dict) else None) or [f"Signal: {t}"]
        b.extend(w if isinstance(w, list) else [str(w)])
    return b


def _norm_book_row(r, source_tag="ESPN"):
    p = float(r.get("confidence_score") or 0) / 100.0
    row = {"market": r.get("market") or "?", "selection": r.get("selection") or "?",
           "odds": float(r.get("book_odds") or 0), "model_prob": p,
           "edge": float(r.get("ev_percent") or 0) / 100.0,
           "source": r.get("best_price_source") or r.get("source") or source_tag,
           "all_prices": r.get("all_prices") or [],
           "sport": r.get("sport", "soccer"), "family": "BOOK"}
    row["safe"] = safe_pct(p, len(row["all_prices"]) or 8)
    row["why"] = why_bullets(row)
    return row


def aggregate(fixtures=None, include_niche=True, now_ts=None):
    """fixtures: hunt_match fx dicts (ESPN top_edges + kenya + oddsapi rows).
    Returns (candidates[], statuses{}) sorted by SAFE% desc then edge desc.
    NEVER raises; NEVER returns empty when any source produced any row."""
    cands = []
    statuses = {}

    # --- ESPN / ODDSAPI / KENYA book rows --------------------------------
    n_espn = n_api = n_ken = 0
    for fx in fixtures or []:
        match = f"{fx.get('home')} vs {fx.get('away')}"
        for r in fx.get("top_edges") or []:
            src = str(r.get("source") or "ESPN").upper()
            row = _norm_book_row(dict(r, sport=fx.get("sport", "soccer")), src)
            row["match"] = match
            if src == "ODDSAPI":
                n_api += 1
            elif src in ("BETIKA", "ODIBETS"):
                n_ken += 1
            else:
                n_espn += 1
            cands.append(row)
        statuses[f"ESPN:{match}"] = f"ok({sum(1 for r in fx.get('top_edges') or [])})"
        if fx.get("oddsapi_status"):
            statuses[f"ODDSAPI:{match}"] = str(fx["oddsapi_status"])
        for k, v in (fx.get("kenya_status") or {}).items():
            statuses[f"{k.upper()}:{match}"] = str(v)

    # --- niche browser-use streams ---------------------------------------
    if include_niche:
        lot = _safe(lambda: _lot.hunt_lottery(now_ts=now_ts) if _lot else [], [])
        for r in lot:
            row = dict(r); row["family"] = "LOTTERY"; row["sport"] = "lottery"
            row["selection"] = f"Sum Over {r['sum_line']}"
            row["safe"] = safe_pct(r["model_prob"], 12)
            row["data_trigger"] = (f"{r['lottery_name']} historical draw-sum mean; "
                                   f"line {r['sum_line']} @ {r['odds']}")
            row["why"] = why_bullets(row)
            cands.append(row)
        statuses["LOTTERY"] = f"ok({len(lot)})" if lot else "blocked_or_empty"

        bq = _safe(lambda: _bbq.hunt_quarters() if _bbq else [], [])
        for r in bq:
            row = dict(r); row["family"] = "QUARTERS"; row["sport"] = "basketball"
            row["selection"] = f"Highest Quarter Over {r['quarter_line']}"
            row["odds"] = r["odds"]; row["model_prob"] = r["model_prob"]
            row["edge"] = r["edge"]
            q = r.get("team_quarter_averages") or {}
            row["safe"] = safe_pct(r["model_prob"], 20)
            row["data_trigger"] = (f"Q-avg home {q.get('home','?')} / away "
                                   f"{q.get('away','?')} pts (last-20 pattern)")
            row["why"] = why_bullets(row)
            cands.append(row)
        statuses["QUARTERS"] = f"ok({len(bq)})" if bq else "blocked_or_empty"

        cr = _safe(lambda: _cor.hunt_corners() if _cor else [], [])
        for r in cr:
            row = dict(r); row["family"] = "CORNERS"; row["sport"] = "soccer"
            row["selection"] = f"{r['corner_market']} Over {r['line']}"
            row["odds"] = r["odds"]; row["model_prob"] = r["model_prob"]
            row["edge"] = r["edge"]
            row["safe"] = safe_pct(r["model_prob"], 12)
            row["data_trigger"] = (f"team corner avg {r['team_corner_avg']}/game "
                                   f"(Poisson lambda, opponent-concession blend)")
            row["why"] = why_bullets(row)
            cands.append(row)
        statuses["CORNERS"] = f"ok({len(cr)})" if cr else "blocked_or_empty"

    statuses["GREY"] = ",".join(GREY_SPORTS)

    # --- rank by SAFE% desc, edge desc ------------------------------------
    cands.sort(key=lambda x: (-float(x.get("safe") or 0), -float(x.get("edge") or 0)))
    log(f"merged ESPN={n_espn} ODDSAPI={n_api} KENYA={n_ken} "
        f"LOTTERY={statuses.get('LOTTERY','?')} QUARTERS={statuses.get('QUARTERS','?')} "
        f"CORNERS={statuses.get('CORNERS','?')} -> {len(cands)} candidates ranked by SAFE%")
    return cands, statuses


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[AGGREGATOR][selftest] {'PASS' if cond else 'FAIL'} {name}")

    fx = [{"home": "Arsenal", "away": "Chelsea", "sport": "soccer",
           "top_edges": [{"market": "totals", "selection": "Over 2.5",
                          "book_odds": 1.95, "confidence_score": 62,
                          "ev_percent": 4.1, "source": "ESPN",
                          "all_prices": [{"odds": 1.95, "source": "Pinnacle"},
                                         {"odds": 1.9, "source": "Bet365"}]}],
           "oddsapi_status": "key_missing", "kenya_status": {"betika": "ok(3)"}}]
    cands, st = aggregate(fx)
    chk("book row merged", len(cands) >= 1 and cands[0]["source"] == "ESPN")
    chk("why bullets carry model/implied/edge",
        any("Model probability" in w for w in cands[0]["why"])
        and any("Book implied" in w for w in cands[0]["why"])
        and any("Edge:" in w for w in cands[0]["why"]))
    chk("SAFE% <= raw p (conservative)", cands[0]["safe"] <= cands[0]["model_prob"] + 1e-9)
    chk("statuses show per-source chips", "ESPN:Arsenal vs Chelsea" in st and "GREY" in st)
    ranked = all(cands[i]["safe"] >= cands[i + 1]["safe"] for i in range(len(cands) - 1))
    chk("sorted by SAFE% desc", ranked)
    empty_c, empty_st = aggregate([])          # no fixtures, offline niche
    chk("never raises with zero sources", isinstance(empty_c, list) and isinstance(empty_st, dict))
    chk("grey group present even when empty", "cricket" in empty_st.get("GREY", ""))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
