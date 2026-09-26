"""Stratum Live Sentinel (Phase 4) — the background watcher.

A daemon thread that polls market makers on a fixed interval (default 30s),
compares every quote against the last snapshot persisted in SQLite, and
pushes an alert when something actionable appears:

  * LINE SHIFT ....... consensus spread for a market/selection moved by
                       >= LINE_SHIFT_THRESHOLD points since the last poll.
  * STALE ARB ........ quant_engine.arbitrage_pct(best opposing ML sides) > 0
                       (a guaranteed-margin opportunity is live right now).
  * STEAM ............ >= 3 major books moved together within 5 minutes
                       (delegated to src.signal_detector.detect_steam).

Alerts go out through a configurable webhook (Discord or Telegram chat-bot
API — both free push proxies onto your phone) AND are appended to the
``alerts`` table so the ALERTS tab can replay history offline.

Design constraints:
  * Zero Streamlit imports — this module must run headless on a server.
  * All odds math delegated to src.quant_engine / src.market_scanner.
  * Never crashes the loop: a bad poll logs and waits for the next cycle.
  * Fully testable: one_cycle() does a single deterministic pass; the thread
    is just sleep + one_cycle. send_alert()/post_webhook() are injectable.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("stratum.sentinel")

POLL_INTERVAL_SECONDS = 30       # cadence required by spec
LINE_SHIFT_THRESHOLD = 1.5       # points — same threshold as scanner stale rule
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ALERT = "alert"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentinel_state (
    match_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    bookmaker TEXT NOT NULL DEFAULT '',
    last_line REAL,
    last_american REAL,
    updated_at TEXT,
    PRIMARY KEY (match_id, market_type, selection, bookmaker)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Persistence helpers (SQLite = Obsidian memory for the watcher)
# ---------------------------------------------------------------------------

def ensure_schema(db_path: Optional[str] = None) -> None:
    """Idempotently create the sentinel tables on top of the Phase 1-3 DB."""
    from src.database import get_connection

    conn = get_connection(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def load_last_snapshot(match_id: str, db_path: Optional[str] = None) -> Optional[Dict]:
    """Return the most recent stored snapshot dict for a match, or None."""
    from src.database import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE match_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    except Exception:  # corrupt row is treated as "no history", never fatal
        return None
    finally:
        conn.close()


def save_snapshot(match_id: str, snapshot: Dict, db_path: Optional[str] = None) -> None:
    from src.database import get_connection

    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO snapshots (match_id, payload_json, created_at) VALUES (?, ?, ?)",
            (match_id, json.dumps(snapshot), _now_iso()),
        )
        # Keep only the last 200 snapshots per match (bounded memory).
        conn.execute(
            "DELETE FROM snapshots WHERE match_id = ? AND id NOT IN "
            "(SELECT id FROM snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 200)",
            (match_id, match_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_alert(match_id: str, kind: str, message: str, severity: str = SEVERITY_INFO,
                 delivered: bool = False, db_path: Optional[str] = None) -> int:
    """Persist an alert row; returns its id (ALERTS tab reads this table)."""
    from src.database import get_connection

    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO alerts (match_id, kind, severity, message, delivered, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (match_id, kind, severity, message, 1 if delivered else 0, _now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_alerts(limit: int = 50, db_path: Optional[str] = None) -> List[Dict]:
    from src.database import get_connection

    ensure_schema(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Webhook delivery — Discord-compatible body also works with Slack-style
# endpoints; Telegram gets auto-translated from the same text.
# ---------------------------------------------------------------------------

def post_webhook(url: str, message: str, timeout: float = 8.0) -> bool:
    """POST an alert to a webhook URL. Returns True on 2xx. Never raises.

    Auto-detects Telegram bot API URLs (``api.telegram.org/bot<token>/...``)
    and sends the JSON payload format they require; everything else receives
    the standard ``{"content": ...}`` Discord/slack-compatible envelope.
    """
    if not url:
        return False
    try:
        import requests

        if "api.telegram.org" in url:
            payload = {"text": f"[STRATUM] {message}"}
        else:
            payload = {"content": f"[STRATUM] {message}", "username": "Stratum Sentinel"}
        resp = requests.post(url, json=payload, timeout=timeout)
        return 200 <= resp.status_code < 300
    except Exception as exc:  # network down / bad URL -> logged, not raised
        logger.warning("Webhook delivery failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Detection logic — pure, deterministic, unit-testable
# ---------------------------------------------------------------------------

def build_market_index(rows: List[Dict]) -> Dict[tuple, Dict]:
    """Index scan rows into {(market_type, selection): aggregate}.

    Aggregate keeps the CONSENSUS line (median across books) and the best
    (highest-decimal) american price plus its book — computed via
    quant_engine only. Rows without prices are ignored, never guessed.
    """
    from src.quant_engine import american_to_decimal

    buckets: Dict[tuple, List[Dict]] = {}
    for r in rows or []:
        key = (r.get("market_type"), r.get("selection"))
        if r.get("american_odds") in (None, 0):
            continue
        buckets.setdefault(key, []).append(r)

    index: Dict[tuple, Dict] = {}
    for key, quotes in buckets.items():
        priced = sorted(quotes, key=lambda q: q["american_odds"])
        best = max(priced, key=lambda q: american_to_decimal(q["american_odds"]))
        lines = sorted(q["line"] for q in quotes if q.get("line") is not None)
        consensus_line = lines[len(lines) // 2] if lines else None
        index[key] = {
            "best_american": best["american_odds"],
            "best_book": best["bookmaker"],
            "consensus_line": consensus_line,
            "n_books": len(quotes),
        }
    return index


def _split_key(key: str) -> tuple:
    """'Spread|Home' -> ('Spread', 'Home'). Tolerant of missing separator."""
    parts = str(key).split("|", 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def detect_line_shifts(prev_index: Dict, curr_index: Dict,
                      threshold: float = LINE_SHIFT_THRESHOLD) -> List[Dict]:
    """Compare two market indexes; emit one shift event per MARKET GROUP.

    Spread/total markets are two-sided: Home -3.0 and Away +3.0 are the SAME
    line, so both sides are merged into a single event (signed toward Home).
    A line shift fires when |delta| >= threshold points; otherwise a price
    move fires when the best american price moved by >= 15 units (-110 ->
    -125). A market missing from either snapshot is NOT an event (unknown !=
    movement — we never fabricate alerts on first sight of a market).
    """
    events: List[Dict] = []
    if not prev_index:
        return events
    for key, cur in curr_index.items():
        prev = prev_index.get(key)
        if not prev:
            continue
        market, selection = _split_key(key)
        p_line, c_line = prev.get("consensus_line"), cur.get("consensus_line")
        if p_line is not None and c_line is not None:
            raw_delta = float(c_line) - float(p_line)
            # Signed toward Home: Away-side rows carry the mirrored delta.
            delta = -raw_delta if str(selection).lower().startswith("away") else raw_delta
            if abs(delta) >= threshold:
                group = next((e for e in events if e["kind"] == "line_shift"
                              and e["market_type"] == market), None)
                if group is None:
                    events.append({
                        "kind": "line_shift",
                        "market_type": market, "selection": selection,
                        "delta_points": round(delta, 2),
                        "message": (
                            f"LINE SHIFT {market}: consensus line "
                            f"{p_line:+.1f} -> {c_line:+.1f} ({delta:+.1f} pts)"
                        ),
                    })
                else:
                    group["delta_points"] = max(group["delta_points"], round(delta, 2),
                                                key=abs)
                continue
        p_price = prev.get("best_american")
        c_price = cur.get("best_american")
        if p_price is not None and c_price is not None:
            price_delta = float(c_price) - float(p_price)
            if abs(price_delta) >= 15:
                events.append({
                    "kind": "price_move",
                    "market_type": market, "selection": selection,
                    "delta_points": round(price_delta, 1),
                    "message": (
                        f"PRICE MOVE {market}/{selection}: "
                        f"{int(p_price):+d} -> {int(c_price):+d}"
                    ),
                })
    return events


def detect_arb(rows: List[Dict]) -> Optional[Dict]:
    """Live arbitrage margin across the best opposing moneyline prices.

    Uses quant_engine.arbitrage_pct exclusively (framework rule #1: math in
    one place). Returns {"arb_pct": x, "side_a", "odds_a", "side_b", "odds_b"}
    when the two best opposing sides imply < 100% total; None otherwise.
    """
    from src.quant_engine import american_to_decimal, arbitrage_pct

    best: Dict[str, float] = {}
    for r in rows or []:
        if r.get("market_type") != "ML" or r.get("american_odds") in (None, 0):
            continue
        sel = str(r.get("selection") or "")
        if sel not in ("Home", "Away"):
            continue
        odds = r["american_odds"]
        if sel not in best or american_to_decimal(odds) > american_to_decimal(best[sel]):
            best[sel] = odds
    if len(best) < 2:
        return None
    try:
        margin = arbitrage_pct(best["Home"], best["Away"])
    except (ValueError, TypeError):
        return None
    if margin > 0:
        return {
            "side_a": "Home", "odds_a": best["Home"],
            "side_b": "Away", "odds_b": best["Away"],
            "arb_pct": round(margin, 3),
        }
    return None


def extract_ml_snapshot(rows: List[Dict]) -> Dict:
    """Reduce raw scan rows into the signal_detector snapshot schema.

    Only books whose ML prices were actually observed appear. No invention.
    """
    books: Dict[str, Dict] = {}
    for r in rows or []:
        if r.get("market_type") != "ML" or r.get("american_odds") in (None, 0):
            continue
        sel = r.get("selection")
        if sel not in ("Home", "Away"):
            continue
        slot = books.setdefault(r.get("bookmaker", "?"), {})
        key = "home_american" if sel == "Home" else "away_american"
        if key not in slot:  # first observed price per book wins, no averaging
            slot[key] = r["american_odds"]
    return {"timestamp": _now_iso(), "books": books}


# ---------------------------------------------------------------------------
# The thread
# ---------------------------------------------------------------------------

class SentinelThread(threading.Thread):
    """Persistent market watcher. Singleton managed by main.py lifecycle.

    Every ``interval`` seconds it scans each tracked match, diffs the board
    against the last snapshot stored in SQLite and pushes alerts through the
    configured webhook. ``stop()`` shuts it down gracefully (joinable).
    """

    def __init__(
        self,
        tracked_matches: Optional[List[str]] = None,
        sport: str = "NFL",
        webhook_url: str = "",
        db_path: Optional[str] = None,
        interval: float = POLL_INTERVAL_SECONDS,
        scanner=None,
        notifier: Optional[Callable[[str, str], bool]] = None,
        name: str = "stratum-sentinel",
    ):
        super().__init__(daemon=True, name=name)
        self.tracked_matches = list(tracked_matches or [])
        self.sport = sport
        self.webhook_url = webhook_url
        self.db_path = db_path
        self.interval = float(interval)
        self._scanner = scanner
        self._notifier = notifier or self._default_notifier
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.alerts_sent = 0
        self.cycles_done = 0
        self.last_error: Optional[str] = None
        self.last_cycle_at: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        logger.info("Sentinel starting: %s matches @ %ss cadence",
                    len(self.tracked_matches), self.interval)
        while not self._stop_event.is_set():
            try:
                self.one_cycle()
            except Exception as exc:  # a bad poll must never kill the watcher
                self.last_error = str(exc)
                logger.exception("Sentinel cycle failed (continuing): %s", exc)
            self._stop_event.wait(self.interval)
        logger.info("Sentinel stopped after %d cycles", self.cycles_done)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and join; graceful, bounded."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self.is_alive() and not self._stop_event.is_set()

    def set_tracked(self, matches: List[str]) -> None:
        with self._lock:
            self.tracked_matches = list(matches)

    # -- one deterministic pass (the unit under test) -------------------------

    def one_cycle(self) -> List[Dict]:
        """Scan every tracked match once; emit alerts; return fired events."""
        ensure_schema(self.db_path)
        fired_all: List[Dict] = []
        with self._lock:
            matches = list(self.tracked_matches)
        for match_id in matches:
            fired_all.extend(self.scan_and_compare(match_id))
        self.cycles_done += 1
        self.last_cycle_at = _now_iso()
        return fired_all

    def scan_and_compare(self, match_id: str) -> List[Dict]:
        """One match: fetch -> diff vs stored snapshot -> alert on triggers."""
        rows = self._scan(match_id)
        if not rows:
            return []

        curr_index = {f"{k[0]}|{k[1]}": v for k, v in build_market_index(rows).items()}
        prev = load_last_snapshot(match_id, self.db_path)
        prev_index = prev.get("index", {}) if isinstance(prev, dict) else {}

        events: List[Dict] = []
        events.extend(detect_line_shifts(prev_index, curr_index))

        arb = detect_arb(rows)
        if arb:
            events.append({
                "kind": "arb",
                "severity": SEVERITY_ALERT,
                "message": (
                    f"ARB LIVE {match_id}: {arb['side_a']} {int(arb['odds_a']):+d} @ "
                    f"{arb['side_b']} {int(arb['odds_b']):+d} -> {arb['arb_pct']:.2f}% guaranteed"
                ),
            })

        curr_snap = extract_ml_snapshot(rows)
        prev_mls = (prev or {}).get("ml_history", []) if isinstance(prev, dict) else []
        from src.signal_detector import detect_steam
        if prev_mls and detect_steam(curr_snap, prev_mls[-3:]):
            events.append({
                "kind": "steam",
                "severity": SEVERITY_ALERT,
                "message": f"STEAM {match_id}: >=3 majors moved together within 5 min",
            })

        # Persist state BEFORE alerting so duplicates don't refire next cycle.
        save_snapshot(match_id, {
            "index": curr_index,
            "ml_history": (prev_mls + [curr_snap])[-10:],
            "timestamp": _now_iso(),
        }, self.db_path)

        for ev in events:
            sev = ev.get("severity") or SEVERITY_WARNING
            self.send_alert(ev["message"], sev, match_id=match_id, kind=ev["kind"])
        return events

    def _scan(self, match_id: str) -> List[Dict]:
        if self._scanner is None:
            from src.market_scanner import MarketScanner
            self._scanner = MarketScanner()
        try:
            return self._scanner.scan_match(match_id, sport=self.sport)
        except Exception as exc:
            logger.warning("Sentinel scan failed for %s: %s", match_id, exc)
            return []

    # -- alerting -------------------------------------------------------------

    def send_alert(self, message: str, severity: str = SEVERITY_INFO,
                   match_id: str = "", kind: str = "manual") -> bool:
        """Push an alert to the phone (webhook) and persist it locally.

        Returns True when the webhook accepted the payload. Delivery failure
        NEVER loses the alert — it is always written to the alerts table.
        """
        delivered = False
        if self.webhook_url:
            delivered = bool(self._notifier(message, severity))
        try:
            record_alert(match_id, kind, message, severity, delivered, self.db_path)
        except Exception as exc:  # pragma: no cover - disk/DB failure path
            logger.error("Failed to persist alert: %s", exc)
        self.alerts_sent += 1
        logger.info("SENTINEL [%s] %s (delivered=%s)", severity.upper(), message, delivered)
        return delivered

    def _default_notifier(self, message: str, severity: str) -> bool:
        prefix = {"alert": "!!", "warning": "*", "info": ""}.get(severity, "")
        return post_webhook(self.webhook_url, f"{prefix} {message}".strip())


# ---------------------------------------------------------------------------
# Global singleton management (main.py lifecycle hooks)
# ---------------------------------------------------------------------------

_SENTINEL: Optional[SentinelThread] = None
_MANAGER_LOCK = threading.Lock()


def start_sentinel(tracked_matches: List[str], sport: str = "NFL",
                   webhook_url: str = "", db_path: Optional[str] = None,
                   interval: float = POLL_INTERVAL_SECONDS, **kwargs) -> SentinelThread:
    """Start (or restart) the process-wide Sentinel singleton."""
    global _SENTINEL
    with _MANAGER_LOCK:
        stop_sentinel()
        _SENTINEL = SentinelThread(
            tracked_matches=tracked_matches, sport=sport,
            webhook_url=webhook_url, db_path=db_path, interval=interval, **kwargs,
        )
        _SENTINEL.start()
        return _SENTINEL


def get_sentinel() -> Optional[SentinelThread]:
    return _SENTINEL


def stop_sentinel() -> None:
    """Gracefully stop the current singleton, if any."""
    global _SENTINEL
    current = _SENTINEL
    if current is not None:
        try:
            current.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        _SENTINEL = None
