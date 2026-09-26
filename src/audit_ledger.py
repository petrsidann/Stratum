"""Stratum Audit Ledger (Phase 5) — the Black Box.

Every scan result, signal and bet recommendation is written to
``scan_history`` with a UUID, UTC timestamp, confidence score and a SHA-256
hash of the source data that produced it. Resolved/expired rows are sealed
by SQLite triggers (defined in src/database.py): any UPDATE or DELETE on a
non-pending row aborts with an IntegrityError. That is what makes the ledger
a flight recorder rather than a scratchpad — CLV claims can be verified
months later against data Stratum provably could not edit after the fact.

Privacy rule: raw LLM insight text is never stored — only its SHA-256 hash
(``llm_insight_hash``), so nothing sensitive lingers in the ledger.

All CLV math is delegated to src.quant_engine.clv_pct (framework rule #1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src import database
from src.quant_engine import clv_pct

logger = logging.getLogger("stratum.ledger")

VALID_STATUSES = ("pending", "resolved", "expired", "noise")
VALID_OUTCOMES = ("won", "lost", "push", "void")


class ImmutableRecordError(RuntimeError):
    """Raised when someone tries to mutate a sealed (resolved/expired) row."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_of(obj: Any) -> str:
    """Stable SHA-256 hex digest of any JSON-serializable object (or text).

    Used for both ``source_data_hash`` (proves WHICH quotes produced a
    signal) and ``llm_insight_hash`` (proves WHICH analysis was shown without
    storing the prose itself).
    """
    if isinstance(obj, (str, bytes)):
        payload = obj.encode("utf-8") if isinstance(obj, str) else obj
    else:
        payload = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ensure_ledger(db_path: Optional[str] = None) -> None:
    """Idempotently create the scan_history table + immutability triggers."""
    database.init_db(db_path)


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def log_scan(
    match_ref: str,
    market_type: str,
    selection: str,
    offered_odds: Optional[float] = None,
    fair_odds_calc: Optional[float] = None,
    edge_pct: Optional[float] = None,
    confidence_score: Optional[int] = None,
    llm_insight: Optional[str] = None,
    source_data: Any = None,
    sport: Optional[str] = None,
    book_agreement_count: int = 0,
    data_source: str = "live",
    below_threshold: bool = False,
    db_path: Optional[str] = None,
) -> str:
    """Append one immutable scan record; returns its UUID.

    EVERY result is logged — including sub-threshold ones, which are tagged
    status='noise' so future models can learn from the full distribution,
    not just the survivors. The row's provenance is captured as hashes:
    ``source_data_hash`` over the raw quotes fed to the detector, and
    ``llm_insight_hash`` over the analyst text (never the text itself).
    """
    if not match_ref or not market_type or not selection:
        raise ValueError("match_ref, market_type and selection are required")
    ensure_ledger(db_path)
    scan_id = str(uuid.uuid4())
    conn = database.get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO scan_history (
                id, timestamp, match_ref, sport, market_type, selection,
                offered_odds, fair_odds_calc, edge_pct, confidence_score,
                book_agreement_count, llm_insight_hash, source_data_hash,
                data_source, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id, _now_iso(), str(match_ref), sport, str(market_type),
                str(selection),
                float(offered_odds) if offered_odds not in (None, "") else None,
                float(fair_odds_calc) if fair_odds_calc not in (None, "") else None,
                float(edge_pct) if edge_pct is not None else None,
                int(confidence_score) if confidence_score is not None else None,
                int(book_agreement_count or 0),
                sha256_of(llm_insight) if llm_insight else None,
                sha256_of(source_data) if source_data is not None else None,
                data_source,
                "noise" if below_threshold else "pending",
            ),
        )
        conn.commit()
        return scan_id
    finally:
        conn.close()


def resolve_scan(
    scan_id: str,
    closing_odds: Optional[float],
    outcome: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Seal a pending scan with its closing price (+ optional outcome).

    Computes final CLV via quant_engine.clv_pct (decimal space, offered vs
    closing American odds) and flips status to 'resolved'. This is the LAST
    permitted write for the row — after it returns, the immutability triggers
    reject any further UPDATE/DELETE. Returns the updated record as a dict.

    Raises KeyError for unknown ids, ValueError for bad inputs and
    ImmutableRecordError when the row was already sealed.
    """
    if outcome is not None and outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {VALID_OUTCOMES}")
    ensure_ledger(db_path)
    conn = database.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM scan_history WHERE id = ?", (scan_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No scan with id {scan_id}")
        if row["status"] != "pending":
            raise ImmutableRecordError(
                f"Scan {scan_id} is already {row['status']} — the ledger is append-only."
            )
        clv_value = None
        if closing_odds not in (None, "", 0) and row["offered_odds"]:
            try:
                clv_value = round(clv_pct(float(row["offered_odds"]), float(closing_odds)), 4)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Cannot compute CLV: {exc}") from exc
        elif closing_odds in (None, "") and outcome is None:
            raise ValueError("resolve_scan needs at least a closing price or an outcome")

        try:
            conn.execute(
                """UPDATE scan_history
                   SET status = 'resolved', closing_odds = ?, clv_pct = ?,
                       outcome = ?, resolved_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (
                    float(closing_odds) if closing_odds not in (None, "") else None,
                    clv_value, outcome, _now_iso(), scan_id,
                ),
            )
            affected = conn.execute("SELECT changes() AS n").fetchone()["n"]
            if affected == 0:  # raced with another resolver
                conn.rollback()
                raise ImmutableRecordError(f"Scan {scan_id} changed under us; refusing to overwrite.")
            _refresh_market_stats(conn, row["sport"], row["market_type"])
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ImmutableRecordError(f"Ledger rejected the write: {exc}") from exc
        updated = conn.execute(
            "SELECT * FROM scan_history WHERE id = ?", (scan_id,)
        ).fetchone()
        return dict(updated)
    finally:
        conn.close()


def _refresh_market_stats(conn: sqlite3.Connection, sport: Optional[str],
                          market_type: Optional[str]) -> None:
    """Recompute the global win-rate/CLV stats for one (sport, market_type).

    Called inside resolve_scan's transaction so the aggregate always mirrors
    the sealed ledger rows — it is derived data, never hand-edited.
    """
    s = sport or ""
    m = market_type or ""
    agg = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN outcome = 'won' THEN 1 ELSE 0 END) AS wins,
                  AVG(clv_pct) AS avg_clv
           FROM scan_history
           WHERE status = 'resolved' AND IFNULL(sport,'') = ? AND market_type = ?""",
        (s, m),
    ).fetchone()
    conn.execute(
        """INSERT INTO market_stats (sport, market_type, n_resolved, n_won, avg_clv_pct, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(sport, market_type) DO UPDATE SET
             n_resolved = excluded.n_resolved,
             n_won = excluded.n_won,
             avg_clv_pct = excluded.avg_clv_pct,
             updated_at = excluded.updated_at""",
        (s, m, int(agg["n"] or 0), int(agg["wins"] or 0),
         round(float(agg["avg_clv"]), 4) if agg["avg_clv"] is not None else None,
         _now_iso()),
    )


def market_win_rate(sport: Optional[str], market_type: str,
                    db_path: Optional[str] = None) -> Optional[float]:
    """Global win rate (0..1) of resolved signals for a market type. Feeds
    confidence_scorer's history term. None when no sample exists."""
    ensure_ledger(db_path)
    conn = database.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT n_resolved, n_won FROM market_stats WHERE IFNULL(sport,'') = ? AND market_type = ?",
            (sport or "", market_type or ""),
        ).fetchone()
    finally:
        conn.close()
    if not row or int(row["n_resolved"]) <= 0:
        return None
    return round(int(row["n_won"]) / int(row["n_resolved"]), 4)


def expire_scan(scan_id: str, db_path: Optional[str] = None) -> int:
    """Seal a pending scan that went unattended before its game started."""
    ensure_ledger(db_path)
    conn = database.get_connection(db_path)
    try:
        try:
            cur = conn.execute(
                "UPDATE scan_history SET status = 'expired', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_now_iso(), scan_id),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ImmutableRecordError(f"Ledger rejected the write: {exc}") from exc
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def list_scans(
    status: Optional[str] = None,
    sport: Optional[str] = None,
    market_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 500,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filtered, newest-first view of the ledger (read-only API)."""
    ensure_ledger(db_path)
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if sport:
        clauses.append("sport = ?")
        params.append(sport)
    if market_type:
        clauses.append("market_type = ?")
        params.append(market_type)
    if date_from:
        clauses.append("date >= ?")
        params.append(str(date_from))
    if date_to:
        # inclusive of the whole end day (timestamps are ISO strings)
        clauses.append("timestamp < datetime(?, '+1 day')")
        params.append(str(date_to))
    sql = "SELECT * FROM scan_history"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(int(limit))
    conn = database.get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def get_scan(scan_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    ensure_ledger(db_path)
    conn = database.get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM scan_history WHERE id = ?", (scan_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ledger_metrics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sport: Optional[str] = None,
    market_type: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate header stats for the AUDIT view.

    Returns total scans, avg confidence, realized avg CLV%, win rate vs the
    closing line (share of resolved bets priced better than close) and the
    settled win rate. Empty windows yield None/'UNKNOWN'-friendly zeros —
    silence is data, we never extrapolate a fake average.
    """
    rows = list_scans(status=None, sport=sport, market_type=market_type,
                      date_from=date_from, date_to=date_to, limit=100000,
                      db_path=db_path)
    metrics = {
        "total_scans": len(rows),
        "avg_confidence": None,
        "realized_clv_pct": None,
        "beat_close_rate_pct": None,
        "win_rate_pct": None,
        "n_resolved": 0,
        "n_noise": 0,
    }
    confs = [int(r["confidence_score"]) for r in rows if r["confidence_score"] is not None]
    if confs:
        metrics["avg_confidence"] = round(sum(confs) / len(confs), 1)
    clvs = [float(r["clv_pct"]) for r in rows if r["clv_pct"] is not None]
    if clvs:
        metrics["realized_clv_pct"] = round(sum(clvs) / len(clvs), 3)
        metrics["beat_close_rate_pct"] = round(
            sum(1 for c in clvs if c > 0) / len(clvs) * 100.0, 1)
    resolved = [r for r in rows if r["status"] == "resolved"]
    metrics["n_resolved"] = len(resolved)
    metrics["n_noise"] = sum(1 for r in rows if r["status"] == "noise")
    settled = [r for r in resolved if r.get("outcome") in ("won", "lost")]
    if settled:
        metrics["win_rate_pct"] = round(
            sum(1 for r in settled if r["outcome"] == "won") / len(settled) * 100.0, 1)
    return metrics


def historical_win_rate(
    market_type: Optional[str] = None,
    selection_key: Optional[str] = None,
    min_samples: int = 5,
    db_path: Optional[str] = None,
) -> Optional[float]:
    """Win rate (0..1) of previously RESOLVED signals of the same kind.

    Feeds the confidence scorer's history component. Selection keys are
    matched loosely (substring, e.g. a player name inside a prop selection)
    so 'Luka Doncic Points Over' history informs the same player's next line.
    Returns None (never a guessed 0.5) when there is insufficient sample.
    """
    ensure_ledger(db_path)
    clauses = ["status = 'resolved'", "outcome IN ('won','lost')"]
    params: List[Any] = []
    if market_type:
        clauses.append("market_type = ?")
        params.append(market_type)
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT outcome, selection FROM scan_history WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    if selection_key:
        key = str(selection_key).lower()
        rows = [r for r in rows if key in str(r["selection"]).lower()]
    if len(rows) < min_samples:
        return None
    wins = sum(1 for r in rows if r["outcome"] == "won")
    return round(wins / len(rows), 4)
