#!/usr/bin/env python3
"""Stratum Ghost Server — GitHub Actions entry point.

This is the ONLY thing the scheduled workflow runs:

    python scripts/run_daily_scan.py

Architecture ("Logic in Actions, UI in Pages"):
  * All backend logic executes here, inside an ephemeral GHA runner.
  * It drives the SAME core engine the old Streamlit app used
    (market_scanner / quant_engine / signal_detector / confidence_scorer /
    database / clv_auditor) and writes strictly-validated JSON artifacts:

      data/latest_scan.json       — full opportunity board + signals
      data/portfolio_stats.json   — bankroll / CLV / ROI aggregates
      data/history.csv            — append-only CLV tracking ledger

  * The static frontend (frontend/, GitHub Pages) only ever fetches these
    files. No server-side rendering anywhere.

Reliability contract (framework rule: never crash the whole workflow):
  * Every network touchpoint has timeout + bounded retry + graceful skip.
  * Weather/news context is cached to .cache/context_cache.json so repeated
    cron runs within the TTL window make ZERO extra API calls.
  * A per-match failure is logged and the loop continues; only a total
    inability to write valid output exits non-zero (so GHA shows red).
  * LLMs (Groq → Gemini fallback) are optional garnish: with no keys the
    scan still completes using the deterministic fallback insight.

Exit codes: 0 = success (JSON written & validated), 1 = fatal (no output).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Make "src" importable regardless of CWD (GHA runs from repo root anyway).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("stratum.scan")

DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
SCAN_JSON = DATA_DIR / "latest_scan.json"
STATS_JSON = DATA_DIR / "portfolio_stats.json"
HISTORY_CSV = DATA_DIR / "history.csv"
CONTEXT_CACHE = CACHE_DIR / "context_cache.json"

MIN_CONFIDENCE = int(os.getenv("STRATUM_MIN_CONFIDENCE", "60"))
CRITICAL_CONFIDENCE = int(os.getenv("STRATUM_CRITICAL_CONFIDENCE", "80"))
CONTEXT_CACHE_TTL_SECONDS = int(os.getenv("STRATUM_CONTEXT_TTL", "240"))  # < 5 min cron
WEATHER_LAT = float(os.getenv("STRATUM_WEATHER_LAT", "40.7128"))
WEATHER_LON = float(os.getenv("STRATUM_WEATHER_LON", "-74.0060"))

HISTORY_FIELDS = [
    "scan_ts", "match_id", "sport", "market_type", "selection",
    "offered_odds", "fair_odds", "edge_pct", "confidence", "data_source",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tracked slate. In production this comes from the DB `games` table; for the
# MVP the workflow recalculates fresh each run, so we seed the tracked list
# from the DB if present and fall back to a small explicit slate otherwise.
# ---------------------------------------------------------------------------

DEFAULT_SLATE: List[Dict[str, str]] = [
    {"match_id": "NFL:KC-BUF", "sport": "NFL", "home": "Chiefs", "away": "Bills"},
    {"match_id": "NFL:PHI-DAL", "sport": "NFL", "home": "Eagles", "away": "Cowboys"},
    {"match_id": "NBA:BOS-NYK", "sport": "NBA", "home": "Celtics", "away": "Knicks"},
    {"match_id": "NBA:LAL-GSW", "sport": "NBA", "home": "Lakers", "away": "Warriors"},
]


def load_tracked_matches(db_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Tracked matches from SQLite games table; DEFAULT_SLATE if unavailable."""
    try:
        from src import database

        conn = database.get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT sport, home_team, away_team FROM games LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
        if rows:
            out = []
            for r in rows:
                sport, home, away = r["sport"], r["home_team"], r["away_team"]
                out.append({
                    "match_id": f"{sport}:{home}-{away}",
                    "sport": sport, "home": home, "away": away,
                })
            logger.info("Loaded %d tracked matches from database.", len(out))
            return out
    except Exception as exc:  # missing DB / schema drift -> keep going
        logger.warning("DB slate unavailable (%s); using default slate.", exc)
    logger.info("Using default slate (%d matches).", len(DEFAULT_SLATE))
    return list(DEFAULT_SLATE)


# ---------------------------------------------------------------------------
# Context cache (weather + news): minimize API calls inside the 5-min window
# ---------------------------------------------------------------------------

def get_context() -> Dict:
    """Return {'weather': ..., 'news': ..., 'cached_at': ..., 'fresh': bool}.

    Reads .cache/context_cache.json first; refreshes via src.scraper only
    when the cache is older than CONTEXT_CACHE_TTL_SECONDS. Any network
    failure yields the stale cache (or empty context) — never an exception.
    """
    cached: Dict = {}
    if CONTEXT_CACHE.exists():
        try:
            cached = json.loads(CONTEXT_CACHE.read_text(encoding="utf-8"))
            age = time.time() - datetime.fromisoformat(cached["cached_at"]).timestamp()
            if age < CONTEXT_CACHE_TTL_SECONDS:
                cached["fresh"] = True
                logger.info("Context cache hit (age %.0fs) — 0 API calls.", age)
                return cached
        except Exception as exc:
            logger.warning("Context cache unreadable (%s); refreshing.", exc)
            cached = {}

    result = {"weather": None, "news": "", "cached_at": _now_iso(), "fresh": False}
    try:
        from src import scraper
        try:
            result["weather"] = scraper.fetch_weather(WEATHER_LAT, WEATHER_LON)
        except Exception as exc:
            logger.warning("Weather fetch failed (skipped): %s", exc)
        try:
            result["news"] = scraper.fetch_match_context("today's featured game odds news injury report")
        except Exception as exc:
            logger.warning("News fetch failed (skipped): %s", exc)
    except Exception as exc:
        logger.warning("Scraper module unavailable (offline mode): %s", exc)

    # On total failure keep whatever stale data we had rather than lose it.
    if cached and result["weather"] is None and not result["news"]:
        cached["fresh"] = False
        return cached

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CONTEXT_CACHE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist context cache: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Per-match pipeline: scan -> fair odds -> edges -> signals -> confidence
# ---------------------------------------------------------------------------

def _build_snapshots(rows: List[Dict]) -> List[Dict]:
    """Group ML quotes into timestamped snapshots for detect_steam()."""
    by_ts: Dict[str, Dict] = {}
    for r in rows:
        if r.get("market_type") != "ML" or r.get("american_odds") is None:
            continue
        snap = by_ts.setdefault(r["timestamp"], {"timestamp": r["timestamp"], "books": {}})
        side = "home" if r.get("selection") == "Home" else "away"
        book = snap["books"].setdefault(r["bookmaker"], {})
        book[f"{side}_american"] = r["american_odds"]
    return sorted(by_ts.values(), key=lambda s: s["timestamp"])


def scan_one(scanner, match: Dict[str, str], context: Dict) -> Dict:
    """Full pipeline for one match. Raises nothing — returns partial results."""
    from src import quant_engine as qe
    from src.signal_detector import scan_signals_for_match
    from src.confidence_scorer import calculate_confidence, confidence_band

    match_id = match["match_id"]
    rows = scanner.scan_match(match_id, match.get("sport", ""))
    compare = scanner.compare_books(rows)

    opportunities: List[Dict] = []
    for group in compare["groups"]:
        american = group.get("best_american")
        if american is None:
            continue
        try:
            implied = qe.implied_probability(float(american))
        except (ValueError, TypeError):
            continue
        # Fair probability for a two-way market: de-vig the best opposing pair.
        counterpart = next(
            (g for g in compare["groups"]
             if g["market_type"] == group["market_type"]
             and g["selection"] != group["selection"]
             and g.get("best_american") is not None),
            None,
        )
        fair_prob = implied
        if counterpart is not None:
            try:
                p_a, _p_b = qe.remove_vig_two_way(float(american), float(counterpart["best_american"]))
                fair_prob = p_a
            except (ValueError, TypeError):
                pass
        edge = round(qe.ev_pct(fair_prob, float(american)), 3)
        fair_odds = round(100.0 / fair_prob - 100.0, 1) if 0 < fair_prob < 1 else None
        agreement = sum(
            1 for g in compare["groups"]
            if g["market_type"] == group["market_type"] and g.get("best_american") is not None
        )
        confidence = calculate_confidence(
            edge_pct=edge,
            book_agreement_count=agreement,
            data_age_seconds=0,          # snapshot taken moments ago
            historical_win_rate=None,    # unknown on a fresh recalc — never guessed
        )
        opportunities.append({
            "market_type": group["market_type"],
            "selection": group["selection"],
            "best_book": group.get("best_book"),
            "offered_odds": american,
            "fair_odds": fair_odds,
            "fair_probability": round(fair_prob, 4),
            "edge_pct": edge,
            "kelly_quarter": round(qe.kelly_criterion(fair_prob, qe.american_to_decimal(float(american)), 0.25), 4),
            "confidence": confidence,
            "band": confidence_band(confidence),
            "above_threshold": confidence >= MIN_CONFIDENCE,
        })

    opportunities.sort(key=lambda o: o["confidence"], reverse=True)

    snaps = _build_snapshots(rows)
    signals = scan_signals_for_match(snaps) if snaps else {"steam": False, "rlm": "NONE", "arb_pct": 0.0, "notes": []}

    top = opportunities[0] if opportunities else None
    return {
        "match_id": match_id,
        "sport": match.get("sport"),
        "home": match.get("home"),
        "away": match.get("away"),
        "scanned_at": _now_iso(),
        "data_source": rows[0]["data_source"] if rows else "none",
        "quote_count": len(rows),
        "opportunities": opportunities,
        "stale_flags": compare["stale_flags"],
        "arb_flags": compare["arb_flags"],
        "signals": signals,
        "top_edge_pct": top["edge_pct"] if top else None,
        "top_confidence": top["confidence"] if top else 0,
        "context_used": bool(context.get("weather") or context.get("news")),
    }


def attach_insights(results: List[Dict], context: Dict) -> None:
    """Optional LLM garnish per critical signal. Never blocks/fails the scan."""
    try:
        from src.reasoning_engine import generate_insight
    except Exception as exc:
        logger.warning("Reasoning engine unavailable (%s); skipping insights.", exc)
        return
    for res in results:
        crit = res["top_confidence"] >= CRITICAL_CONFIDENCE
        steam = bool(res.get("signals", {}).get("steam"))
        if not (crit or steam):
            continue
        top = res["opportunities"][0] if res["opportunities"] else {}
        try:
            res["insight"] = generate_insight(
                {"match": res["match_id"], "market": top.get("market_type"),
                 "selection": top.get("selection"), "edge_pct": top.get("edge_pct"),
                 "confidence": res["top_confidence"]},
                str(context.get("news") or ""),
            )
        except Exception as exc:
            logger.warning("Insight generation failed for %s (kept deterministic): %s", res["match_id"], exc)


# ---------------------------------------------------------------------------
# Portfolio stats (from SQLite bets_log via clv_auditor; zeros if empty)
# ---------------------------------------------------------------------------

def build_portfolio_stats() -> Dict:
    stats = {
        "generated_at": _now_iso(),
        "window_days": 30,
        "has_data": False,
        "n_bets": 0, "n_settled": 0,
        "total_staked": 0.0, "net_profit": 0.0, "roi_pct": 0.0,
        "win_rate_pct": 0.0, "avg_clv_pct": None, "beat_close_rate_pct": None,
        "best_market": None, "worst_market": None,
    }
    try:
        from src.clv_auditor import get_performance_report
        stats.update(get_performance_report(days=30))
        stats["generated_at"] = _now_iso()
    except Exception as exc:
        logger.warning("Portfolio stats unavailable (empty ledger is fine): %s", exc)
    return stats


# ---------------------------------------------------------------------------
# Strict validation + atomic writers ("Clean Data" constraint)
# ---------------------------------------------------------------------------

def validate_scan(doc: Dict) -> List[str]:
    """Return a list of schema violations ([] means the doc is publishable)."""
    errors: List[str] = []
    for key in ("schema_version", "generated_at", "status", "summary", "results"):
        if key not in doc:
            errors.append(f"missing top-level key '{key}'")
    if not isinstance(doc.get("results"), list):
        errors.append("'results' must be a list")
        return errors
    for i, res in enumerate(doc["results"]):
        if not isinstance(res.get("match_id"), str) or not res["match_id"]:
            errors.append(f"results[{i}].match_id must be a non-empty string")
        if not isinstance(res.get("top_confidence"), int) or not (0 <= res["top_confidence"] <= 95):
            errors.append(f"results[{i}].top_confidence must be int in [0,95]")
        for j, opp in enumerate(res.get("opportunities", [])):
            c = opp.get("confidence")
            if not isinstance(c, int) or not (0 <= c <= 95):
                errors.append(f"results[{i}].opportunities[{j}].confidence out of range")
            e = opp.get("edge_pct")
            if e is not None and not isinstance(e, (int, float)):
                errors.append(f"results[{i}].opportunities[{j}].edge_pct must be numeric")
    return errors


def write_json_atomic(path: Path, doc: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX; readers never see a half-written file


def append_history(results: List[Dict]) -> int:
    """Append above-threshold opportunities to data/history.csv (CLV tracking)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_CSV.exists()
    n = 0
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new_file:
            writer.writeheader()
        for res in results:
            for opp in res["opportunities"]:
                if not opp["above_threshold"]:
                    continue
                writer.writerow({
                    "scan_ts": res["scanned_at"], "match_id": res["match_id"],
                    "sport": res["sport"], "market_type": opp["market_type"],
                    "selection": opp["selection"], "offered_odds": opp["offered_odds"],
                    "fair_odds": opp["fair_odds"], "edge_pct": opp["edge_pct"],
                    "confidence": opp["confidence"], "data_source": res["data_source"],
                })
                n += 1
    return n


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    started = time.monotonic()
    logger.info("=== Stratum Ghost Server scan starting ===")
    context = get_context()

    from src.market_scanner import MarketScanner
    scanner = MarketScanner()

    results: List[Dict] = []
    failures: List[str] = []
    for match in load_tracked_matches():
        try:
            results.append(scan_one(scanner, match, context))
        except Exception as exc:  # one bad match must never kill the run
            failures.append(match["match_id"])
            logger.error("Match %s failed (continuing): %s", match["match_id"], exc)

    try:
        attach_insights(results, context)
    except Exception as exc:
        logger.warning("Insight phase skipped: %s", exc)

    critical = [r for r in results if r["top_confidence"] >= CRITICAL_CONFIDENCE]
    steam = [r for r in results if r["signals"].get("steam")]
    arbs = [r for r in results if r["signals"].get("arb_pct", 0) > 0]
    all_opps = [o for r in results for o in r["opportunities"]]
    sample_board = bool(results) and all(r["data_source"] == "sample" for r in results)
    doc = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "status": "degraded" if (failures or sample_board) else "live",
        "duration_seconds": round(time.monotonic() - started, 2),
        "summary": {
            "matches_scanned": len(results),
            "matches_failed": len(failures),
            "failed_match_ids": failures,
            "total_opportunities": len(all_opps),
            "above_threshold": sum(1 for o in all_opps if o["above_threshold"]),
            "critical_alerts": len(critical),
            "steam_signals": len(steam),
            "arbitrage_found": len(arbs),
            "min_confidence": MIN_CONFIDENCE,
            "critical_confidence": CRITICAL_CONFIDENCE,
            "data_sources": sorted({r["data_source"] for r in results}) or ["none"],
        },
        "context": {"weather": context.get("weather"), "cache_fresh": context.get("fresh", False)},
        "results": results,
    }

    errors = validate_scan(doc)
    if errors:
        for err in errors:
            logger.error("VALIDATION: %s", err)
        logger.error("Refusing to publish invalid scan output.")
        return 1

    write_json_atomic(SCAN_JSON, doc)
    write_json_atomic(STATS_JSON, build_portfolio_stats())
    n_hist = append_history(results)

    logger.info("Published %s (%d matches, %d opps, %d critical)", SCAN_JSON.name, len(results), len(all_opps), len(critical))
    logger.info("Published %s; appended %d history rows", STATS_JSON.name, n_hist)
    logger.info("=== Scan complete in %.1fs ===", time.monotonic() - started)

    # Expose outputs to the workflow (for the optional alert step).
    gh_env = os.getenv("GITHUB_OUTPUT")
    if gh_env:
        try:
            with open(gh_env, "a", encoding="utf-8") as fh:
                fh.write(f"critical_alerts={len(critical)}\n")
                fh.write(f"matches_scanned={len(results)}\n")
        except OSError as exc:
            logger.warning("Could not write GITHUB_OUTPUT: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
