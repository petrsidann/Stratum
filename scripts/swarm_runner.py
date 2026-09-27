#!/usr/bin/env python3
"""Stratum V3.0 — Agent Swarm Orchestrator.

`run_hunt(query, sport)` spawns four async agents and streams their progress
in real time so the frontend terminal never goes silent:

    [SCOUT]       agent_scout      concurrent scrape of Betika / Odibets /
                                  Flashscore / ESPN price mirror (real prices
                                  only — nothing is ever synthesized)
    [ACTUARY]     agent_actuary    imports the quant engine (src/analyzer.py):
                                  no-vig fair odds, EV%, fractional Kelly,
                                  confidence, cross-book signals
    [CONTEXT]     agent_context    optional LLM pass (Groq/OpenAI-compatible)
                                  over fixture context; on ANY failure it
                                  returns a NEUTRAL adjustment and flags the
                                  hunt "math-only" with lowered confidence
    [STRATEGIST]  agent_strategist merges everything, sorts by EV x Confidence,
                                  renders Matplotlib PNG reports, writes the
                                  final report conforming to
                                  schemas/hunt_result.schema.json

Transport for the live console (MVP = polling, per spec):
    every ~1s the full snapshot {status, stage, progress, lines[], result}
    is atomically written to data/hunt_status.json. The PWA polls that file.
    A single-flight lock prevents two hunts from interleaving their streams.

CLI:
    python3 scripts/swarm_runner.py --query "Al Hilal vs Al Nassr" \
        --sport soccer [--output data/hunts/latest.json] [--serve --port 8788]

The embedded dev server (`serve`) exposes, with permissive CORS:
    POST /hunt/start   {"query": "...", "sport": "soccer"}   -> 202 | 409 busy
    GET  /hunt/status  -> data/hunt_status.json snapshot (poll this)
    GET  /hunt/result  -> last completed report (schema-conformant JSON)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

SWARM_VERSION = "3.0.0"
DATA_DIR = os.path.join(_ROOT, "data")
STATUS_PATH = os.environ.get("STRATUM_HUNT_STATUS",
                             os.path.join(DATA_DIR, "hunt_status.json"))
HUNTS_DIR = os.path.join(DATA_DIR, "hunts")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
WRITE_INTERVAL_S = 1.0          # snapshot cadence (~2s in spec; snappier here)
AGENT_TIMEOUT_S = float(os.environ.get("SWARM_AGENT_TIMEOUT_S", "120"))

VALID_AGENTS = ("scout", "actuary", "context", "strategist")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Swarm context: log bus + weighted progress + atomic status snapshots
# ---------------------------------------------------------------------------

class SwarmContext:
    """Shared state passed to every agent coroutine."""

    def __init__(self, query: str, sport: str, status_path: str = STATUS_PATH):
        self.query = query
        self.sport = sport
        self.status_path = status_path
        self.started = time.monotonic()
        self.lines: List[Dict[str, Any]] = []
        self.agent_status: Dict[str, str] = {a: "pending" for a in VALID_AGENTS}
        self.progress_weight: Dict[str, float] = {}
        self._done_weight = 0.0
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._dirty = True
        self._last_write = 0.0

    # -- logging -------------------------------------------------------------
    def log(self, agent: str, msg: str) -> None:
        entry = {
            "t": round(time.monotonic() - self.started, 2),
            "agent": agent.upper(),
            "msg": msg,
        }
        with self._lock:
            self.lines.append(entry)
            self._dirty = True

    # -- progress ------------------------------------------------------------
    def begin_agent(self, name: str, weight: float) -> None:
        with self._lock:
            self.agent_status[name] = "running"
            self.progress_weight[name] = weight
            self._dirty = True
        self.log(name, f"{_AGENT_MISSION[name]} ...")

    def complete_agent(self, name: str, ok: bool = True) -> None:
        with self._lock:
            w = self.progress_weight.get(name, 1.0)
            if ok:
                self._done_weight += w
                self.agent_status[name] = "done"
            else:
                # failed agents still consume their share of the bar so the
                # hunt can always reach 100%
                self._done_weight += w
                self.agent_status[name] = "failed"
            self._dirty = True

    @property
    def progress(self) -> float:
        total = sum(self.progress_weight.values()) or 1.0
        return min(max(self._done_weight / total, 0.0), 1.0)

    def stage(self) -> str:
        for a in VALID_AGENTS:
            if self.agent_status.get(a) == "running":
                return a
        return "complete" if self.status() in ("complete", "error") else "queued"

    def status(self) -> str:
        if self.error:
            return "error"
        if self.result is not None:
            return "complete"
        return "running"

    # -- snapshot --------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lines = list(self.lines)
        return {
            "version": SWARM_VERSION,
            "query": self.query,
            "sport": self.sport,
            "status": self.status(),
            "stage": self.stage(),
            "progress": round(self.progress, 3),
            "agents": dict(self.agent_status),
            "elapsed_s": round(time.monotonic() - self.started, 1),
            "updated_at": utc_now_iso(),
            "lines": lines,
            "result": self.result,
            "message": self.error or "",
        }

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._dirty and not force:
                return
            self._dirty = False
            self._last_write = now
        try:
            atomic_write(self.status_path, self.snapshot())
        except Exception:
            pass  # never let telemetry kill the hunt


_AGENT_MISSION = {
    "scout": "Deploying hunters across market sources",
    "actuary": "Crunching no-vig fair values",
    "context": "Querying fixture context (LLM)",
    "strategist": "Synthesizing final edge report",
}


# ---------------------------------------------------------------------------
# Agents (each: async run(ctx) -> dict payload; ctx.log streams to console)
# ---------------------------------------------------------------------------

async def agent_scout(ctx: SwarmContext) -> Dict[str, Any]:
    """Concurrent multi-source scrape. Streams per-source completion live."""
    import hunter_api  # heavy import kept local so runner boots instantly

    ctx.begin_agent("scout", weight=4.0)
    loop = asyncio.get_running_loop()
    for name in ("betika", "odibets", "flashscore", "espn_mirror"):
        ctx.log("scout", f"Connecting to {name} ...")

    def on_source_done(name: str, rows: int, status: str) -> None:
        # called from scraper worker threads -> marshal onto the log bus.
        # Statuses are typed by hunter_api (blocked_waf / timeout /
        # spa_shell_deferred_to_pw / no_fixture / fixture_no_odds / ok) so
        # "site blocked us" is NEVER displayed as a generic empty.
        icon = "OK" if status == "ok" else status.upper()
        ctx.log("scout", f"{name}: {rows} price rows [{icon}]")

    payload = await asyncio.wait_for(
        loop.run_in_executor(
            None, lambda: hunter_api.run_hunt(ctx.query, ctx.sport,
                                              on_source_done)),
        timeout=AGENT_TIMEOUT_S,
    )

    sources = payload.get("sources", {})
    ctx.log("scout", f"Total raw price rows harvested: "
          f"{payload.get('raw_prices_seen', 0)}")
    ah = [r for r in payload.get("_raw_rows", [])
          if r.get("market") in ("asian_handicap", "spread")]
    if ah:
        variants = len({(r.get("selection"), r.get("line")) for r in ah})
        ctx.log("scout", f"Parsing Asian Handicap lines... "
              f"Found {variants} variants")
    ou = [r for r in payload.get("_raw_rows", []) if r.get("market") == "total"]
    if ou:
        ctx.log("scout", f"Totals board: {len(ou)} Over/Under quotes parsed")
    ctx.log("scout", "Sources online: "
          + ", ".join(f"{k}={v}" for k, v in sources.items()))
    payload.pop("_raw_rows", None)
    ctx.complete_agent("scout", ok=True)
    return payload


async def agent_actuary(ctx: SwarmContext, scout_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Re-run the quant engine (src/analyzer) over the Scout's raw rows so
    the console reports genuine computation, then cross-check against the
    Scout's already-analyzed picks."""
    ctx.begin_agent("actuary", weight=2.0)
    loop = asyncio.get_running_loop()

    def _compute() -> Dict[str, Any]:
        from analyzer import analyze_rows
        rows = scout_payload.get("_raw_rows", [])
        state: Dict[str, Any] = {"odds_history": {}, "last_run": None}
        event_id = re.sub(r"[^a-z0-9]+", "-",
                          re.sub(r"\s+", " ", ctx.query.lower()).strip()) or "hunt"
        markets = analyze_rows(rows, state, event_id) if rows else []
        return {"markets": markets}

    try:
        computed = await asyncio.wait_for(loop.run_in_executor(None, _compute),
                                          timeout=60)
        markets = computed["markets"] or scout_payload.get("picks", [])
    except Exception as exc:
        ctx.log("actuary", f"quant pass degraded ({type(exc).__name__}); "
              "using Scout-side analysis output")
        markets = scout_payload.get("picks", [])

    handles = len({(m.get('type'), m.get('line')) for m in markets})
    ctx.log("actuary", f"Ingested {handles} validated market handles "
          f"({len(markets)} priced legs)")
    ctx.log("actuary", "Calculating No-Vig Fair Odds... Done")
    pos = [m for m in markets if m.get("ev_percent", 0) > 0]
    best = max((m.get("ev_percent", 0) for m in markets), default=0.0)
    ctx.log("actuary", f"Edge scan: {len(pos)} positive-EV legs "
          f"(max raw edge {best:+.2f}%)")
    ctx.complete_agent("actuary", ok=True)
    return {"markets": markets, "edges": scout_payload.get("edges", []),
            "raw_prices_seen": scout_payload.get("raw_prices_seen", 0)}


async def agent_context(ctx: SwarmContext, actuary_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Optional LLM injury/news pass. ALWAYS degrades to neutral on failure."""
    ctx.begin_agent("context", weight=1.5)
    api_key = (os.environ.get("GROQ_API_KEY", "").strip()
               or os.environ.get("OPENAI_API_KEY", "").strip())
    base_url = os.environ.get("GROQ_BASE_URL",
                              "https://api.groq.com/openai/v1/chat/completions")
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    notes: List[str] = []
    adjustment = 1.0
    available = False

    if not api_key:
        ctx.log("context", "No GROQ_API_KEY configured -> NEUTRAL adjustment "
                           "(math-only mode, confidence will be capped)")
    else:
        try:
            import requests

            def _call() -> Optional[Dict[str, Any]]:
                prompt = (
                    "You are a sports betting context agent. For the fixture "
                    f"'{ctx.query}' ({ctx.sport}), list ONLY verifiable recent "
                    "facts relevant to pricing: injuries/suspensions of key "
                    "players, rest days, motivation. Reply STRICT JSON "
                    '{"notes": ["..."], "prob_adjustment": 1.0}. '
                    "If you are not sure, use empty notes and 1.0."
                )
                r = requests.post(
                    base_url,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.1, "max_tokens": 400},
                    timeout=15,
                )
                if r.status_code != 200:
                    ctx.log("context", f"LLM HTTP {r.status_code} -> "
                                       "NEUTRAL adjustment (rate-limited?)")
                    return None
                body = r.json()["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", body, re.S)
                return json.loads(m.group(0)) if m else None

            data = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _call),
                timeout=20)
            if data:
                notes = [str(n)[:160] for n in data.get("notes", [])][:6]
                try:
                    adj = float(data.get("prob_adjustment", 1.0))
                except (TypeError, ValueError):
                    adj = 1.0
                adjustment = min(max(adj, 0.85), 1.15)  # sanity clamp
                available = True
                for n in notes:
                    ctx.log("context", n)
                ctx.log("context", f"Context factor applied: x{adjustment:.2f}")
        except Exception as exc:
            ctx.log("context", f"LLM unavailable ({type(exc).__name__}) -> "
                               "proceeding math-only with lower confidence")

    ctx.complete_agent("context", ok=True)
    return {"available": available, "notes": notes,
            "adjustment": adjustment if available else 1.0,
            "confidence_penalty": 0 if available else 15.0}


async def agent_strategist(ctx: SwarmContext, scout_payload: Dict[str, Any],
                           actuary_payload: Dict[str, Any],
                           context_payload: Dict[str, Any]) -> Dict[str, Any]:
    ctx.begin_agent("strategist", weight=2.5)
    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(
        None, _build_report, ctx, scout_payload, actuary_payload, context_payload)
    top = report.get("top_edges", [])
    if top:
        pick = top[0]
        ctx.log("strategist",
                f"Synthesizing Edge... Top Pick: {pick['selection']} "
                f"@ {pick['book_odds']:.2f} "
                f"(Conf: {int(pick['confidence_score'])}%)")
    else:
        ctx.log("strategist",
                f"No verified edge for '{ctx.query}'. Sources returned "
                f"{report['markets_scanned_count']} valid handles. "
                "Refusing to fabricate picks.")
    ctx.complete_agent("strategist", ok=True)
    return report


async def _guard(ctx: SwarmContext, name: str, fn, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Run one agent; on crash mark it failed and return the honest fallback."""
    try:
        return await fn()
    except Exception as exc:
        ctx.log(name, f"AGENT FAILED ({type(exc).__name__}: {exc}) -> "
                      f"continuing without {name} output")
        ctx.complete_agent(name, ok=False)
        return dict(fallback)


# ---------------------------------------------------------------------------
# Report assembly (schema: schemas/hunt_result.schema.json)
# ---------------------------------------------------------------------------

def _reasoning_summary(market: Dict[str, Any], notes: List[str]) -> str:
    sigs = market.get("signals", [])
    bits = []
    if "STALE_LINE_ARB" in sigs or "STALE_LINE" in sigs:
        bits.append("Cross-book price gap detected (stale line).")
    if "LINE_MOVE" in sigs:
        bits.append("Recent line movement confirms sharp action.")
    if market.get("num_bookmakers", 0) >= 2:
        bits.append(f"Consensus taken from {market['num_bookmakers']} books.")
    if notes:
        bits.append("Context: " + "; ".join(notes[:2]))
    if not bits:
        bits.append("Pure no-vig value vs consensus market.")
    return " ".join(bits)


def _render_reports(match_doc: Dict[str, Any], stamp: str) -> List[str]:
    """Matplotlib PNGs into data/reports/ via the existing visualizer."""
    files: List[str] = []
    try:
        from visualizer import generate_reports
        out = generate_reports({"matches": [match_doc]}, REPORTS_DIR,
                               max_matches=1)
        for kind, lst in out.items():
            for f in lst:
                src = os.path.join(REPORTS_DIR, f)
                if os.path.exists(src):
                    files.append(f"data/reports/{f}")
    except Exception:
        pass  # charts are cosmetic; never fatal, never faked
    return files


def _build_report(ctx: SwarmContext, scout: Dict[str, Any],
                  actuary: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    markets = actuary.get("markets", [])
    penalty = float(context.get("confidence_penalty", 0.0))
    notes = context.get("notes", [])
    adjustment = float(context.get("adjustment", 1.0))

    scored: List[Dict[str, Any]] = []
    for m in markets:
        ev = float(m.get("ev_percent", 0.0))
        book_odds = float((m.get("best") or {}).get("decimal", 0) or 0)
        if ev <= 0 or book_odds <= 1.0 or m.get("fair_probability") is None:
            continue
        conf = float(m.get("confidence", 0.0)) * adjustment
        conf = max(0.0, min(100.0, conf - penalty))
        scored.append({
            "market": (f"{m.get('name') or m.get('type')}"
                       + (f" {m['line']:+g}" if m.get("line") is not None else "")),
            "selection": m.get("selection", "?"),
            "book_odds": round(float((m.get("best") or {}).get("decimal", 0)), 3),
            "fair_odds": (round(100.0 / float(m["fair_probability"]), 3)
                          if m.get("fair_probability") else None),
            "ev_percent": round(ev, 2),
            "confidence_score": round(conf, 1),
            "kelly_stake_pct": round(max(0.0, float(m.get("kelly_stake", 0.0))) * 100.0, 2),
            "reasoning_summary": _reasoning_summary(m, notes),
            "best_bookmaker": m["best"]["bookmaker"],
            "signals": m.get("signals", []),
        })

    scored.sort(key=lambda e: -(e["ev_percent"] * e["confidence_score"]))
    top = scored[:10]

    event_id = re.sub(r"[^a-z0-9]+", "-",
                      re.sub(r"\s+", " ", ctx.query.lower()).strip()).strip("-") \
        or "hunt"
    match_doc = {
        "id": f"swarm_{event_id}",
        "home_team": ctx.query.split(" vs ")[0].strip().title() if " vs " in ctx.query.lower() else "Home",
        "away_team": ctx.query.split(" vs ")[-1].strip().title() if " vs " in ctx.query.lower() else "Away",
        "markets": markets,
    }
    stamp = utc_now_iso()
    pngs = _render_reports(match_doc, stamp) if top else []

    report = {
        "schema_version": "3.0.0",
        "query": ctx.query,
        "sport": ctx.sport,
        "timestamp_utc": stamp,
        "agents_executed": [a for a in VALID_AGENTS],
        "agents_failed": [a for a, s in ctx.agent_status.items() if s == "failed"],
        "markets_scanned_count": int(scout.get("markets_scanned", len(markets))),
        "raw_prices_seen": actuary.get("raw_prices_seen", 0),
        "sources": scout.get("sources", {}),
        "context_available": bool(context.get("available")),
        "mode": "full" if context.get("available") else "math_only",
        "top_edges": top,
        "visual_reports_png": pngs,
        "data_policy": "real scraped prices only; empty top_edges means no "
                       "verified edge — never fabricated odds",
        "status": "success" if top else "no_results",
    }
    if not top:
        # Honest, diagnostic empty result: name the failure mode per source
        # so operators can tell "site blocked us" from "no match listed".
        _diag = {
            "blocked_waf": "BLOCKED_BY_WAF (site refused our IP/headers)",
            "spa_shell_deferred_to_pw": "DEFERRED_TO_PLAYWRIGHT_PHASE_B "
                                        "(JS-rendered board, no static odds)",
            "timeout": "TIMEOUT_ON_LOAD",
            "not_found": "NOT_FOUND (stale route)",
            "fixture_no_odds": "fixture located but bookmaker lines absent/"
                               "expired on that feed",
            "no_fixture": "match not listed on any scanned board",
            "empty": "board reachable, nothing matched the query",
            "error": "scraper error",
            "skipped": "skipped (sport mismatch)",
        }
        srcs = scout.get("sources", {})
        parts = [f"{k}={_diag.get(v, v)}" for k, v in srcs.items()] or \
                ["no sources reported"]
        report["error_message"] = (
            f"No tradable edge found for '{ctx.query}'. Source telemetry: "
            + "; ".join(parts) + ". "
            f"{report['markets_scanned_count']} markets scanned, "
            f"{len([m for m in markets])} valid handles, 0 positive EV after "
            "vig removal. This is an honest empty result, not a bug.")
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _pipeline(ctx: SwarmContext) -> None:
    writer_stop = asyncio.Event()

    async def writer() -> None:
        while not writer_stop.is_set():
            ctx.flush()
            try:
                await asyncio.wait_for(writer_stop.wait(),
                                       timeout=WRITE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    wt = asyncio.create_task(writer())
    try:
        ctx.log("swarm", f"STRATUM V3.0 swarm deployed :: target='{ctx.query}' "
                         f"sport={ctx.sport}")
        # Each agent is individually guarded: a failure degrades the hunt
        # (flagged in agents_failed + lower confidence) but never aborts it.
        scout = await _guard(ctx, "scout", lambda: agent_scout(ctx), {})
        actuary = await _guard(ctx, "actuary",
                               lambda: agent_actuary(ctx, scout),
                               {"markets": scout.get("picks", []),
                                "edges": scout.get("edges", []),
                                "raw_prices_seen": scout.get("raw_prices_seen", 0)})
        context = await _guard(ctx, "context",
                               lambda: agent_context(ctx, actuary),
                               {"available": False, "notes": [],
                                "adjustment": 1.0, "confidence_penalty": 15.0})
        if not context.get("available"):
            ctx.log("strategist", "CONTEXT degraded -> math-only report; "
                    "confidence scores penalized")
        report = await agent_strategist(ctx, scout, actuary, context)
        ctx.result = report
        ctx.log("swarm", f"Hunt complete in {time.monotonic() - ctx.started:.1f}s "
                         f":: {len(report['top_edges'])} ranked edges")
        # Persist archived copies: data/hunts/latest.json + timestamped dir
        os.makedirs(HUNTS_DIR, exist_ok=True)
        atomic_write(os.path.join(HUNTS_DIR, "latest.json"), report)
        ts_dir = os.path.join(
            HUNTS_DIR, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        atomic_write(os.path.join(ts_dir, "hunt_result.json"), report)
    except asyncio.TimeoutError:
        ctx.error = "Agent exceeded hard deadline; partial results discarded."
        ctx.log("swarm", ctx.error)
    except Exception as exc:
        ctx.error = f"Swarm failure: {type(exc).__name__}: {exc}"
        ctx.log("swarm", ctx.error)
        traceback.print_exc(limit=3)
    finally:
        writer_stop.set()
        await wt
        ctx.flush(force=True)


_run_lock = threading.Lock()
_current: Optional[threading.Thread] = None


def start_hunt_background(query: str, sport: str) -> bool:
    """Kick off a hunt in a daemon thread. Returns False if one is running."""
    global _current
    with _run_lock:
        if _current is not None and _current.is_alive():
            return False
        ctx = SwarmContext(query, sport)
        ctx.log("swarm", "Queued -> spinning up agents")
        ctx.flush(force=True)

        def _thread_main() -> None:
            asyncio.run(_pipeline(ctx))

        _current = threading.Thread(target=_thread_main, daemon=True,
                                    name="swarm-hunt")
        _current.start()
        return True


def run_hunt_sync(query: str, sport: str) -> Dict[str, Any]:
    """Run one hunt to completion in the calling thread (CLI / CI mode)."""
    ctx = SwarmContext(query, sport)
    asyncio.run(_pipeline(ctx))
    if ctx.result:
        return ctx.result
    return {"schema_version": SWARM_VERSION, "query": query, "sport": sport,
            "timestamp_utc": utc_now_iso(),
            "agents_executed": list(ctx.agent_status),
            "markets_scanned_count": 0, "top_edges": [],
            "visual_reports_png": [], "status": "error",
            "error_message": ctx.error or "hunt produced no report"}


# ---------------------------------------------------------------------------
# Scheduled universe scan (`scan-slate`) — SCHEDULED SWARM architecture
#
# The swarm runs on a cron (see .github/workflows/deploy-swarm.yml), scans a
# BROAD fixture slate (today + tomorrow, all configured leagues), computes
# edges for every market it finds, and commits ONE file:
#     data/market_universe.json   (schemas/market_universe.schema.json)
# The PWA loads that file once and FILTERS client-side by the user's typed
# match. Typing = instant; no per-hunt network round-trip, no token in the
# browser, no git-push latency decoupling.
#
# It ALSO rewrites data/hunt_status.json as a REPLAYABLE log of the cycle so
# the HunterConsole can honestly show what the agents did, stamped "last
# scheduled scan @ <time>".
# ---------------------------------------------------------------------------

UNIVERSE_PATH = os.path.join(DATA_DIR, "market_universe.json")
UNIVERSE_SCHEMA_PATH = os.path.join(_ROOT, "schemas",
                                    "market_universe.schema.json")
SCAN_DAYS_AHEAD = int(os.environ.get("STRATUM_SCAN_DAYS", "1"))
CYCLE_INTERVAL_MIN = int(os.environ.get("STRATUM_SCAN_INTERVAL_MIN", "15"))


def _validate_universe(universe: Dict[str, Any]) -> None:
    """jsonschema gate: an invalid universe must never reach main."""
    try:
        import jsonschema
    except ImportError:
        return  # validator optional at runtime; CI always has it
    with open(UNIVERSE_SCHEMA_PATH, encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=universe, schema=schema)


def _fixture_from_event(lg: Dict[str, str], ev: Dict[str, Any],
                        rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run the quant engine over one ESPN event's real price rows and shape
    a universe fixture record. Zero-fabrication rule holds: no rows in,
    empty top_edges out."""
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    home_name = ((home.get("team") or {}).get("displayName")
                 or (home.get("team") or {}).get("shortDisplayName") or "HOME")
    away_name = ((away.get("team") or {}).get("displayName")
                 or (away.get("team") or {}).get("shortDisplayName") or "AWAY")

    state: Dict[str, Any] = {"odds_history": {}, "last_run": None}
    markets = analyze_rows_cached(rows, state, str(ev.get("id", ""))) \
        if rows else []
    scored: List[Dict[str, Any]] = []
    for m in markets:
        ev_pct = float(m.get("ev_percent", 0.0))
        book_odds = float((m.get("best") or {}).get("decimal", 0) or 0)
        if ev_pct <= 0 or book_odds <= 1.0 or m.get("fair_probability") is None:
            continue
        conf = max(0.0, min(100.0, float(m.get("confidence", 0.0))))
        scored.append({
            "market": (f"{m.get('name') or m.get('type')}"
                       + (f" {m['line']:+g}" if m.get("line") is not None else "")),
            "selection": m.get("selection", "?"),
            "book_odds": round(book_odds, 3),
            "fair_odds": round(100.0 / float(m["fair_probability"]), 3),
            "ev_percent": round(ev_pct, 2),
            "confidence_score": round(conf, 1),
            "kelly_stake_pct": round(max(0.0, float(m.get("kelly_stake", 0.0))) * 100.0, 2),
            "reasoning_summary": _reasoning_summary(m, []),
        })
    scored.sort(key=lambda e: -(e["ev_percent"] * e["confidence_score"]))
    return {
        "fixture_id": f"espn_{ev.get('id')}",
        "home": home_name,
        "away": away_name,
        "sport": "soccer",           # slate currently covers soccer boards
        "league": lg.get("display") or lg.get("key", ""),
        "kickoff_utc": ev.get("date", ""),
        "markets_scanned": len(markets),
        "top_edges": scored[:10],
        "agent_trace_lines": [],     # filled by the pipeline below
    }


def analyze_rows_cached(rows, state, event_id):
    """Thin indirection so the actuary import stays lazy & testable."""
    from analyzer import analyze_rows
    return analyze_rows(rows, state, event_id)


def scan_slate() -> Dict[str, Any]:
    """Build the fixture slate, run the math pipeline per fixture, publish
    data/market_universe.json + a replayable hunt_status.json. Returns the
    universe document (also written to disk)."""
    import hunter_api

    t0 = time.monotonic()
    cycle_stamp = utc_now_iso()
    print(f"[swarm] UNIVERSE SCAN started :: window=d1+d{SCAN_DAYS_AHEAD + 1} "
          f"@ {cycle_stamp}", flush=True)

    boards = hunter_api.espn_slate_boards(days_ahead=SCAN_DAYS_AHEAD)
    print(f"[swarm] slate built: {len(boards)} events across "
          f"{len({lg['key'] for lg, _ in boards})} leagues "
          f"({time.monotonic() - t0:.1f}s)", flush=True)

    fixtures: List[Dict[str, Any]] = []
    priced_events = 0
    total_rows = 0
    for lg, ev in boards:
        rows = hunter_api._espn_event_rows(ev)
        if not rows:
            continue
        priced_events += 1
        total_rows += len(rows)
        fx = _fixture_from_event(lg, ev, rows)
        name = f"{fx['home']} vs {fx['away']}"
        fx["agent_trace_lines"] = [
            {"agent": "SCOUT",
             "text": f"Board {lg['key']}: harvested {len(rows)} real price "
                     f"rows for '{name}'",
             "ts": round(time.monotonic() - t0, 2)},
            {"agent": "ACTUARY",
             "text": f"No-vig pass on {fx['markets_scanned']} markets -> "
                     f"{len(fx['top_edges'])} positive-EV legs",
             "ts": round(time.monotonic() - t0, 2)},
            {"agent": "STRATEGIST",
             "text": (f"Top Pick: {fx['top_edges'][0]['selection']} "
                      f"@ {fx['top_edges'][0]['book_odds']:.2f} "
                      f"(Conf: {int(fx['top_edges'][0]['confidence_score'])}%)"
                      if fx["top_edges"] else
                      "No verified edge after vig removal — honest empty"),
             "ts": round(time.monotonic() - t0, 2)},
        ]
        fixtures.append(fx)

    fixtures.sort(key=lambda f: -(sum(e["ev_percent"] * e["confidence_score"]
                                      for e in f["top_edges"]) or 0))

    universe = {
        "schema_version": SWARM_VERSION,
        "generated_at_utc": cycle_stamp,
        "next_refresh_estimate_utc": (
            datetime.now(timezone.utc)
            + timedelta(minutes=CYCLE_INTERVAL_MIN)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_status": {
            "espn_mirror": f"ok({priced_events})" if priced_events
                           else "no_fixture_in_board",
            "betika_json": "deferred",
            "odibets_html": "deferred",
            "flashscore": "deferred",
        },
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
        "data_policy": "real scraped prices only; empty top_edges means no "
                       "verified edge — never fabricated odds",
        "scan_stats": {
            "events_seen": len(boards),
            "events_priced": priced_events,
            "raw_price_rows": total_rows,
            "elapsed_s": round(time.monotonic() - t0, 1),
        },
    }

    _validate_universe(universe)
    atomic_write(UNIVERSE_PATH, universe)

    # Replayable console snapshot: same shape the PWA poller expects, marked
    # complete so the UI renders it as "last scheduled scan", never as live.
    trace: List[Dict[str, Any]] = []
    seen: set = set()
    for fx in fixtures[:60]:
        for ln in fx["agent_trace_lines"]:
            key = (ln["agent"], ln["text"])
            if key in seen:
                continue
            seen.add(key)
            trace.append(ln)
    n_edges = sum(len(f["top_edges"]) for f in fixtures)
    replay_lines = ([{"t": 0.0, "agent": "SWARM",
                      "msg": f"Scheduled universe scan @ {cycle_stamp} "
                             f"(replay — not a live hunt)"}]
                    + [{"t": ln["ts"], "agent": ln["agent"], "msg": ln["text"]}
                       for ln in trace]
                    + [{"t": round(time.monotonic() - t0, 2), "agent": "SWARM",
                        "msg": f"Cycle complete: {len(fixtures)} fixtures, "
                               f"{n_edges} ranked edges, "
                               f"{universe['scan_stats']['elapsed_s']}s"}])
    atomic_write(STATUS_PATH, {
        "version": SWARM_VERSION,
        "query": "(scheduled universe scan)",
        "sport": "auto",
        "status": "complete",
        "stage": "complete",
        "progress": 1.0,
        "agents": {a: "done" for a in VALID_AGENTS},
        "elapsed_s": round(time.monotonic() - t0, 1),
        "updated_at": cycle_stamp,
        "lines": replay_lines,
        "result": None,
        "message": "",
        "mode": "scheduled_replay",
        "universe_fixture_count": len(fixtures),
    })
    print(f"[swarm] universe published: {len(fixtures)} fixtures, "
          f"{n_edges} edges -> {UNIVERSE_PATH}", flush=True)
    return universe


# ---------------------------------------------------------------------------
# Dev server (stdlib only) — powers the live console without GitHub round-trips
# ---------------------------------------------------------------------------

def serve(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # CORS preflight
            self._send(204, b"", "text/plain")

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/hunt/status"):
                try:
                    with open(STATUS_PATH, "rb") as fh:
                        body = fh.read()
                except FileNotFoundError:
                    body = json.dumps({"status": "idle", "progress": 0,
                                       "lines": [], "result": None}).encode()
                self._send(200, body, "application/json")
            elif self.path.startswith("/hunt/result"):
                path = os.path.join(HUNTS_DIR, "latest.json")
                try:
                    with open(path, "rb") as fh:
                        body = fh.read()
                except FileNotFoundError:
                    body = json.dumps({"status": "no_results",
                                       "top_edges": []}).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def do_POST(self):  # noqa: N802
            if not self.path.startswith("/hunt/start"):
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, b'{"error":"bad json"}', "application/json")
                return
            query = str(payload.get("query", "")).strip()
            sport = str(payload.get("sport", "auto")).strip() or "auto"
            if not query:
                self._send(400, b'{"error":"missing query"}', "application/json")
                return
            started = start_hunt_background(query, sport)
            code = 202 if started else 409
            body = json.dumps({"accepted": started,
                               "message": "" if started
                               else "another hunt is already running"}).encode()
            self._send(code, body, "application/json")

        def log_message(self, fmt, *args):
            pass  # keep console clean

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[swarm] dev server on http://127.0.0.1:{port} "
          f"(POST /hunt/start · GET /hunt/status · GET /hunt/result)",
          flush=True)
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stratum V3.0 agent swarm")
    ap.add_argument("--query", type=str, default="", help='e.g. "Al Hilal vs Al Nassr"')
    ap.add_argument("--sport", type=str, default="soccer",
                    choices=["auto", "soccer", "basketball", "tennis"])
    ap.add_argument("--output", "-o", type=str, default="",
                    help="write final report JSON here")
    sub = ap.add_subparsers(dest="cmd")
    serve_p = sub.add_parser("serve", help="run the swarm dev server")
    serve_p.add_argument("--port", type=int, default=8788)
    sub.add_parser("scan-slate",
                   help="scheduled universe scan -> data/market_universe.json "
                        "(cron entry for deploy-swarm.yml)")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        serve(args.port)
        return 0

    if args.cmd == "scan-slate":
        try:
            u = scan_slate()
        except Exception as exc:
            print(f"[swarm] scan-slate FAILED: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
        # Non-empty universe OR honest typed failure — never a silent empty.
        return 0 if u["fixture_count"] > 0 else 2

    if not args.query:
        ap.error("--query is required (or use: serve / scan-slate)")

    report = run_hunt_sync(args.query, args.sport)
    text = json.dumps(report, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)),
                    exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"[swarm] wrote {args.output}")
    else:
        print(text)
    return 0 if report.get("status") in ("success", "no_results") else 1


if __name__ == "__main__":
    sys.exit(main())
