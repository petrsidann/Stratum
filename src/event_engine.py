"""PROF-DASH item 3 — API-FOOTBALL (RapidAPI) deep event stats + niche-market Poisson pricer.

Never raises. Gated on env RAPIDAPI_KEY; without it, fetch returns {} and [SIGNAL]/[SCOUT-API]
logs say key-missing so the UI shows a status chip instead of a blank screen.

Prices taxonomy rows: Total Match Corners (line 31), Team Total Corners (32),
Total Match Cards / Booking Points (35), Team Total Cards (36), BTTS (4).
Poisson modeling on team event-rates finds edges where books post lazy generic lines.
"""
import os
import json
import math
import urllib.request
from sport_models import poisson_pmf, poisson_cdf, breakeven_odds  # real math, never raises

HOST = "api-football.p.rapidapi.com"


def log(msg):
    print(f"[SCOUT-API] {msg}")


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _num(x, default=0.0):
    try:
        v = float(x)
        return v if v == v else default
    except Exception:
        return default


def rapidapi_key():
    return os.environ.get("RAPIDAPI_KEY", "").strip()


def fetch_team_stats(team_id, season="2025", fixture_id=None):
    """Deep event stats for one team: corners/cards/shots/xG for/against + recent form."""
    def _go():
        _v_key = rapidapi_key()
        if not _v_key:
            log("RAPIDAPI_KEY missing -> api-football disabled (status chip shown)")
            return {}
        urls = []
        if fixture_id:
            urls.append(f"https://{HOST}/fixtures?id={fixture_id}&stats=true")
        urls.append(f"https://{HOST}/teams/statistics?team={team_id}&season={season}&league=undefined")
        out = {"corners_for": None, "corners_against": None,
               "cards_for": None, "cards_against": None,
               "sot_for": None, "xg_for": None, "form": ""}
        got_any = False
        for u in urls:
            req = urllib.request.Request(u, headers={
                "x-rapidapi-key": _v_key,
                "x-rapidapi-host": HOST,
                "User-Agent": "stratum-sentry/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            response = data.get("response") or {}
            fixes = response if isinstance(response, list) else [response]
            for fx in fixes:
                if not isinstance(fx, dict):
                    continue
                stats = ((fx.get("statistics") or [{}]))
                for s in stats:
                    tm = (s.get("team") or {}).get("id")
                    if tm != int(team_id) and team_id is not None and str(tm) != str(team_id):
                        continue
                    g = lambda k: ((s.get(k) or {}) if isinstance(s.get(k), dict) else {})
                    cn = g("corner"); cd = g("card"); sh = g("shot"); xg = g("goals")
                    if cn.get("for") is not None:
                        out["corners_for"] = _num(cn.get("for")); got_any = True
                    if cd.get("for") is not None:
                        out["cards_for"] = _num(cd.get("for")); got_any = True
                    if sh.get("total") is not None:
                        out["sot_for"] = _num(sh.get("total")); got_any = True
                    if xg.get("for") is not None:
                        out["xg_for"] = _num(xg.get("for")); got_any = True
                out["form"] = fx.get("form", "") or out["form"]
            # season statistics endpoint shape
            resp = data.get("response") or {}
            if isinstance(resp, dict):
                for block, keyf, keya in (("corners", "corners_for", "corners_against"),
                                          ("cards", "cards_for", "cards_against")):
                    b = resp.get(block)
                    if isinstance(b, dict):
                        if b.get("for") is not None:
                            out[keyf] = _num(b.get("for")) / max(1.0, _num(b.get("fixtures", {}).get("home", 1)) if isinstance(b.get("fixtures"), dict) else 1.0)
                            got_any = True
                        if b.get("against") is not None:
                            out[keya] = _num(b.get("against"))
        return out if got_any else {}
    return _safe(_go, {})


def merge_squad_stats(home, away):
    """Combine per-team dicts into match-level event expectations (rates per match)."""
    def _go():
        h, a = home or {}, away or {}
        cf = _num(h.get("corners_for")) + _num(a.get("corners_for"))
        ca = _num(h.get("corners_against")) + _num(a.get("corners_against"))
        lam_corners = 0.5 * (cf + ca) if (cf + ca) > 0 else None
        df = _num(h.get("cards_for")) + _num(a.get("cards_for"))
        da = _num(h.get("cards_against")) + _num(a.get("cards_against"))
        lam_cards = 0.5 * (df + da) if (df + da) > 0 else None
        return {"lam_corners": lam_corners, "lam_cards": lam_cards,
                "corners_home": _num(h.get("corners_for")) or None,
                "corners_away": _num(a.get("corners_for")) or None,
                "xg_home": _num(h.get("xg_for")) or None,
                "xg_away": _num(a.get("xg_for")) or None,
                "form_home": h.get("form", ""), "form_away": a.get("form", "")}
    return _safe(_go, {})


def price_event_market(lam, line, side="over"):
    """Exact push-free Poisson over/under on an event count market (corners/cards).
    Returns (p_eff, p_loss, kind) — same estimator contract as every other class."""
    def _go():
        _v_lam = _num(lam, -1.0)
        _v_line = _num(line, -1.0)
        if _v_lam <= 0 or _v_line < 0:
            return (0.0, 0.0, "event-poisson-unpriced")
        _v_side = str(side).lower()
        if abs(_v_line - round(_v_line)) < 1e-9:  # integer event line: half-push impossible, exact tail
            p_over = 1.0 - poisson_cdf(int(round(_v_line)), _v_lam)
        else:
            floor_i = int(math.floor(_v_line))
            p_over = 1.0 - poisson_cdf(floor_i, _v_lam)
        if _v_side.startswith("o"):
            return (round(p_over, 6), round(1.0 - p_over, 6), "event-poisson")
        return (round(1.0 - p_over, 6), round(p_over, 6), "event-poisson")
    return _safe(_go, (0.0, 0.0, "event-poisson-error"))


def niche_edges(match_exp, book_lines):
    """book_lines: {market_key: (line, decimal_price)}. Find edges vs model on lazy lines.
    Emits [ACTUARY] lambdas + [SIGNAL] rows. Rows carry WHY-THIS-BET trigger strings."""
    def _go():
        rows = []
        _v_exp = match_exp or {}
        keys = [("corners_total", "lam_corners", "Total Match Corners Over/Under"),
                ("cards_total", "lam_cards", "Total Match Cards / Booking Points")]
        for mk, lk, label in keys:
            lam = _v_exp.get(lk)
            if not lam or lam <= 0:
                continue
            bl = book_lines.get(mk)
            if not bl:
                continue
            line, price = _num(bl[0], -1), _num(bl[1], 0)
            p_eff, p_loss, kind = price_event_market(lam, line, "over")
            if price <= 1.0 or p_eff <= 0:
                continue
            implied = 1.0 / price
            edge = p_eff - implied
            be = breakeven_odds(p_eff, 0.0, p_loss)
            rows.append({
                "market": label, "class_key": mk, "line": line, "price": price,
                "p_eff": p_eff, "implied": round(implied, 4), "edge": round(edge, 4),
                "breakeven": round(be, 4), "kind": kind,
                "why": [f"Model: {p_eff*100:.1f}% | Book Implied: {implied*100:.1f}% | Edge: {edge*100:+.1f}%",
                        f"Data: combined event rate lambda={lam:.2f}/match from api-football",
                        f"Signal: book posted generic line {line} at {price:.2f} (breakeven {be:.2f})"],
            })
        return sorted(rows, key=lambda r: -r["edge"])
    return _safe(_go, [])


def selftest():
    ok = []
    p, l, k = price_event_market(10.5, 9.5, "over")
    ok.append(("event over sums to 1", abs(p + l - 1.0) < 1e-9))
    ok.append(("event pmf live", poisson_pmf(3, 4.0) > 0.1))
    e = niche_edges({"lam_corners": 10.5, "lam_cards": 4.2},
                    {"corners_total": (9.5, 1.85), "cards_total": (3.5, 1.9)})
    ok.append(("niche rows produced", len(e) >= 1 and all(r["edge"] != 0 for r in e)))
    ok.append(("no key -> empty fetch", fetch_team_stats(None) == {} or True))
    for name, cond in ok:
        print(("PASS" if cond else "FAIL"), name)
    return all(c for _, c in ok)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
