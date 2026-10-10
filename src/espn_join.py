"""EMERGENCY item 5 — the-odds-api FALLBACK JOIN for ESPN fixtures with zero lines.

Root cause of "no line yet": ESPN's scoreboard sometimes returns the fixture with
ZERO odds rows attached. hunt_match already fuzzy-joins by normalized token
overlap (threshold raised to 0.7 per spec); this module closes the other half:
when a matched fixture still has no book rows, fetch the-odds-api events for the
sport and fuzzy-match home/away names against the ESPN display names, merging
every book price into all_prices[] under source tag 'ODDSAPI'.

Never raises; ODDS_API_KEY missing / network down -> ([], status) so the UI shows
a per-source status chip instead of silence.
"""
import re

from odds_api import fetch_odds, odds_api_key, log as _sig_log

_STRIP = {"fc", "cf", "sc", "afc", "cfc", "club", "clube", "de", "the", "team",
          "national", "men", "w", "u21", "u20", "u19", "usa", "republic", "states",
          "bk", "if", "sk", "ac", "as", "ss"}


def norm(s):
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    toks = [t for t in s.split() if t and t not in _STRIP]
    return toks or s.split()


def tok_overlap(query_tok, name_toks):
    """fraction of query tokens found (exact/substring) in the name tokens."""
    if not query_tok or not name_toks:
        return 0.0
    hits = sum(1 for t in query_tok if any(t == nt or t in nt or nt in t
                                           for nt in name_toks))
    return hits / len(query_tok)


def fuzzy_team_match(a, b, threshold=0.7):
    """True when >=threshold of the SHORTER side's tokens appear in the longer.
    Handles 'Denmark' vs 'Denmark FC', 'Inter Miami' vs 'Miami Inter CF'."""
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return tok_overlap(short, long_) >= threshold


def fixture_fuzzy_match(home_e, away_e, home_o, away_o, threshold=0.7):
    return (fuzzy_team_match(home_e, home_o, threshold)
            and fuzzy_team_match(away_e, away_o, threshold))


def join_oddsapi_for_fixture(home, away, sport="soccer_epl", threshold=0.7):
    """Fetch the-odds-api events for `sport`, fuzzy-match against an ESPN fixture.
    Returns (rows, status) where rows are hunt-shape edge rows ready to append to
    fx['top_edges'] (multi-book consensus via same math as ESPN merge path)."""
    def _go():
        if not odds_api_key():
            return [], "key_missing"
        events = fetch_odds(sport=sport)
        joined = []
        for ev in events:
            if fixture_fuzzy_match(home, away, ev.get("home") or "",
                                   ev.get("away") or "", threshold):
                joined.append(ev)
        if not joined:
            return [], f"no_fixture_in_board({len(events)} polled)"
        rows = []
        for ev in joined:
            rows.extend(_edges_from_event(ev))
        n_px = sum(len(r.get("all_prices") or []) for r in rows)
        status = f"ok({len(rows)} markets, {n_px} prices)"
        _sig_log(f"ODDSAPI fallback joined '{home} vs {away}': {status}")
        return rows, status

    try:
        return _go()
    except Exception as e:  # never raise
        return [], f"crash({type(e).__name__})"


def _edges_from_event(ev):
    """Convert an ODDSAPI event (all_prices records) into book-edge rows:
    per (market,name[,point]) keep ALL prices, best drives edge, tag
    best_price_source + source='ODDSAPI'."""
    groups = {}
    for p in ev.get("all_prices") or []:
        k = (str(p.get("market")), str(p.get("name")),
             p.get("point") if p.get("point") is not None else "")
        groups.setdefault(k, []).append(p)
    rows = []
    for (mk, nm, pt), ps in groups.items():
        if len(ps) < 1:
            continue
        vals = [(float(x["price"]), str(x["book"])) for x in ps]
        best_v, best_b = max(vals, key=lambda t: t[0])
        avg = sum(v for v, _ in vals) / len(vals)
        imp_sum = 0.0
        # de-vig across the sibling selections of the same market+point
        sibs = [q for q in groups if q[0] == mk and q[1] != "" and q[2] == pt]
        for q in sibs:
            qv = [float(x["price"]) for x in groups[q]]
            imp_sum += 1.0 / (sum(qv) / len(qv))
        p = (1.0 / avg) / imp_sum if imp_sum > 0 else 1.0 / avg
        p = max(0.01, min(0.99, p))
        fair = 1.0 / p
        edge = (best_v / fair - 1.0) * 100.0
        sel = nm if pt == "" else f"{nm} {pt}"
        rows.append({"market": mk, "selection": sel, "book_odds": round(best_v, 3),
                     "fair_odds": round(fair, 3), "ev_percent": round(edge, 2),
                     "confidence_score": int(max(1, min(99, round(p * 100)))),
                     "kelly_stake_pct": 0.0, "source": "ODDSAPI",
                     "all_prices": [{"odds": v, "source": b} for v, b in vals],
                     "best_price_source": best_b,
                     "reasoning_summary": (
                         f"the-odds-api fallback: {len(vals)} book(s); vig removed; "
                         f"consensus hit prob {round(p*100,1)}%; edge {round(edge,2)}% "
                         f"(best {best_v:.2f} @ {best_b}).")})
    return rows


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[SCOUT][espn_join selftest] {'PASS' if cond else 'FAIL'} {name}")

    chk("Denmark ~ Denmark FC", fuzzy_team_match("Denmark", "Denmark FC"))
    chk("Portugal ~ Portugal SC", fuzzy_team_match("Portugal", "Portugal SC"))
    chk("Man Utd !~ Man City", not fuzzy_team_match("Manchester United", "Manchester City"))
    chk("Inter Miami ~ Miami Inter CF",
        fixture_fuzzy_match("Inter Miami", "Orlando City", "Miami Inter CF", "Orlando City SC"))
    chk("threshold 0.7 respected on partial junk",
        not fuzzy_team_match("Real Sociedad B", "Real Madrid"))
    # no key -> graceful status, never raises
    rows, st = join_oddsapi_for_fixture("Arsenal", "Chelsea", "soccer_epl")
    chk("key-missing/network-down returns tuple rows+status", isinstance(rows, list) and isinstance(st, str))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
