"""Stratum Signal Detection Engine (Phase 3) — the Logic.

Programmatically detects STEAM, REVERSE LINE MOVEMENT (RLM) and ARBITRAGE
from real-time data snapshots. Pure functions over caller-supplied snapshot
dicts — no I/O, no fabricated data. All odds arithmetic is delegated to
src/quant_engine.py (framework rule #1: math lives in ONE place).

Snapshot schema expected by detect_steam():
    {
      "timestamp": "<ISO8601>",
      "books": {
         "Pinnacle": {"home_american": -150, "away_american": 130},
         "Circa":    {"home_american": -155, "away_american": 135},
         ...
      }
    }
Only books whose prices were actually observed appear in a snapshot; missing
entries simply don't move (we never interpolate or invent quotes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("stratum.signals")

# A "major" (sharp/reference) book for steam verification purposes.
MAJOR_BOOKS = ("Pinnacle", "Circa", "BetMGM", "DraftKings", "FanDuel", "Caesars", "Westgate")

STEAM_MIN_BOOKS = 3            # >= 3 major books must move in sync
STEAM_WINDOW_MINUTES = 5       # within < 5 minutes


def _parse_ts(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _direction(prev: float, cur: float) -> int:
    """+1 if price moved UP (decimal-wise), -1 if DOWN, 0 if unchanged."""
    from src.quant_engine import american_to_decimal
    d_prev = american_to_decimal(prev)
    d_cur = american_to_decimal(cur)
    if d_cur > d_prev:
        return 1     # payout improved -> line moved toward the OTHER side
    if d_cur < d_prev:
        return -1    # payout shortened -> money coming in on this side
    return 0


def detect_steam(
    current_snapshot: Dict,
    previous_snapshots: List[Dict],
    window_minutes: int = STEAM_WINDOW_MINUTES,
) -> bool:
    """True when >= STEAM_MIN_BOOKS major books moved the SAME direction
    within ``window_minutes`` of the current snapshot's timestamp.

    'Same direction' is evaluated on the HOME price across books: every
    qualifying book must have moved either all-up or all-down vs its most
    recent earlier quote inside the window. Books with no history, equal
    prices, or timestamps outside the window are ignored — never guessed.
    """
    if not current_snapshot or not previous_snapshots:
        return False
    cur_ts = _parse_ts(current_snapshot.get("timestamp"))
    if cur_ts is None:
        logger.warning("detect_steam: current snapshot has no timestamp")
        return False
    cur_books = current_snapshot.get("books") or {}

    # Most recent prior quote per book that falls inside the window.
    latest_prior: Dict[str, tuple] = {}   # book -> (ts, quote_dict)
    for snap in previous_snapshots:
        ts = _parse_ts(snap.get("timestamp"))
        if ts is None:
            continue
        age = (cur_ts - ts).total_seconds() / 60.0
        if age <= 0 or age > window_minutes:
            continue
        for book, quote in (snap.get("books") or {}).items():
            prev_known = latest_prior.get(book)
            if prev_known is None or ts > prev_known[0]:
                latest_prior[book] = (ts, quote)

    up = down = 0
    for book, quote in cur_books.items():
        if book not in MAJOR_BOOKS:
            continue
        prior = latest_prior.get(book)
        if prior is None:
            continue
        prev_quote = prior[1]
        home_now = quote.get("home_american")
        home_prev = prev_quote.get("home_american")
        away_now = quote.get("away_american")
        away_prev = prev_quote.get("away_american")
        if home_now is None or home_prev is None:
            continue
        try:
            dir_home = _direction(home_prev, home_now)
            # Cross-check away moved opposite (a genuine market shift shortens
            # one side and lengthens the other). If away is unknown, trust home.
            if away_now is not None and away_prev is not None:
                dir_away = _direction(away_prev, away_now)
                if dir_home == 0 or dir_away == 0 or dir_home == dir_away:
                    continue  # only one side moved, or both same way -> noise
            if dir_home < 0:
                up += 1     # home decimal shortened => steam ON home
            else:
                down += 1   # home decimal lengthened => steam AWAY from home
        except ValueError as exc:
            logger.warning("detect_steam: bad odds for %s: %s", book, exc)
            continue

    return max(up, down) >= STEAM_MIN_BOOKS


def detect_rlm(public_ticket_pct: float, line_movement_dir: int) -> str:
    """Reverse Line Movement detector.

    ``public_ticket_pct``: % of tickets on Team A (the public side, 0..100).
    ``line_movement_dir``: +1 if the line moved TOWARD Team A,
                           -1 if it moved AWAY from Team A (toward Team B),
                            0 flat.

    Framework concept: 75% of the public on Team A but the line moves toward
    Team B ⇒ sharps are on B. Returns "SHARP_ON_B" when Public > 60% AND the
    line moved opposite the public; otherwise "NONE".
    """
    if public_ticket_pct is None or line_movement_dir is None:
        return "NONE"
    try:
        pct = float(public_ticket_pct)
        direction = int(line_movement_dir)
    except (TypeError, ValueError):
        return "NONE"
    if pct > 60.0 and direction < 0:
        return "SHARP_ON_B"
    return "NONE"


def find_arbitrage_opportunities(best_home: float, best_away: float) -> float:
    """Arbitrage margin (%) between the best available Home and Away prices.

    Delegates ALL math to quant_engine.arbitrage_pct. Returns e.g. 2.0 when
    the two best sides imply ~98% total, and exactly 0.0 when there is no arb.
    Missing/zero prices return 0.0 (unknown ≠ opportunity).
    """
    from src.quant_engine import arbitrage_pct
    try:
        return round(arbitrage_pct(best_home, best_away), 4)
    except (ValueError, TypeError):
        return 0.0


def scan_signals_for_match(
    snapshots: List[Dict],
    public_ticket_pct_home: Optional[float] = None,
    opening_home_american: Optional[float] = None,
    current_home_american: Optional[float] = None,
    best_home: Optional[float] = None,
    best_away: Optional[float] = None,
) -> Dict:
    """Convenience aggregator used by the Signals Dashboard tab.

    Builds the steam verdict from the snapshot timeline (last snapshot =
    current, earlier ones = history), derives line movement direction from
    open→current prices via quant comparisons, then runs all three detectors.
    Any input that is missing yields NONE/0 — we never fabricate a signal.
    """
    result = {"steam": False, "rlm": "NONE", "arb_pct": 0.0, "notes": []}
    snaps = [s for s in (snapshots or []) if _parse_ts(s.get("timestamp"))]
    snaps.sort(key=lambda s: _parse_ts(s.get("timestamp")))
    if len(snaps) >= 2:
        result["steam"] = detect_steam(snaps[-1], snaps[:-1])
        if result["steam"]:
            result["notes"].append(f"🔥 Steam: ≥{STEAM_MIN_BOOKS} majors moved together within {STEAM_WINDOW_MINUTES} min")
    else:
        result["notes"].append("Steam needs ≥2 timestamped snapshots (none recorded yet)")

    if public_ticket_pct_home is not None and opening_home_american is not None and current_home_american is not None:
        from src.quant_engine import american_to_decimal
        d_open = american_to_decimal(opening_home_american)
        d_cur = american_to_decimal(current_home_american)
        # dir convention for detect_rlm: +1 = moved TOWARD public side (home),
        # i.e. home decimal shortened.
        direction = 1 if d_cur < d_open else (-1 if d_cur > d_open else 0)
        result["rlm"] = detect_rlm(public_ticket_pct_home, direction)
        if result["rlm"] != "NONE":
            result["notes"].append(
                f"⚖️ RLM: public {public_ticket_pct_home:.0f}% on Home but line moved away → SHARP_ON_AWAY"
            )
    elif public_ticket_pct_home is None:
        result["notes"].append("RLM needs public ticket % (not fetched — unknown, not guessed)")

    result["arb_pct"] = find_arbitrage_opportunities(best_home, best_away)
    if result["arb_pct"] > 0:
        result["notes"].append(f"💸 Arbitrage: {result['arb_pct']:.2f}% guaranteed margin")
    return result
