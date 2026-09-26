"""Stratum CLV Auditor (Phase 3) — the Scoreboard.

Records every bet vs its closing line to measure long-term edge (framework
Section 1, Concept #2: CLV is the only honest scoreboard). All history lives
in SQLite ("Obsidian Memory") so Stratum learns over time.

CLV math is defined ONCE in src.quant_engine.clv_pct (decimal-space):
    clv% = (decimal(closing) / decimal(placed) - 1) * 100
Positive CLV = we beat the close. The user spec wrote this ratio informally
on American numbers; dividing raw American odds (e.g. -110 → -105) yields a
meaningless negative value for a genuine price improvement, so we normalize
through american_to_decimal first. This module never inlines any other math.
"""

from __future__ import annotations

import logging
from typing import Optional

from src import database
from src.quant_engine import clv_pct

logger = logging.getLogger("stratum.clv")

VALID_STATUSES = ("open", "won", "lost", "void")


def _ensure_db(db_path: Optional[str] = None) -> None:
    """Idempotently create tables before use (safe on every entry point)."""
    database.init_db(db_path)


def record_bet(
    match_id: str,
    market: str,
    selection: str,
    odds_placed: float,
    stake: float,
    data_source: str = "manual",
    db_path: Optional[str] = None,
) -> int:
    """Insert a new bet with status='open'; returns the new row id."""
    if not match_id or not market or not selection:
        raise ValueError("match_id, market and selection are required")
    if not odds_placed:
        raise ValueError("odds_placed must be a real American price — never blank/zero")
    if stake is None or float(stake) <= 0:
        raise ValueError("stake must be positive")
    _ensure_db(db_path)
    conn = database.get_connection(db_path)
    try:
        return database.insert(conn, "bets_log", {
            "match_id": match_id,
            "market": market,
            "selection": selection,
            "odds_placed": float(odds_placed),
            "stake": float(stake),
            "status": "open",
            "data_source": data_source,
        })
    finally:
        conn.close()


def update_closing_line(bet_id: int, final_market_odds: float, db_path: Optional[str] = None) -> float:
    """Lock in the closing price for a bet and compute its CLV.

    Returns the computed clv_value (%). Raises KeyError for unknown bets and
    ValueError when either price is missing/invalid (quant_engine enforces).
    """
    _ensure_db(db_path)
    conn = database.get_connection(db_path)
    try:
        row = database.fetch_one(conn, "bets_log", bet_id)
        if row is None:
            raise KeyError(f"No bet with id {bet_id}")
        value = clv_pct(row["odds_placed"], float(final_market_odds))
        database.update(conn, "bets_log", bet_id, {
            "closing_odds": float(final_market_odds),
            "clv_value": round(value, 4),
        })
        return value
    finally:
        conn.close()


def settle_bet(bet_id: int, status: str, db_path: Optional[str] = None) -> int:
    """Mark a bet won/lost/void. Returns rows affected."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    _ensure_db(db_path)
    conn = database.get_connection(db_path)
    try:
        return database.update(conn, "bets_log", bet_id, {"status": status})
    finally:
        conn.close()


def get_performance_report(days: int = 30, db_path: Optional[str] = None) -> dict:
    """Aggregate ROI, Win Rate, Avg CLV, Best/Worst markets over the window.

    Settled (won/lost) bets drive ROI & win rate; bets with a recorded
    closing line drive Avg CLV. Open bets still count toward total staked
    exposure but never fabricate a result. Empty windows yield zeros and an
    explicit ``has_data=False`` — 'Unknown' is a valid state.
    """
    _ensure_db(db_path)
    conn = database.get_connection(db_path)
    try:
        cutoff = f"-{int(abs(days))} days"
        rows = conn.execute(
            "SELECT * FROM bets_log WHERE created_at >= datetime('now', ?)", (cutoff,)
        ).fetchall()
    finally:
        conn.close()

    report = {
        "days": int(days),
        "n_bets": len(rows),
        "n_settled": 0,
        "total_staked": 0.0,
        "net_profit": 0.0,
        "roi_pct": 0.0,
        "win_rate_pct": 0.0,
        "avg_clv_pct": None,
        "beat_close_rate_pct": None,
        "best_market": None,
        "worst_market": None,
        "has_data": False,
    }
    if not rows:
        return report

    settled_profit = 0.0
    settled_stake = 0.0
    wins = 0
    clvs = []
    per_market: dict = {}
    for r in rows:
        stake = float(r["stake"] or 0.0)
        report["total_staked"] += stake
        mkt = r["market"]
        bucket = per_market.setdefault(mkt, {"profit": 0.0, "stake": 0.0})
        if r["status"] == "won":
            from src.quant_engine import american_to_decimal
            profit = stake * (american_to_decimal(float(r["odds_placed"])) - 1.0)
            settled_profit += profit
            settled_stake += stake
            bucket["profit"] += profit
            bucket["stake"] += stake
            bucket["settled"] = True
            wins += 1
            report["n_settled"] += 1
        elif r["status"] == "lost":
            settled_profit -= stake
            settled_stake += stake
            bucket["profit"] -= stake
            bucket["stake"] += stake
            bucket["settled"] = True
            report["n_settled"] += 1
        elif r["status"] == "void":
            bucket["stake"] += stake
        if r["clv_value"] is not None:
            clvs.append(float(r["clv_value"]))

    report["net_profit"] = round(settled_profit, 2)
    if settled_stake > 0:
        report["roi_pct"] = round(settled_profit / settled_stake * 100.0, 2)
    if report["n_settled"]:
        report["win_rate_pct"] = round(wins / report["n_settled"] * 100.0, 2)
    if clvs:
        report["avg_clv_pct"] = round(sum(clvs) / len(clvs), 3)
        report["beat_close_rate_pct"] = round(sum(1 for c in clvs if c > 0) / len(clvs) * 100.0, 1)
    rated = [
        (m, b["profit"] / b["stake"] * 100.0)
        for m, b in per_market.items() if b["stake"] > 0 and b.get("settled")
    ]
    if rated:
        rated.sort(key=lambda kv: kv[1])
        report["worst_market"] = {"market": rated[0][0], "roi_pct": round(rated[0][1], 2)}
        report["best_market"] = {"market": rated[-1][0], "roi_pct": round(rated[-1][1], 2)}
    report["has_data"] = True
    return report


def list_bets(status: Optional[str] = None, db_path: Optional[str] = None) -> list:
    """Return logged bets (newest first), optionally filtered by status."""
    _ensure_db(db_path)
    conn = database.get_connection(db_path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM bets_log WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bets_log ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
