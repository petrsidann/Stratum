"""Stratum Obsidian Brain - analysis layer.

Consumes raw scraped odds rows and produces fully-analyzed market documents:
no-vig fair odds, EV, fractional Kelly stake, confidence score, and
cross-bookmaker signal detection (stale-line arbitrage, line movement, value).

All prices are DECIMAL odds. Raw row schema (from any source):
    {
      "source":    str,   # bookmaker name, e.g. "Betika"
      "market":    str,   # canonical market type, e.g. "moneyline"
      "name":      str,   # display name, e.g. "Match Winner"
      "selection": str,   # e.g. "Home", "Over 2.5", player name
      "line":      float | None,
      "odds":      float, # decimal odds
    }

Integrity rule inherited from the hunter: nothing here ever invents a price.
A market with no rows yields no output.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Core quant math (self-contained; mirrors scripts/engine.py semantics but
# operates on decimal odds directly instead of American prices)
# ---------------------------------------------------------------------------

SHARP_SOURCES = {"pinnacle", "circa"}
OVERROUND_MAX = 1.15           # discard handles whose implied sum exceeds this
OVERROUND_MIN = 0.95           # below this the scrape is almost certainly partial
VALUE_EDGE_THRESHOLD = 3.0     # percent EV that qualifies as a flagged edge
STALE_SPREAD_THRESHOLD = 0.10  # decimal-odds gap between books on same leg


def expected_value(fair_prob: float, decimal_odds: float) -> float:
    """EV per 1 unit staked: p*(d-1) - (1-p)."""
    if not decimal_odds or decimal_odds <= 1.0:
        return 0.0
    return fair_prob * (decimal_odds - 1.0) - (1.0 - fair_prob)


def kelly_stake(ev: float, decimal_odds: float, fraction: float = 0.25) -> float:
    """Fractional Kelly stake as share of bankroll (default quarter-Kelly)."""
    if not decimal_odds or decimal_odds <= 1.0 or ev <= 0:
        return 0.0
    b = decimal_odds - 1.0
    p = (ev + 1.0) / (b + 1.0)
    q = 1.0 - p
    full = (b * p - q) / b if b > 0 else 0.0
    return max(0.0, round(full * fraction, 4))


def confidence_score(edge_pct: float, num_books: int, has_movement: bool,
                     liquidity_rank: int) -> float:
    """0-100 heuristic blend: edge size, book count, fresh movement, liquidity."""
    if edge_pct <= 0:
        return 0.0
    s_edge = min(edge_pct / 8.0, 1.0) * 45.0
    s_books = min(num_books / 5.0, 1.0) * 20.0
    s_move = 15.0 if has_movement else 0.0
    s_liq = max(0.0, (5 - max(liquidity_rank, 1)) / 4.0) * 20.0
    return round(min(s_edge + s_books + s_move + s_liq, 100.0), 1)


# ---------------------------------------------------------------------------
# Odds math (decimal-native)
# ---------------------------------------------------------------------------

def decimal_to_implied(dec: Optional[float]) -> Optional[float]:
    if dec is None:
        return None
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return None
    if d <= 1.0 or math.isnan(d) or math.isinf(d):
        return None
    return 1.0 / d


def calculate_fair_value(implied_probs: List[float]) -> Optional[List[float]]:
    """Multiplicative vig removal. Returns per-leg fair probabilities."""
    probs = [p for p in implied_probs if p and p > 0]
    if len(probs) < 2:
        return None
    total = sum(probs)
    if total <= OVERROUND_MIN or total >= OVERROUND_MAX:
        return None
    return [p / total for p in probs]


def validate_handle(implied_probs: List[float]) -> bool:
    """sum(probabilities) must sit inside sane bounds or the scrape is junk."""
    probs = [p for p in implied_probs if p and p > 0]
    if len(probs) < 2:
        return False
    return OVERROUND_MIN <= sum(probs) < OVERROUND_MAX


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_signals(selection: str, legs: Dict[str, Dict[str, Any]],
                   history: List[List[Any]]) -> List[str]:
    """Cross-source signals on one market handle.

    STALE_LINE_ARB : two books quote the identical leg with a decimal gap
                     >= STALE_SPREAD_THRESHOLD (e.g. AH -1.5 @ 1.80 vs 1.65).
    LINE_MOVE      : best price drifted meaningfully within the snapshot set.
    VALUE          : best available price beats fair probability by >3% EV.
    """
    signals: List[str] = []
    leg = legs.get(selection) or {}
    books: Dict[str, float] = leg.get("books", {})

    if len(books) >= 2:
        hi = max(books.values())
        lo = min(books.values())
        if hi - lo >= STALE_SPREAD_THRESHOLD:
            signals.append("STALE_LINE_ARB")
        sharp_vals = [v for k, v in books.items()
                      if any(s in k.lower().replace(" ", "") for s in SHARP_SOURCES)]
        retail_vals = [v for k, v in books.items()
                       if not any(s in k.lower().replace(" ", "") for s in SHARP_SOURCES)]
        if sharp_vals and retail_vals:
            if (1.0 / max(retail_vals)) - (1.0 / max(sharp_vals)) > 0.04:
                signals.append("STALE_LINE")

    if len(history) >= 2:
        try:
            if abs(float(history[-1][1]) - float(history[0][1])) >= 0.05:
                signals.append("LINE_MOVE")
        except (TypeError, ValueError):
            pass

    fair_p = leg.get("fair_probability")
    if fair_p:
        best_dec = max(books.values(), default=0.0)
        if best_dec and expected_value(fair_p, best_dec) * 100.0 > VALUE_EDGE_THRESHOLD:
            signals.append("VALUE")

    return signals


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_rows(rows: List[Dict[str, Any]], state: Dict[str, Any],
                 event_id: str) -> List[Dict[str, Any]]:
    """Group raw decimal-odds rows into analyzed market documents.

    `state` carries the persistent odds history used for movement tracking
    (keyed "<event>:<market>|<line>:<selection>").
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            dec = float(r["odds"])
        except (KeyError, TypeError, ValueError):
            continue
        if dec <= 1.0:
            continue
        mtype = r.get("market") or "other"
        line = r.get("line")
        gkey = f"{mtype}|{line if line is not None else ''}"
        grp = grouped.setdefault(gkey, {
            "type": mtype,
            "display": r.get("name") or mtype.replace("_", " ").title(),
            "point": line,
            "legs": {},
        })
        sel = (r.get("selection") or "?").strip()
        leg = grp["legs"].setdefault(sel, {"implied": [], "books": {}})
        imp = decimal_to_implied(dec)
        if imp is None:
            continue
        leg["implied"].append(imp)
        bk = r.get("source") or "Unknown"
        prev = leg["books"].get(bk)
        if prev is None or dec > prev:
            leg["books"][bk] = round(dec, 3)

    markets: List[Dict[str, Any]] = []
    for gkey, grp in grouped.items():
        selections = list(grp["legs"].keys())
        if not selections:
            continue
        implied_sets = [sum(l["implied"]) / len(l["implied"])
                        for l in grp["legs"].values() if l["implied"]]
        if len(implied_sets) >= 2 and not validate_handle(implied_sets):
            # Bad scrape (overround outside sane bounds): discard silently.
            continue
        fair_probs = calculate_fair_value(implied_sets)

        # Attach fair prob to each leg so detect_signals can see it.
        for idx, sel in enumerate(selections):
            leg = grp["legs"][sel]
            leg["fair_probability"] = (
                fair_probs[idx] if fair_probs and idx < len(fair_probs) else None)

        for idx, sel in enumerate(selections):
            leg = grp["legs"][sel]
            if not leg["implied"] or not leg["books"]:
                continue
            raw_p = sum(leg["implied"]) / len(leg["implied"])
            fair_p = fair_probs[idx] if fair_probs and idx < len(fair_probs) else raw_p
            best_bk, best_dec = max(leg["books"].items(), key=lambda kv: kv[1])
            ev = expected_value(fair_p, best_dec)
            ev_pct = round(ev * 100.0, 2)
            stake = kelly_stake(ev, best_dec)

            mkey = f"{event_id}:{gkey}:{sel}"
            hist = state.setdefault("odds_history", {})
            points = hist.setdefault(mkey, [])
            now = utc_now_iso()
            if not points or points[-1][1] != best_dec:
                points.append([now, best_dec])
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=26)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")
            points = [p for p in points if str(p[0]) >= cutoff]
            hist[mkey] = points[-48:]
            recent_window = (datetime.now(timezone.utc) - timedelta(hours=2)
                             ).strftime("%Y-%m-%dT%H:%M:%SZ")
            recent = [p for p in points if str(p[0]) >= recent_window]

            market = {
                "key": mkey,
                "type": grp["type"],
                "name": grp["display"],
                "line": grp["point"],
                "selection": sel,
                "raw_probability": round(raw_p * 100, 2),
                "fair_probability": round(fair_p * 100, 2),
                "vig_removed_percent": round((fair_p - raw_p) * 100, 2),
                "best": {"bookmaker": best_bk, "price": round(best_dec, 3),
                         "decimal": round(best_dec, 3)},
                "bookmakers": sorted(
                    [{"bookmaker": bk, "price": d, "decimal": d,
                      "implied_probability": round((decimal_to_implied(d) or 0) * 100, 2)}
                     for bk, d in leg["books"].items()],
                    key=lambda b: -b["decimal"]),
                "num_bookmakers": len(leg["books"]),
                "ev_percent": ev_pct,
                "kelly_stake": stake,
                "confidence": confidence_score(max(ev_pct, 0), len(leg["books"]),
                                               len(recent) >= 2, 2),
                "movement": [list(p) for p in points[-48:]],
                "has_data": True,
            }
            market["signals"] = detect_signals(sel, grp["legs"], points[-48:])
            markets.append(market)

    markets.sort(key=lambda m: -m["ev_percent"])
    return markets
