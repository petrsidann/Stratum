"""Stratum v0.3 — Production mobile-first PWA shell (Phase 5).

Native-app architecture on Streamlit:
  * App chrome   : ui_theme.inject_css() restyles every widget; default
                   header/menu/footer are hidden. Dark fintech palette,
                   Inter labels + Roboto Mono data, zero emojis.
  * Navigation   : fixed bottom tab bar [SCAN] [AUDIT] [PORTFOLIO]
                   [ALERTS] [SETTINGS]. Tabs navigate via ?view=<key> links
                   (works under Streamlit's iframe CSP where JS injection does
                   not); main.py syncs the query param into session_state so
                   all other widgets keep their state across navigation.
  * Deep engine  : Phase 5 adds the 200-Market Expander (player props /
                   derivatives), the Contextual Reasoning Layer (LLM "why"
                   text per card), the Immutable Audit Ledger (every scan is
                   sealed in SQLite with UUID + hashes) and the Confidence
                   Scorer (Book Agreement > Edge > Recency, capped at 95).
  * Live layer   : src.live_watcher.SentinelThread runs in the background at
                   a 30s cadence, diffs odds against SQLite snapshots and
                   pushes phone alerts through a Discord/Telegram webhook.
                   Lifecycle hooks start it on first render and stop it on
                   interpreter exit.
  * Honesty      : unchanged framework rules — all math lives in
                   quant_engine, sample boards are loudly labeled, unknown
                   prices stay unknown. The UI never invents numbers.

Run: streamlit run main.py
"""

from __future__ import annotations

import atexit
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stratum.app")

try:
    import streamlit as st
    _HAVE_STREAMLIT = True
except ImportError:  # pragma: no cover - allows `python main.py` headless
    _HAVE_STREAMLIT = False

import pandas as pd
import plotly.graph_objects as go

from config import DATABASE_PATH, GOOGLE_API_KEY, GROQ_API_KEY, get_env
from src import report, scraper, ui_theme
from src.audit_ledger import (
    ImmutableRecordError, get_scan, ledger_metrics, list_scans, log_scan,
    market_win_rate, resolve_scan, sha256_of,
)
from src.clv_auditor import (
    get_performance_report, list_bets, record_bet, settle_bet, update_closing_line,
)
from src.confidence_scorer import (
    DEFAULT_MIN_CONFIDENCE, confidence_band, calculate_confidence, passes_threshold,
)
from src.database import init_db
from src import live_watcher
from src.market_expander import MarketExpander
from src.market_scanner import MARKET_ML, MARKET_PROPS, MarketScanner
from src.quant_engine import (
    american_to_decimal,
    arbitrage_pct,
    ev_pct,
    implied_probability,
    kelly_criterion,
    remove_vig_two_way,
    vig_pct_two_way,
)
from src.reasoning_engine import FALLBACK_INSIGHT, generate_insight
from src.signal_detector import find_arbitrage_opportunities, scan_signals_for_match
from src.ui_theme import COLORS, fmt_odds, money, pct, signed

SPORTS = ["NFL", "NBA", "MLB", "Soccer", "Tennis"]
VIEWS = ["scan", "audit", "portfolio", "alerts", "settings"]
FILTERS = ["ALL", "STEAM", "ARB", "PROPS"]
DEFAULT_WEBHOOK = get_env("STRATUM_WEBHOOK")

# Process-wide Phase 5 singletons: the expander's TTL cache must survive
# Streamlit reruns, so it lives at module scope (same pattern as Sentinel).
_EXPANDER = MarketExpander()

# ---------------------------------------------------------------------------
# Pure analysis core (no Streamlit, no network) — unit-tested in
# tests/test_main_flow.py. All arithmetic happens HERE, inside quant_engine.
# ---------------------------------------------------------------------------

def analyze_market(
    ml_home: float,
    ml_away: float,
    model_prob_home=None,
    bankroll: float = 1000.0,
    kelly_fraction: float = 0.25,
) -> dict:
    """Turn FINAL numeric prices into fair probs, vig, edge, EV and stake."""
    imp_home = implied_probability(ml_home)
    imp_away = implied_probability(ml_away)
    fair_home, fair_away = remove_vig_two_way(ml_home, ml_away)
    vig_pct = (imp_home + imp_away - 1.0) * 100.0

    result = {
        "implied": {"home": imp_home, "away": imp_away},
        "fair": {"home": fair_home, "away": fair_away},
        "vig_pct": vig_pct,
        "kelly": None,
    }

    if model_prob_home is not None and 0.0 < float(model_prob_home) < 1.0:
        p = float(model_prob_home)
        dec_home = american_to_decimal(ml_home)
        edge_home = p * dec_home - 1.0                        # EV per $1 staked
        ev_home = (p * (dec_home - 1.0) - (1.0 - p)) * 100.0  # EV% on a $1 bet
        quarter_kelly = kelly_criterion(p, dec_home, fraction=kelly_fraction)
        stake = max(0.0, quarter_kelly) * float(bankroll)
        result["kelly"] = {
            "model_prob_home": p,
            "edge_home_pct": edge_home * 100.0,
            "ev_home_pct": ev_home,
            "quarter_kelly_frac": quarter_kelly,
            "stake": stake,
            "bankroll": float(bankroll),
        }
    return result


def fair_american(prob: float):
    """Convert a no-vig fair probability to American odds (display only)."""
    if prob is None or not 0.0 < prob < 1.0:
        return None
    dec = 1.0 / prob
    amer = round((dec - 1.0) * 100.0) if dec >= 2.0 else round(-100.0 / (dec - 1.0))
    if -100 < amer < 100:  # clamp into the legal American range around even
        amer = 100 if amer >= 0 else -100
    return amer


def required_prices_missing(parsed) -> bool:
    """True when we lack BOTH a full moneyline pair AND any side/total price."""
    if not parsed:
        return True
    has_ml = parsed.get("ml_home_american") is not None and parsed.get("ml_away_american") is not None
    has_side = parsed.get("spread_home") is not None or parsed.get("total_line") is not None
    return not (has_ml or has_side)


# ---------------------------------------------------------------------------
# Phase 3 pure pipeline helpers (no Streamlit, no network) — unit-tested in
# tests/test_signals.py. All arithmetic routes through quant_engine.
# ---------------------------------------------------------------------------

def rank_value_spots(scan_rows, fair_prob_by_selection=None):
    """Sort scanned quotes into EV-ranked value spots using quant_engine only.

    ``fair_prob_by_selection`` maps selection -> our estimate of true win p
    (e.g. the no-vig fair prob). For each priced quote we compute
    ev_pct(p, odds); rows without a known probability get ev=None (shown as
    'Unknown', never guessed). Returns top rows sorted by EV desc.
    """
    fair_prob_by_selection = fair_prob_by_selection or {}
    out = []
    for row in scan_rows or []:
        p = fair_prob_by_selection.get(row.get("selection"))
        ev = None
        vig = None
        if p is not None and row.get("american_odds"):
            try:
                ev = round(ev_pct(float(p), float(row["american_odds"])), 2)
            except ValueError:
                ev = None
        if row.get("market_type") == "ML" and row.get("selection") in ("Home", "Away"):
            other = "Away" if row["selection"] == "Home" else "Home"
            opp = fair_prob_by_selection.get(f"{other}_odds")
            if opp is not None and row.get("american_odds"):
                try:
                    vig = round(vig_pct_two_way(float(row["american_odds"]), float(opp)), 2)
                except ValueError:
                    vig = None
        out.append({**row, "ev_pct": ev, "book_vig_pct": vig})
    out.sort(key=lambda r: (r["ev_pct"] is None, -(r["ev_pct"] or 0)))
    return out


def build_fair_probs(ml_home, ml_away):
    """No-vig fair probabilities + American-odds lookup map for the scanner.

    Returns (fair_dict, prob_map) where prob_map feeds rank_value_spots:
    Home/Away -> fair probs; '<side>_odds' carries the opposite price so the
    helper can compute book vig via quant_engine.
    """
    fair_home, fair_away = remove_vig_two_way(ml_home, ml_away)
    prob_map = {"Home": fair_home, "Away": fair_away,
                "Home_odds": ml_home, "Away_odds": ml_away}
    return {"home": fair_home, "away": fair_away}, prob_map


def gather_live_context(match: str, sport: str, lat, lon, brain):
    """Fetch context + weather + parsed prices. Returns (context, weather, parsed, warnings).

    Never raises: any failure degrades to empty text / None prices + warnings,
    so the caller can fall through to the manual-entry form.
    """
    warnings: list[str] = []
    context = scraper.fetch_match_context(f"{match} {sport}")
    weather = scraper.fetch_weather(lat, lon) if (lat is not None and lon is not None) else None
    if weather is None and lat is not None and lon is not None:
        warnings.append("Weather unavailable (Open-Meteo unreachable).")

    parsed = None
    if context:
        from src.llm_router import BrainUnavailableError, StratumBrain
        brain = brain or StratumBrain()
        if brain.is_configured():
            try:
                parsed = brain.extract_market_context(context, sport)
            except BrainUnavailableError:
                warnings.append("AI brain unavailable (Groq & Gemini failed) — using regex parse / manual entry.")
        if required_prices_missing(parsed):
            regex_parsed = scraper.parse_odds_from_text(context)
            if regex_parsed:
                # Merge: keep extracted non-nulls, fill gaps from regex. Never invent.
                merged = dict(parsed or {})
                for key, val in regex_parsed.items():
                    if merged.get(key) is None:
                        merged[key] = val
                merged.setdefault("sharp_signal", "none")
                merged.setdefault("source_quotes", [])
                merged.setdefault("injury_notes", None)
                parsed = merged
    else:
        warnings.append("No market context fetched (blocked/offline) — manual entry required.")
    return context, weather, parsed, warnings


# ---------------------------------------------------------------------------
# Phase 4 pure view-model helpers (unit-tested in tests/test_ui_and_sentinel.py)
# ---------------------------------------------------------------------------

def best_ml_pair(rows):
    """Best decimal price per opposing ML side from real rows only.

    Returns (home_best, away_best) American odds or (None, None). Selections
    must literally be Home/Away — we never guess which row is which side.
    """
    home = away = None
    for r in rows or []:
        if r.get("market_type") != MARKET_ML or r.get("american_odds") in (None, 0):
            continue
        sel = r.get("selection")
        if sel == "Home" and (home is None or american_to_decimal(r["american_odds"]) > american_to_decimal(home)):
            home = r["american_odds"]
        elif sel == "Away" and (away is None or american_to_decimal(r["american_odds"]) > american_to_decimal(away)):
            away = r["american_odds"]
    return home, away


def opportunity_cards(scan_rows, comparison, fair_probs=None, steam=False):
    """Reduce a raw scan into ranked, UI-ready opportunity cards.

    Each card: game/market/selection/book/best odds/fair odds/EV %, tagged
    with STEAM / ARB / STALE / SAMPLE flags. Only rows carrying a real
    observed price become cards; anything without an EV baseline keeps
    ev=None (rendered 'UNKNOWN', never fabricated). Arb/stale sort first.
    """
    fair_probs = fair_probs or {}
    stale_keys = {
        (f.get("market_type"), f.get("bookmaker"))
        for f in (comparison.get("stale_flags") or [])
    }
    arb_sels = set()
    arb_pct_val = 0.0
    for flag in comparison.get("arb_flags") or []:
        arb_sels.update({flag.get("side_a"), flag.get("side_b")})
        arb_pct_val = max(arb_pct_val, float(flag.get("arb_pct") or 0.0))

    ranked = rank_value_spots(scan_rows, fair_probs.get("prob_map", {}))
    cards = []
    for r in ranked:
        if r.get("american_odds") in (None, 0):
            continue
        fair_amer = fair_american(fair_probs.get("prob_map", {}).get(r.get("selection")))
        cards.append({
            "game": r.get("match_id", ""),
            "market": r.get("market_type", ""),
            "selection": r.get("selection", ""),
            "book": r.get("bookmaker", ""),
            "best_odds": r["american_odds"],
            "line": r.get("line"),
            "fair_odds": fair_amer,
            "ev_pct": r.get("ev_pct"),
            "is_props": r.get("market_type") == MARKET_PROPS,
            "arb": r.get("selection") in arb_sels,
            "arb_pct": arb_pct_val if r.get("selection") in arb_sels else 0.0,
            "stale": (r.get("market_type"), r.get("bookmaker")) in stale_keys,
            "steam": bool(steam),
            "sample": r.get("data_source") == "sample",
        })
    cards.sort(key=lambda c: (not c["arb"], not c["stale"],
                              -(c["ev_pct"] if c["ev_pct"] is not None else -1e9)))
    return cards


def filter_cards(cards, filt: str):
    """Apply SCAN filter chips: ALL / STEAM / ARB / PROPS."""
    f = (filt or "ALL").upper()
    if f == "STEAM":
        return [c for c in cards if c["steam"]]
    if f == "ARB":
        return [c for c in cards if c["arb"]]
    if f == "PROPS":
        return [c for c in cards if c["is_props"]]
    return list(cards)


def ledger_with_pnl(bets):
    """Enrich bet rows with realized profit (quant_engine decimal math only)."""
    out = []
    for b in bets or []:
        row = dict(b)
        status, stake = row.get("status"), float(row.get("stake") or 0.0)
        if status == "won" and row.get("odds_placed"):
            row["profit"] = round(stake * (american_to_decimal(float(row["odds_placed"])) - 1.0), 2)
        elif status == "lost":
            row["profit"] = -stake
        else:
            row["profit"] = 0.0
        out.append(row)
    return out


def dark_figure(fig: go.Figure, title: str = "") -> go.Figure:
    """Re-skin any Plotly figure with the Stratum dark theme tokens."""
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=COLORS["surface"],
        plot_bgcolor=COLORS["surface"],
        # Font family is set on every component individually — update_layout
        # does NOT cascade a bare `font.*` onto axis/title fonts in Plotly.
        font=dict(family=ui_theme.FONTS["mono"], color=COLORS["text_secondary"], size=11),
        margin=dict(l=8, r=8, t=42 if title else 12, b=8),
        height=280,
        xaxis=dict(gridcolor=COLORS["border"], zerolinecolor=COLORS["border"],
                   tickfont=dict(family=ui_theme.FONTS["mono"], color=COLORS["text_secondary"])),
        yaxis=dict(gridcolor=COLORS["border"], zerolinecolor=COLORS["border"],
                   tickfont=dict(family=ui_theme.FONTS["mono"], color=COLORS["text_secondary"])),
        hoverlabel=dict(font=dict(family=ui_theme.FONTS["mono"])),
        showlegend=False,
    )
    if title:
        fig.update_layout(title=dict(text=title,
                                     font=dict(family=ui_theme.FONTS["sans"],
                                               color=COLORS["text_primary"], size=13)))
    return fig


def cumulative_profit_figure(rows) -> go.Figure:
    """Dark-theme cumulative settled P&L line chart from enriched ledger rows."""
    xs, ys = [], []
    running = 0.0
    for b in sorted(rows or [], key=lambda r: str(r.get("created_at") or "")):
        if b.get("status") not in ("won", "lost"):
            continue
        running += float(b.get("profit", 0.0))
        xs.append(str(b.get("created_at") or "?"))
        ys.append(round(running, 2))
    fig = go.Figure()
    color = COLORS["positive"] if (ys and ys[-1] >= 0) else COLORS["negative"]
    if xs:
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines+markers",
            line=dict(color=color, width=2.5), marker=dict(size=7, color=color),
            fill="tozeroy",
            fillcolor=("rgba(46,230,166,0.08)" if ys[-1] >= 0 else "rgba(255,77,77,0.08)"),
        ))
    return dark_figure(fig, "CUMULATIVE P&L")


def clv_histogram_figure(clv_values) -> go.Figure:
    """Dark-theme CLV distribution histogram (beat-close margins)."""
    vals = [float(v) for v in (clv_values or []) if v is not None]
    fig = go.Figure()
    if vals:
        fig.add_trace(go.Histogram(
            x=vals, nbinsx=max(6, min(14, len(vals))),
            marker_color=COLORS["positive"], opacity=0.85,
        ))
    return dark_figure(fig, "CLV DISTRIBUTION — BEAT vs MISS THE CLOSE")


# ---------------------------------------------------------------------------
# Sentinel lifecycle (startup / shutdown hooks)
# ---------------------------------------------------------------------------

_SENTINEL_KEY = "sentinel"          # session_state handle (per browser session)
_PROC_STARTED = False               # process-level guard (one thread per server)


def secrets_status() -> dict:
    """Which AI/push secrets are configured (never returns the values).

    config.py resolves each key from st.secrets first, then os.environ — so
    on Streamlit Cloud the Secrets manager alone is enough. Used by the
    missing-keys banner and the SETTINGS view; tests monkeypatch this to
    simulate both states.
    """
    return {
        "groq": bool(GROQ_API_KEY),
        "google": bool(GOOGLE_API_KEY),
        "webhook": bool(DEFAULT_WEBHOOK),
    }


def missing_required_secrets(status: dict) -> list:
    """Human-readable names of absent API keys ([] when live scanning is ready)."""
    missing = []
    if not status.get("groq"):
        missing.append("GROQ_API_KEY")
    if not status.get("google"):
        missing.append("GOOGLE_API_KEY")
    return missing


def _load_settings() -> dict:
    """Settings persistence: .streamlit/settings.json keeps values across
    fresh browser sessions (session_state alone dies with the tab)."""
    import json
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "settings.json")
    defaults = {
        "sentinel_enabled": bool(DEFAULT_WEBHOOK),
        "webhook_url": DEFAULT_WEBHOOK,
        "bankroll": 1000.0,
        "kelly_fraction": 0.25,
        "tracked": [],
        "theme": "dark",
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        defaults.update({k: v for k, v in stored.items() if k in defaults})
    except (OSError, ValueError):
        pass  # first run / corrupt file -> defaults, never crash
    return defaults


def _save_settings(settings: dict) -> None:
    import json
    import os

    dirpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit")
    try:
        os.makedirs(dirpath, exist_ok=True)
        with open(os.path.join(dirpath, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError as exc:  # read-only disk etc — degrade quietly
        logger.warning("Settings not persisted: %s", exc)


def ensure_app_boot() -> dict:
    """One-time per-process boot: DB schema + settings + shutdown hook.

    The Sentinel starts lazily (only when enabled in Settings) but ALWAYS
    stops gracefully on interpreter exit via atexit — the registered hook
    joins every live daemon thread so a Cloud container never leaks watchers.
    """
    global _PROC_STARTED
    init_db()
    live_watcher.ensure_schema()
    if "settings" not in st.session_state:
        st.session_state["settings"] = _load_settings()
    if not _PROC_STARTED:
        atexit.register(live_watcher.stop_all_sentinels)
        _PROC_STARTED = True
    return st.session_state["settings"]


def sync_sentinel(settings: dict, force_restart: bool = False) -> bool:
    """Reconcile the singleton watcher with the desired Settings state.

    Cloud thread-safety rules enforced here:
      1. Check ``st.session_state`` first — if this session already holds a
         live handle, reuse it (never re-inspect/re-spawn needlessly).
      2. Before spawning, check the PROCESS-level singleton; if another
         session's thread is already running, adopt it instead of starting a
         duplicate (duplicate watchers = duplicated polls, alerts, memory).
      3. ``start_sentinel`` itself refuses to double-spawn as a final guard.

    Returns True when the Sentinel thread is alive afterwards.
    """
    current = st.session_state.get(_SENTINEL_KEY) or live_watcher.get_sentinel()
    desired_running = bool(settings.get("sentinel_enabled"))
    tracked = [t for t in settings.get("tracked", []) if t and t.strip()]

    if not desired_running:
        if current is not None or live_watcher.is_sentinel_alive():
            live_watcher.stop_sentinel()
        st.session_state.pop(_SENTINEL_KEY, None)
        return False

    if current is not None and current.running and not force_restart:
        current.set_tracked(tracked)
        current.webhook_url = settings.get("webhook_url", "")
        st.session_state[_SENTINEL_KEY] = current
        return True

    if live_watcher.is_sentinel_alive() and not force_restart:
        # Another session already owns the one-per-process thread: adopt it.
        existing = live_watcher.get_sentinel()
        st.session_state[_SENTINEL_KEY] = existing
        return existing.running

    sentinel = live_watcher.start_sentinel(
        tracked_matches=tracked,
        sport="NFL",
        webhook_url=settings.get("webhook_url", ""),
        db_path=DATABASE_PATH,
        interval=live_watcher.POLL_INTERVAL_SECONDS,
        restart=force_restart,
    )
    st.session_state[_SENTINEL_KEY] = sentinel
    return sentinel.running


# ---------------------------------------------------------------------------
# Graceful-degradation banner (missing API keys must NEVER white-screen)
# ---------------------------------------------------------------------------

def secrets_banner_html(missing: list) -> str:
    """Friendly HTML banner shown when live-scan AI keys are absent.

    Pure function (unit-tested): the app keeps rendering in degraded mode —
    manual entry, sample boards and the offline ledger all stay available.
    """
    keys = ", ".join(missing) if missing else "API keys"
    return (
        '<div class="stratum-banner">'
        '<div class="stratum-banner-title">CONNECT API KEYS IN SETTINGS TO '
        'ENABLE LIVE SCANNING</div>'
        f'<div class="stratum-banner-body">Missing {ui_theme.esc(keys)}. '
        "Add them under SETTINGS → SECRETS (Streamlit Cloud: app dashboard "
        "→ Secrets). The terminal runs fully offline meanwhile — manual "
        "entry, sample boards and the audit ledger all work without keys."
        "</div></div>"
    )


# ---------------------------------------------------------------------------
# Shared UI fragments
# ---------------------------------------------------------------------------

def _read_view_from_query() -> str:
    """Bottom-nav links set ?view=<key>; sync into session_state once."""
    qp = st.query_params
    requested = str(qp.get("view", "")).lower()
    if requested in VIEWS:
        if st.session_state.get("current_view") != requested:
            st.session_state["current_view"] = requested
    elif "current_view" not in st.session_state:
        st.session_state["current_view"] = "scan"
    return st.session_state["current_view"]


def _empty(title: str, note: str = "") -> None:
    st.markdown(
        '<div class="stratum-card" style="align-items:center;padding:34px 16px;">'
        f'<div class="stratum-card-value" style="font-size:16px;">{ui_theme.esc(title)}</div>'
        f'<div class="stratum-card-meta">{ui_theme.esc(note)}</div></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# VIEW: SCAN
# ---------------------------------------------------------------------------

def view_scan(settings: dict, sentinel_running: bool) -> None:
    c_search, c_btn = st.columns([5, 1], gap="small")
    with c_search:
        match = st.text_input(
            "Matchup", value="", placeholder="Chiefs @ Ravens",
            label_visibility="collapsed", key="scan_match",
        )
    with c_btn:
        st.write("")  # vertical nudge to align with the input
        run = st.button("SCAN", type="primary", key="run_scan",
                        disabled=not match.strip(), use_container_width=True)

    sport = st.selectbox("Sport", SPORTS, index=0, key="scan_sport")

    # Track toggle feeds the Sentinel watch-list.
    track = st.toggle(
        "Track with Sentinel (poll every 30s)",
        value=(match.strip() in settings.get("tracked", [])) if match.strip() else False,
        key="track_toggle", disabled=not match.strip(),
    )
    if match.strip():
        now = match.strip()
        tracked = list(settings.get("tracked", []))
        if track and now not in tracked:
            tracked.append(now)
            settings["tracked"] = tracked
            _save_settings(settings)
            if sentinel_running and live_watcher.get_sentinel() is not None:
                live_watcher.get_sentinel().set_tracked(tracked)
        elif not track and now in tracked:
            settings["tracked"] = [t for t in tracked if t != now]
            _save_settings(settings)
            if sentinel_running and live_watcher.get_sentinel() is not None:
                live_watcher.get_sentinel().set_tracked(settings["tracked"])

    st.session_state.setdefault("scan_filter", "ALL")
    chip_cols = st.columns(len(FILTERS))
    for i, opt in enumerate(FILTERS):
        active = st.session_state["scan_filter"] == opt
        if chip_cols[i].button(opt, key=f"fchip_{opt}", use_container_width=True,
                               type="primary" if active else "secondary"):
            st.session_state["scan_filter"] = opt

    cached = st.session_state.get("scan_result")
    if run:
        scanner = MarketScanner()
        with st.spinner("Scanning market board…"):
            try:
                rows = scanner.scan_match(match.strip(), sport=sport)
                comparison = scanner.compare_books(rows)
            except Exception as exc:  # graceful, never a blank page
                st.error(f"Scanner failed gracefully: {exc}")
                return
        home_best, away_best = best_ml_pair(rows)
        fair, prob_map = (None, {})
        if home_best and away_best:
            try:
                fair, prob_map = build_fair_probs(home_best, away_best)
            except ValueError as exc:
                st.warning(f"Fair-prob baseline unavailable: {exc}")
        from datetime import datetime, timezone
        snap_hist = st.session_state.setdefault("snapshot_history", {})
        signals = scan_signals_for_match(list(snap_hist.get(match.strip(), [])))
        books = {}
        for r in rows:
            if r.get("market_type") == MARKET_ML and r.get("selection") in ("Home", "Away"):
                slot = books.setdefault(r["bookmaker"], {})
                key = "home_american" if r["selection"] == "Home" else "away_american"
                slot.setdefault(key, r["american_odds"])
        snap_hist.setdefault(match.strip(), []).append(
            {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "books": books}
        )
        snap_hist[match.strip()] = snap_hist[match.strip()][-50:]
        st.session_state["scan_result"] = {
            "match": match.strip(), "rows": rows, "comparison": comparison,
            "fair": fair, "prob_map": prob_map, "steam": signals["steam"],
            "arb_pct": signals["arb_pct"],
        }

    result = st.session_state.get("scan_result")
    if not result or result.get("match") != match.strip():
        _empty("NO ACTIVE SCAN",
               "Enter a matchup and press SCAN. Silence is data — we never fabricate a board.")
        return

    rows = result["rows"]
    sources = {r.get("data_source") for r in rows}
    if sources == {"sample"}:
        st.warning(
            "SAMPLE DATA — live feed unavailable. Prices below are DEMO placeholders "
            "(data_source='sample'); verify at your book before acting."
        )

    cards = opportunity_cards(rows, result["comparison"],
                              {"prob_map": result["prob_map"]}, steam=result["steam"])
    cards = filter_cards(cards, st.session_state["scan_filter"])

    k1, k2, k3, k4 = st.columns(4, gap="small")
    with k1:
        ui_theme.render_metric_card("Markets Scanned", str(len(rows)), trend="flat",
                                    meta=f"{result['match']} · {sport}")
    with k2:
        ev_top = next((c["ev_pct"] for c in cards if c["ev_pct"] is not None), None)
        ui_theme.render_metric_card("Top Edge",
                                    pct(ev_top) if ev_top is not None else "UNKNOWN",
                                    delta=ev_top,
                                    trend="up" if (ev_top or 0) > 0 else "flat",
                                    meta="EV% vs no-vig fair")
    with k3:
        arb = result["arb_pct"]
        ui_theme.render_metric_card("Arb Margin",
                                    pct(arb, sign=False) if arb > 0 else "NONE",
                                    trend="up" if arb > 0 else "flat",
                                    meta="guaranteed %" if arb > 0 else "implied sum >= 100%")
    with k4:
        ui_theme.render_metric_card("Steam", "DETECTED" if result["steam"] else "QUIET",
                                    trend="up" if result["steam"] else "flat",
                                    meta=">=3 majors in sync")

    ui_theme.section_label(f"OPPORTUNITIES — {len(cards)} SHOWN")
    if not cards:
        _empty("ZERO OPPORTUNITIES IN THIS FILTER", "Switch the filter chip or widen the scan.")
        return
    # Seal each surfaced signal into the immutable audit ledger (Phase 5).
    # Fire-and-forget: a ledger failure must NEVER break the SCAN view.
    try:
        for c in cards[:12]:
            conf = calculate_confidence(
                edge_pct=c.get("ev_pct"), book_agreement_count=None,
                data_age_seconds=None, historical_win_rate=None)
            log_scan(result["match"], c["market"], c["selection"],
                     offered_odds=c["best_odds"], fair_odds_calc=c.get("fair_odds"),
                     edge_pct=c.get("ev_pct"), confidence_score=conf,
                     sport=sport, data_source="sample" if c["sample"] else "live")
    except Exception as exc:  # noqa: BLE001 - auditing is best-effort
        logger.warning("Ledger seal skipped: %s", exc)
    for idx, c in enumerate(cards[:12]):
        _opportunity_card(c, idx, settings)


def _opportunity_card(c: dict, idx: int, settings: dict) -> None:
    badges = ""
    if c["arb"]:
        badges += '<span class="stratum-badge stratum-badge-hot">ARB ' + pct(c["arb_pct"], sign=False) + "</span>"
    if c["stale"]:
        badges += '<span class="stratum-badge stratum-badge-stale">STALE</span>'
    if c["steam"]:
        badges += '<span class="stratum-badge stratum-badge-hot">STEAM</span>'
    if c["sample"]:
        badges += '<span class="stratum-badge stratum-badge-sample">SAMPLE</span>'
    ev = c["ev_pct"]
    ev_color = (COLORS["positive"] if (ev is not None and ev > 0)
                else COLORS["negative"] if (ev is not None and ev < 0)
                else COLORS["text_secondary"])
    line_txt = "\u2014" if c["line"] is None else f"{c['line']:+.1f}"
    st.markdown(
        '<div class="stratum-opp">'
        '<div class="stratum-opp-head">'
        f'<span class="stratum-opp-game">{ui_theme.esc(c["game"])} — {ui_theme.esc(c["selection"])}</span>'
        f'<span class="stratum-opp-market">{ui_theme.esc(c["market"])}</span></div>'
        f'<div style="margin-top:6px">{badges}</div>'
        '<div class="stratum-opp-grid">'
        f'<div><div class="stratum-opp-cell-k">Best Odds</div>'
        f'<div class="stratum-opp-cell-v">{fmt_odds(c["best_odds"])}</div></div>'
        f'<div><div class="stratum-opp-cell-k">Fair Odds</div>'
        f'<div class="stratum-opp-cell-v">{fmt_odds(c["fair_odds"])}</div></div>'
        f'<div><div class="stratum-opp-cell-k">Line</div>'
        f'<div class="stratum-opp-cell-v">{line_txt}</div></div>'
        f'<div><div class="stratum-opp-cell-k">Edge EV</div>'
        f'<div class="stratum-opp-cell-v" style="color:{ev_color}">'
        f'{pct(ev) if ev is not None else "UNKNOWN"}</div></div>'
        "</div>"
        f'<div class="stratum-card-meta">{ui_theme.esc(c["book"])}</div>'
        "</div>",
        unsafe_allow_html=True,
    )
    stake = round(float(settings.get("bankroll", 1000.0))
                  * float(settings.get("kelly_fraction", 0.25)) * 0.05, 2)
    bcol1, _ = st.columns([1, 3], gap="small")
    with bcol1:
        if st.button("PLACE BET",
                     key=f"place_{idx}_{c['game']}_{c['market']}_{c['selection']}_{c['book']}",
                     type="primary", use_container_width=True):
            try:
                bid = record_bet(c["game"], c["market"], c["selection"],
                                 float(c["best_odds"]), stake,
                                 data_source="sample" if c["sample"] else "live")
                st.success(f"Bet #{bid} logged at {fmt_odds(c['best_odds'])} — audited against the close later.")
            except (ValueError, KeyError) as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# VIEW: PORTFOLIO (My Bets)
# ---------------------------------------------------------------------------

def view_portfolio(settings: dict) -> None:
    bets = ledger_with_pnl(list_bets())
    open_bets = [b for b in bets if b["status"] == "open"]
    rep = get_performance_report(days=30)

    m1, m2, m3, m4 = st.columns(4, gap="small")
    with m1:
        ui_theme.render_metric_card("Net P&L", money(rep["net_profit"], sign=True),
                                    delta=rep["net_profit"],
                                    meta=f"ROI {pct(rep['roi_pct'], sign=False)} · 30d")
    with m2:
        ui_theme.render_metric_card("Open Exposure", money(sum(b["stake"] for b in open_bets)),
                                    trend="flat", meta=f"{len(open_bets)} live positions")
    with m3:
        avg_clv = rep["avg_clv_pct"]
        ui_theme.render_metric_card("Avg CLV",
                                    pct(avg_clv) if avg_clv is not None else "UNKNOWN",
                                    delta=avg_clv,
                                    meta=(f"beat close {rep['beat_close_rate_pct']:.0f}%"
                                          if rep["beat_close_rate_pct"] is not None
                                          else "needs closing lines"))
    with m4:
        ui_theme.render_metric_card("Win Rate",
                                    pct(rep["win_rate_pct"], sign=False) if rep["n_settled"] else "0.0%",
                                    trend="flat",
                                    meta=f"{rep['n_settled']} settled · {rep['n_bets']} logged")

    ui_theme.section_label("ACTIVE POSITIONS")
    if not bets:
        _empty("LEDGER EMPTY",
               "Place from SCAN or record a manual bet below. The scoreboard starts empty — like every honest one.")
    else:
        df = pd.DataFrame([{
            "ID": b["id"], "Game": b["match_id"], "Market": b["market"],
            "Selection": b["selection"], "Placed": fmt_odds(b["odds_placed"]),
            "Stake": money(b["stake"]), "Status": str(b["status"]).upper(),
            "Close": fmt_odds(b.get("closing_odds")), "CLV": pct(b.get("clv_value")),
            "P&L": money(b["profit"], sign=True),
        } for b in bets])
        st.dataframe(df, use_container_width=True, hide_index=True)

        with st.expander("Settle / lock closing line"):
            sc1, sc2, sc3 = st.columns([1, 1, 2], gap="small")
            with sc1:
                bid_in = st.number_input("Bet ID", min_value=1, step=1, key="settle_id")
            with sc2:
                close = st.number_input("Closing odds", value=0, step=5, key="settle_close",
                                        help="American price at close. 0 = skip CLV.")
            with sc3:
                status = st.selectbox("Settle as", ["(keep)", "open", "won", "lost", "void"],
                                      key="settle_status")
            if st.button("APPLY", key="settle_apply", use_container_width=True):
                try:
                    if close:
                        v = update_closing_line(int(bid_in), float(close))
                        st.success(f"CLV {signed(v, suffix='%')} — "
                                   f"{'beat the close' if v > 0 else 'behind the close'}")
                    if status != "(keep)":
                        settle_bet(int(bid_in), status)
                        st.success(f"Bet #{int(bid_in)} set to {status.upper()}")
                    st.rerun()
                except (KeyError, ValueError) as exc:
                    st.error(str(exc))

    ui_theme.section_label("PERFORMANCE")
    left, right = st.columns(2, gap="small")
    with left:
        st.plotly_chart(cumulative_profit_figure(bets), use_container_width=True, key="fig_pnl")
    with right:
        st.plotly_chart(clv_histogram_figure([b.get("clv_value") for b in bets]),
                        use_container_width=True, key="fig_clv")

    ui_theme.section_label("RECORD MANUAL BET")
    with st.form("manual_bet_form", clear_on_submit=True):
        fc1, fc2, fc3 = st.columns(3, gap="small")
        with fc1:
            m = st.text_input("Match", placeholder="Chiefs @ Ravens")
            market = st.selectbox("Market", ["ML", "Spread", "Total", "PlayerProps",
                                             "Alternates", "Halves", "Quarters"])
        with fc2:
            selection = st.text_input("Selection", placeholder="Home -3.5")
            odds = st.number_input("Odds placed (American)", value=0, step=5)
        with fc3:
            stake_in = st.number_input("Stake ($)", min_value=0.0, value=0.0, step=10.0)
            source = st.selectbox("Data source", ["manual", "live", "sample"])
        submitted = st.form_submit_button("RECORD BET", type="primary", use_container_width=True)
        if submitted:
            try:
                if odds == 0:
                    raise ValueError("Odds of 0 are not a price — enter the American odds you got.")
                new_id = record_bet(m, market, selection, odds, stake_in, data_source=source)
                st.success(f"Logged bet #{new_id}. Never guessed, always audited.")
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# VIEW: AUDIT (immutable scan ledger — Phase 5 flight recorder)
# ---------------------------------------------------------------------------

def view_audit() -> None:
    """Read-only browser over the sealed scan_history ledger.

    Resolved/expired rows are protected by SQLite triggers, so nothing here
    can mutate history — this view only renders what the engine provably
    recorded at scan time.
    """
    min_conf = st.slider("MIN CONFIDENCE", 0, 95, DEFAULT_MIN_CONFIDENCE, step=5,
                         key="audit_min_conf")
    m = ledger_metrics()
    k1, k2, k3, k4 = st.columns(4, gap="small")
    with k1:
        ui_theme.render_metric_card("Sealed Scans", str(m["total_scans"]), trend="flat",
                                    meta=f"{m['n_resolved']} resolved · {m['n_noise']} noise")
    with k2:
        ui_theme.render_metric_card("Avg Confidence",
                                    str(m["avg_confidence"]) if m["avg_confidence"] is not None else "UNKNOWN",
                                    trend="flat", meta="0-95 scale (capped at 95)")
    with k3:
        clv = m["realized_clv_pct"]
        ui_theme.render_metric_card("Realized CLV",
                                    pct(clv) if clv is not None else "UNKNOWN",
                                    delta=clv, meta="avg vs closing line")
    with k4:
        wr = m["win_rate_pct"]
        ui_theme.render_metric_card("Settled Win Rate",
                                    pct(wr, sign=False) if wr is not None else "UNKNOWN",
                                    trend="flat", meta="resolved won / (won+lost)")

    ui_theme.section_label("SCAN LEDGER — NEWEST FIRST")
    rows = [r for r in list_scans(limit=200)
            if passes_threshold(r.get("confidence_score"), min_conf)]
    if not rows:
        _empty("LEDGER EMPTY AT THIS THRESHOLD",
               "Every SCAN surfaces signals here once you run one. Lower the confidence slider to see sub-threshold noise.")
        return
    df = pd.DataFrame([{
        "Time": str(r.get("timestamp") or "")[:16],
        "Match": r.get("match_ref"), "Market": r.get("market_type"),
        "Selection": r.get("selection"), "Offered": fmt_odds(r.get("offered_odds")),
        "Fair": fmt_odds(r.get("fair_odds_calc")), "Edge": pct(r.get("edge_pct")),
        "Conf": r.get("confidence_score") if r.get("confidence_score") is not None else "—",
        "Band": confidence_band(r.get("confidence_score")),
        "Status": str(r.get("status") or "").upper(),
        "CLV": pct(r.get("clv_pct")), "Source": r.get("data_source"),
        "SHA-256": str(r.get("source_data_hash") or "")[:10],
    } for r in rows])
    st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("Resolve a pending scan against the actual close"):
        pending = [r for r in rows if r.get("status") == "pending"]
        if not pending:
            st.caption("No pending records at this threshold — silence is data.")
        else:
            oc1, oc2, oc3 = st.columns([3, 1, 1], gap="small")
            with oc1:
                labels = [f"{r['timestamp'][:16]} · {r['match_ref']} · {r['market_type']} "
                          f"{r['selection']} @ {fmt_odds(r['offered_odds'])}" for r in pending]
                pick_idx = st.selectbox("Pending record", labels, key="audit_pick")
            with oc2:
                outcome = st.selectbox("Outcome", ["won", "lost", "push", "void"], key="audit_outcome")
            with oc3:
                close_in = st.number_input("Closing odds", value=0, step=5, key="audit_close",
                                           help="American price at close. 0 = resolve without CLV.")
            st.caption("Sealing is one-way: a resolved row becomes immutable (SQLite trigger).")
            if st.button("SEAL RECORD", key="audit_seal", type="primary"):
                rec = pending[labels.index(pick_idx)]
                try:
                    sealed = resolve_scan(rec["id"],
                                          closing_odds=float(close_in) if close_in else None,
                                          outcome=outcome)
                    clv = sealed.get("clv_pct") if isinstance(sealed, dict) else None
                    st.success(f"Sealed. CLV {signed(clv, suffix='%')}"
                               if clv is not None else "Sealed (no closing line supplied).")
                    st.rerun()
                except ImmutableRecordError as exc:
                    st.error(f"Record already sealed — the ledger rejects edits: {exc}")
                except (KeyError, ValueError) as exc:
                    st.error(str(exc))


# ---------------------------------------------------------------------------
# VIEW: ALERTS
# ---------------------------------------------------------------------------

def view_alerts(sentinel_running: bool) -> None:
    alerts = live_watcher.list_alerts(limit=60)
    ui_theme.section_label("SENTINEL FEED")
    if not alerts:
        _empty("NO ALERTS YET",
               "The Sentinel fires on >=1.5 pt line shifts, live arbs and steam. Silence is data.")
        if not sentinel_running:
            st.caption("Enable the Live Sentinel in SETTINGS to start monitoring tracked games.")
        return
    sev_color = {"alert": COLORS["negative"], "warning": COLORS["warning"],
                 "info": COLORS["text_secondary"]}
    for a in alerts:
        color = sev_color.get(a["severity"], COLORS["text_secondary"])
        delivered = "\u25CF PUSHED" if a["delivered"] else "\u25CB LOCAL ONLY"
        st.markdown(
            '<div class="stratum-card" style="flex-direction:row;justify-content:space-between;'
            'align-items:center;gap:12px;margin-bottom:8px;">'
            f'<div><div class="stratum-card-delta" style="color:{color};font-size:14px;">'
            f'{ui_theme.esc(a["kind"].upper())}</div>'
            f'<div style="font-family:{ui_theme.FONTS["mono"]};font-size:13px;'
            f'color:{COLORS["text_primary"]};">{ui_theme.esc(a["message"])}</div></div>'
            f'<div style="text-align:right;white-space:nowrap;"><div class="stratum-card-meta">{delivered}</div>'
            f'<div class="stratum-card-meta">{ui_theme.esc(a["created_at"])}</div></div>'
            "</div>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# VIEW: SETTINGS
# ---------------------------------------------------------------------------

def view_settings(settings: dict) -> None:
    ui_theme.section_label("LIVE SENTINEL")
    enabled = st.toggle("Enable Live Sentinel", value=bool(settings.get("sentinel_enabled")),
                        key="set_enabled",
                        help="Background thread polling tracked games every 30 seconds.")
    webhook = st.text_input(
        "Push Webhook URL (Discord or Telegram bot)",
        value=settings.get("webhook_url", ""), key="set_webhook",
        placeholder="https://discord.com/api/webhooks/... or https://api.telegram.org/bot<token>/sendMessage",
        type="password",
    )
    st.caption(
        f"Cadence: {live_watcher.POLL_INTERVAL_SECONDS}s · shift threshold: "
        f"{live_watcher.LINE_SHIFT_THRESHOLD} pts · every alert is stored in the SQLite "
        f"'alerts' table regardless of delivery."
    )

    ui_theme.section_label("BANKROLL MANAGEMENT")
    bankroll = st.number_input("Bankroll ($)", min_value=1.0,
                               value=float(settings.get("bankroll", 1000.0)),
                               step=50.0, key="set_bankroll")
    kelly = st.slider("Kelly fraction applied to stakes", 0.05, 1.0,
                      float(settings.get("kelly_fraction", 0.25)), step=0.05, key="set_kelly",
                      help="0.25 = quarter-Kelly sizing recommended for live bankrolls.")

    ui_theme.section_label("APPEARANCE")
    theme = st.radio("Theme", ["dark", "light"], horizontal=True,
                     index=0 if settings.get("theme", "dark") == "dark" else 1,
                     key="set_theme")

    ui_theme.section_label("SENTINEL WATCHLIST")
    st.caption("Add matchups from the SCAN tab by toggling 'Track with Sentinel'.")
    tracked = list(settings.get("tracked", []))
    if tracked:
        removed_key = None
        tc = st.columns([4, 1] * min(len(tracked), 3))
        for i, t in enumerate(tracked[:3]):
            with tc[i * 2]:
                st.markdown('<div class="stratum-card"><div class="stratum-card-value" '
                            f'style="font-size:14px">{ui_theme.esc(t)}</div></div>',
                            unsafe_allow_html=True)
            with tc[i * 2 + 1]:
                if st.button("REMOVE", key=f"untrack_{t}", use_container_width=True):
                    removed_key = t
        if removed_key:
            settings["tracked"] = [t for t in tracked if t != removed_key]
            _save_settings(settings)
            if live_watcher.get_sentinel() is not None:
                live_watcher.get_sentinel().set_tracked(settings["tracked"])
            st.rerun()
        if len(tracked) > 3:
            st.caption(f"+{len(tracked) - 3} more tracked.")

    dirty = (
        enabled != bool(settings.get("sentinel_enabled"))
        or webhook != settings.get("webhook_url")
        or bankroll != settings.get("bankroll")
        or kelly != settings.get("kelly_fraction")
        or theme != settings.get("theme")
    )
    if dirty:
        settings.update(sentinel_enabled=enabled, webhook_url=webhook, bankroll=bankroll,
                        kelly_fraction=kelly, theme=theme)
        _save_settings(settings)
        sync_sentinel(settings, force_restart=True)
        st.toast("Settings saved")
        st.rerun()

    ui_theme.section_label("DELIVERY TEST")
    dcol1, dcol2 = st.columns([1, 3], gap="small")
    with dcol1:
        if st.button("SEND TEST ALERT", key="test_alert", disabled=not webhook,
                     use_container_width=True):
            target = live_watcher.get_sentinel() or live_watcher.SentinelThread(
                webhook_url=webhook, db_path=DATABASE_PATH)
            ok = target.send_alert("TEST: Stratum delivery check", severity="info", kind="test")
            (st.success("Webhook accepted the alert.") if ok
             else st.error("Webhook rejected/unreachable — check the URL."))
    with dcol2:
        st.caption("Free push proxy: create a Discord channel webhook (or a Telegram bot via "
                   "@BotFather) and paste the URL above. Alerts reach your phone even with the "
                   "app closed.")


# ---------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------

def render() -> None:
    st.set_page_config(
        page_title="Stratum", layout="wide", initial_sidebar_state="collapsed",
        menu_items={"Get Help": None, "Report a bug": None,
                    "About": "# Stratum\nQuant edge terminal."},
    )
    settings = ensure_app_boot()
    st.markdown(ui_theme.inject_css(theme=settings.get("theme", "dark")), unsafe_allow_html=True)

    current_view = _read_view_from_query()
    sentinel_running = sync_sentinel(settings)

    ui_theme.render_header("STRATUM", "QUANT EDGE TERMINAL", sentinel_running)

    # Graceful degradation: missing API keys show a friendly banner instead of
    # crashing — the app stays fully usable offline (manual entry + samples).
    missing = missing_required_secrets(secrets_status())
    if missing:
        st.markdown(secrets_banner_html(missing), unsafe_allow_html=True)

    try:
        if current_view == "audit":
            view_audit()
        elif current_view == "portfolio":
            view_portfolio(settings)
        elif current_view == "alerts":
            view_alerts(sentinel_running)
        elif current_view == "settings":
            view_settings(settings)
        else:
            view_scan(settings, sentinel_running)
    except Exception as exc:  # the app must ALWAYS remain usable
        logger.exception("UI error")
        st.error(f"Unexpected error (degraded mode): {exc}")

    ui_theme.render_bottom_nav(current_view)


if _HAVE_STREAMLIT:
    render()
else:  # headless fallback keeps `python main.py` working without streamlit
    logger.info("Stratum Ready")
