"""Stratum Market Scanner (Phase 3) — the Eyes.

Ingests odds for the FULL 200-market board: moneylines, spreads, totals,
halves, quarters, player props and alternate lines across multiple books.

Honesty contract (framework rule #2):
  * A live fetch is attempted first via ``src.scraper`` (free, polite HTML).
    If it yields nothing (offline / blocked / no keys), we degrade to an
    EXPLICITLY LABELED deterministic sample board so the UI is still usable.
  * We NEVER present invented prices as real. Every row carries a
    ``data_source`` field ("live" or "sample") and sample boards surface a
    loud warning in the UI. No fake player names are ever generated either —
    prop rows only exist if the source text actually contained them.

All derived math (fair probs, EV%, arb%) happens in src/quant_engine.py —
this module only fetches, normalizes and groups raw quotes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("stratum.scanner")

# --- Market taxonomy (the 200-market board) --------------------------------
MARKET_ML = "ML"
MARKET_SPREAD = "Spread"
MARKET_TOTAL = "Total"
MARKET_HALVES = "Halves"
MARKET_QUARTERS = "Quarters"
MARKET_PROPS = "PlayerProps"
MARKET_ALTERNATES = "Alternates"

ALL_MARKET_TYPES = [
    MARKET_ML, MARKET_SPREAD, MARKET_TOTAL,
    MARKET_HALVES, MARKET_QUARTERS, MARKET_PROPS, MARKET_ALTERNATES,
]

MAJOR_BOOKS = ["Pinnacle", "Circa", "BetMGM", "DraftKings", "FanDuel", "Caesars"]

# Discrepancy thresholds for STALE-LINE flags (framework: soft books lag sharp ones)
SPREAD_DISCREPANCY_THRESHOLD = 1.5   # points
PRICE_DIFF_PCT_THRESHOLD = 5.0       # % gap on the American price of a selection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MarketScanner:
    """Fetches and normalizes multi-market, multi-book odds snapshots."""

    def __init__(self, fetcher=None):
        """``fetcher(query) -> str`` is injectable for tests; defaults to scraper."""
        self._fetcher = fetcher

    # -- public API ----------------------------------------------------------

    def scan_match(self, match_id: str, sport: str = "NFL") -> List[Dict]:
        """Return ALL available markets for a match as a list of quote dicts.

        Row schema: {market_type, selection, bookmaker, american_odds,
        line, timestamp, data_source}. Empty list is a valid answer when a
        market (e.g. props) cannot be fetched — we never invent rows.
        """
        rows: List[Dict] = []
        try:
            text = self._fetch_raw(match_id, sport)
        except Exception as exc:  # network/scrape failure -> graceful empty
            logger.warning("Live scan failed for %s: %s", match_id, exc)
            text = ""

        if text:
            rows = self._parse_live(text, match_id)
            if rows:
                return rows
            logger.warning("Live scan returned text but zero parseable quotes for %s", match_id)

        # MVP fallback: deterministic SAMPLE board, loudly labeled.
        return self.sample_board(match_id)

    def compare_books(self, scan_results: List[Dict]) -> Dict:
        """Group quotes by (market_type, selection) and flag stale-line spots.

        Returns {"groups": [...], "stale_flags": [...], "arb_flags": [...]}.
        A STALE flag fires when one book's spread differs from the consensus
        by > SPREAD_DISCREPANCY_THRESHOLD, or its price deviates from the
        consensus by > PRICE_DIFF_PCT_THRESHOLD. Arb detection between best
        home/away ML prices uses quant_engine.arbitrage_pct exclusively.
        """
        from src.quant_engine import arbitrage_pct  # local import keeps module light

        groups: Dict[tuple, List[Dict]] = {}
        for row in scan_results or []:
            key = (row.get("market_type"), row.get("selection"))
            groups.setdefault(key, []).append(row)

        stale_flags: List[Dict] = []
        out_groups: List[Dict] = []
        for (mtype, selection), quotes in sorted(groups.items(), key=lambda kv: str(kv[0])):
            entry = {
                "market_type": mtype,
                "selection": selection,
                "books": [q["bookmaker"] for q in quotes],
                "best_american": None,
                "consensus_line": None,
                "spread_discrepancy": None,
                "price_diff_pct": None,
            }
            priced = [q for q in quotes if q.get("american_odds") is not None]
            lined = [q for q in quotes if q.get("line") is not None]
            if priced:
                # Best = most favorable decimal price; ranking delegated to quant math.
                from src.quant_engine import american_to_decimal
                best = max(priced, key=lambda q: american_to_decimal(q["american_odds"]))
                entry["best_american"] = best["american_odds"]
                entry["best_book"] = best["bookmaker"]
                # Consensus = median price across books (raw grouping stat only).
                svals = sorted(q["american_odds"] for q in priced)
                consensus = svals[len(svals) // 2]
                if consensus:
                    diff_pct = abs(best["american_odds"] - consensus) / abs(consensus) * 100.0
                    entry["price_diff_pct"] = round(diff_pct, 2)
                    if len(priced) >= 2 and diff_pct > PRICE_DIFF_PCT_THRESHOLD:
                        stale_flags.append({
                            "market_type": mtype, "selection": selection,
                            "bookmaker": best["bookmaker"],
                            "reason": f"price {best['american_odds']:+.0f} deviates "
                                      f"{diff_pct:.1f}% from consensus {consensus:+.0f}",
                            "kind": "price",
                        })
            if lined:
                lvals = sorted(q["line"] for q in lined)
                lo, hi = lvals[0], lvals[-1]
                entry["consensus_line"] = lvals[len(lvals) // 2]
                entry["spread_discrepancy"] = round(hi - lo, 2)
                if len(lined) >= 2 and (hi - lo) > SPREAD_DISCREPANCY_THRESHOLD:
                    widest = max(lined, key=lambda q: abs(q["line"] - entry["consensus_line"]))
                    stale_flags.append({
                        "market_type": mtype, "selection": selection,
                        "bookmaker": widest["bookmaker"],
                        "reason": f"line {widest['line']} off consensus "
                                  f"{entry['consensus_line']} by {hi - lo:.1f} pts",
                        "kind": "line",
                    })
            out_groups.append(entry)

        # Arbitrage check across the two best opposing ML sides.
        arb_flags: List[Dict] = []
        ml_groups = [g for g in out_groups if g["market_type"] == MARKET_ML and g["best_american"]]
        if len(ml_groups) >= 2:
            best_a = ml_groups[0]["best_american"]
            best_b = ml_groups[1]["best_american"]
            margin = arbitrage_pct(best_a, best_b)
            if margin > 0:
                arb_flags.append({
                    "side_a": ml_groups[0]["selection"], "odds_a": best_a,
                    "side_b": ml_groups[1]["selection"], "odds_b": best_b,
                    "arb_pct": round(margin, 3),
                })

        return {"groups": out_groups, "stale_flags": stale_flags, "arb_flags": arb_flags}

    # -- live fetch + parse ----------------------------------------------------

    def _fetch_raw(self, match_id: str, sport: str) -> str:
        if self._fetcher is not None:
            return self._fetcher(f"{match_id} {sport}")
        try:
            from src import scraper
            return scraper.fetch_match_context(f"{match_id} {sport} all markets props alternates")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Scraper unavailable: %s", exc)
            return ""

    _ROW_RE = re.compile(
        r"(?P<mtype>ML|moneyline|spread|h[-a]?line|total|o/u|quarters?(?: \d+)?|"
        r"halves(?: \w+)?|half(?:\s+\w+)*|props?|player props?|alt(?:ernate)?(?: lines?)?)\s*[:|–-]\s*"
        r"(?P<sel>[^|;]{1,60}?)\s*[:|@]\s*"
        r"(?P<book>[A-Za-z][A-Za-z .]{1,20}?)\s*[:|@]?\s*(?P<odds>[+-]\d{2,4})"
        r"(?:\s*(?:pts?|lines?)[:|]?\s*(?P<line>[+-]?\d+(?:\.\d+)?))?",
        re.IGNORECASE,
    )

    def _parse_live(self, text: str, match_id: str) -> List[Dict]:
        """Parse strict-format quote lines from a trusted feed text.

        Expected line format (per source doc): ``Market : Selection @ Book : +130``.
        Anything that doesn't match is skipped — partial results beat fabrications.
        """
        ts = _now_iso()
        rows: List[Dict] = []
        for line in text.splitlines():
            m = self._ROW_RE.search(line)
            if not m:
                continue
            raw_type = m.group("mtype").lower()
            if raw_type.startswith("quarter"):
                mtype = MARKET_QUARTERS
            elif raw_type.startswith("half"):
                mtype = MARKET_HALVES
            elif raw_type.startswith("alt"):
                mtype = MARKET_ALTERNATES
            elif "prop" in raw_type:
                mtype = MARKET_PROPS
            else:
                mtype = {
                    "ml": MARKET_ML, "moneyline": MARKET_ML, "spread": MARKET_SPREAD,
                    "h": MARKET_SPREAD, "ha": MARKET_SPREAD, "hl": MARKET_SPREAD,
                    "h-line": MARKET_SPREAD, "total": MARKET_TOTAL, "o/u": MARKET_TOTAL,
                    "prop": MARKET_PROPS,
                }.get(raw_type, raw_type.title())
            line_val = m.group("line")
            rows.append({
                "market_type": mtype,
                "selection": m.group("sel").strip(),
                "bookmaker": m.group("book").strip(),
                "american_odds": int(m.group("odds")),
                "line": float(line_val) if line_val else None,
                "timestamp": ts,
                "data_source": "live",
                "match_id": match_id,
            })
        return rows

    # -- deterministic SAMPLE board (clearly labeled, never sold as live) ------

    def sample_board(self, match_id: str) -> List[Dict]:
        """Build a fixed, obviously-synthetic multi-market board for the MVP demo.

        Rows carry ``data_source='sample'`` so every consumer can gray them
        out / warn about them. Prices are round numbers a demo user would
        recognize; NO real player names are used (props reference generic
        'Home QB'/'Away RB' role labels).
        """
        ts = _now_iso()
        mk = lambda **kw: {**kw, "timestamp": ts, "data_source": "sample", "match_id": match_id}
        rows = [
            # Moneylines across books (one soft book lags => stale candidate)
            mk(market_type=MARKET_ML, selection="Home", bookmaker="Pinnacle", american_odds=-150, line=None),
            mk(market_type=MARKET_ML, selection="Away", bookmaker="Pinnacle", american_odds=+130, line=None),
            mk(market_type=MARKET_ML, selection="Home", bookmaker="Circa", american_odds=-148, line=None),
            mk(market_type=MARKET_ML, selection="Away", bookmaker="Circa", american_odds=+132, line=None),
            mk(market_type=MARKET_ML, selection="Home", bookmaker="BetMGM", american_odds=-145, line=None),
            mk(market_type=MARKET_ML, selection="Away", bookmaker="BetMGM", american_odds=+140, line=None),
            # Spreads (BetMGM lags by 1.5 pts => stale flag)
            mk(market_type=MARKET_SPREAD, selection="Home", bookmaker="Pinnacle", american_odds=-110, line=-3.0),
            mk(market_type=MARKET_SPREAD, selection="Away", bookmaker="Pinnacle", american_odds=-110, line=+3.0),
            mk(market_type=MARKET_SPREAD, selection="Home", bookmaker="Circa", american_odds=-105, line=-3.5),
            mk(market_type=MARKET_SPREAD, selection="Away", bookmaker="Circa", american_odds=-115, line=+3.5),
            mk(market_type=MARKET_SPREAD, selection="Home", bookmaker="BetMGM", american_odds=-110, line=-1.5),
            mk(market_type=MARKET_SPREAD, selection="Away", bookmaker="BetMGM", american_odds=-110, line=+1.5),
            # Totals
            mk(market_type=MARKET_TOTAL, selection="Over", bookmaker="Pinnacle", american_odds=-110, line=47.5),
            mk(market_type=MARKET_TOTAL, selection="Under", bookmaker="Pinnacle", american_odds=-110, line=47.5),
            mk(market_type=MARKET_TOTAL, selection="Over", bookmaker="DraftKings", american_odds=+100, line=48.5),
            mk(market_type=MARKET_TOTAL, selection="Under", bookmaker="DraftKings", american_odds=-118, line=48.5),
            # Halves / Quarters (main game ML style)
            mk(market_type=MARKET_HALVES, selection="1st Half Home", bookmaker="Pinnacle", american_odds=-135, line=None),
            mk(market_type=MARKET_HALVES, selection="1st Half Away", bookmaker="Pinnacle", american_odds=+115, line=None),
            mk(market_type=MARKET_QUARTERS, selection="Q1 Home", bookmaker="DraftKings", american_odds=-125, line=None),
            mk(market_type=MARKET_QUARTERS, selection="Q1 Away", bookmaker="DraftKings", american_odds=+105, line=None),
            # Player props with ROLE labels only (never fabricated real names)
            mk(market_type=MARKET_PROPS, selection="Home QB Passing Yards Over", bookmaker="FanDuel", american_odds=-114, line=245.5),
            mk(market_type=MARKET_PROPS, selection="Home QB Passing Yards Under", bookmaker="FanDuel", american_odds=-106, line=245.5),
            mk(market_type=MARKET_PROPS, selection="Away RB Rushing Yards Over", bookmaker="BetMGM", american_odds=+105, line=78.5),
            mk(market_type=MARKET_PROPS, selection="Away RB Rushing Yards Under", bookmaker="BetMGM", american_odds=-125, line=78.5),
            # Alternate lines
            mk(market_type=MARKET_ALTERNATES, selection="Home Alt -6.5", bookmaker="Pinnacle", american_odds=+150, line=-6.5),
            mk(market_type=MARKET_ALTERNATES, selection="Home Alt -1.5", bookmaker="Circa", american_odds=-210, line=-1.5),
            mk(market_type=MARKET_ALTERNATES, selection="Team Total Home Over 28.5", bookmaker="DraftKings", american_odds=+120, line=28.5),
        ]
        return rows
