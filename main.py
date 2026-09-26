"""Stratum v0.1 — Streamlit entrypoint (Phase 3: Scanner, Signals, CLV).

Flow: sidebar inputs -> optional live fetch (DDG snippets + Open-Meteo weather)
-> LLM extraction (Groq/Gemini) or regex parse -> manual-entry fallback for any
missing price -> ALL math via src/quant_engine.py -> Plotly charts from
src/report.py -> verdict card. Phase 3 adds three tabs: 🔍 Deep Scan (200-market
board + stale/arb flags), 📊 Signals Dashboard (steam/RLM/arb detectors) and
💰 Bankroll & CLV (Obsidian-memory bet log + performance report). The page is
always usable with zero keys and zero network; Stratum never guesses odds.
"""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO)

try:
    import streamlit as st
    _HAVE_STREAMLIT = True
except ImportError:  # pragma: no cover - allows `python main.py` headless
    _HAVE_STREAMLIT = False

from src import report, scraper
from src.clv_auditor import get_performance_report, list_bets, record_bet, settle_bet, update_closing_line
from src.llm_router import BrainUnavailableError, StratumBrain
from src.market_scanner import MarketScanner
from src.quant_engine import (
    american_to_decimal,
    ev_pct,
    implied_probability,
    kelly_criterion,
    remove_vig_two_way,
    vig_pct_two_way,
)
from src.signal_detector import find_arbitrage_opportunities, scan_signals_for_match

SPORTS = ["NFL", "NBA", "MLB", "Soccer", "Tennis"]


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


def gather_live_context(match: str, sport: str, lat, lon, brain: StratumBrain):
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
# Streamlit UI
# ---------------------------------------------------------------------------

def _tab_analyze(sport: str, match: str, lat, lon):
    """Phase 1/2 single-market analysis (original flow)."""
    with st.sidebar:
        mode = st.toggle("Use live fetch", value=True, help="Off = manual odds entry only")
        brain = StratumBrain()
        if not brain.is_configured():
            st.info("No GROQ_API_KEY / GOOGLE_API_KEY found — live fetch uses regex parse + manual entry.")

    if not st.button("Analyze", type="primary", disabled=not match.strip()):
        return

    # ---- Step 1: optional live fetch -------------------------------------
    parsed = None
    weather = None
    context = ""
    if mode:
        with st.spinner("Fetching context (polite scrape)…"):
            context, weather, parsed, fetch_warnings = gather_live_context(match, sport, lat, lon, brain)
        for w in fetch_warnings:
            st.warning(w)

    # ---- Step 2: manual-entry fallback for anything missing ---------------
    has_ml = bool(parsed) and parsed.get("ml_home_american") is not None and parsed.get("ml_away_american") is not None
    if not has_ml:
        st.warning(
            "Auto-fetch incomplete — enter the live prices you see. "
            "Stratum never guesses odds.",
            icon="✍️",
        )
    st.subheader("Prices" + (" — fetched (confirm below)" if has_ml else " — manual entry"))
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        ml_home = st.number_input("ML Home (American)", value=parsed["ml_home_american"] if has_ml else None, step=5, key="mlh")
    with c2:
        ml_away = st.number_input("ML Away (American)", value=parsed["ml_away_american"] if has_ml else None, step=5, key="mla")
    with c3:
        spread = st.number_input("Spread Home (optional)", value=(parsed or {}).get("spread_home"), step=0.5, key="spr")
    with c4:
        total = st.number_input("Total (optional)", value=(parsed or {}).get("total_line"), step=0.5, key="tot")

    if ml_home is None or ml_away is None or ml_home == 0 or ml_away == 0:
        st.stop()  # "Unknown" is valid; we simply do not compute without real prices

    # ---- Step 3: ALL math in quant_engine ----------------------------------
    with st.expander("Model override & bankroll (for EV / Kelly)"):
        st.caption("Fair prob vs implied is 0 by definition — paste YOUR model win% on the home side to get real EV/Kelly.")
        model_pct = st.number_input("Model win% on HOME (0 = none)", min_value=0, max_value=100, value=0, step=1)
        bankroll = st.number_input("Bankroll ($)", min_value=1.0, value=1000.0, step=50.0)
    try:
        analysis = analyze_market(
            ml_home, ml_away,
            model_prob_home=(model_pct / 100.0) if model_pct else None,
            bankroll=bankroll,
        )
    except ValueError as exc:
        st.error(f"Odds rejected by quant engine: {exc}")
        st.stop()

    # ---- Header card --------------------------------------------------------
    home, _, away = (match + " @ ").split("@", 1)
    weather_line = (
        f"Weather: {weather['temp_c']:.0f}°C, {weather['wind_kmh']:.0f} km/h wind, "
        f"{weather['precip_mm']:.1f} mm — {weather['condition']}"
        if weather else "Weather: Unknown"
    )
    st.subheader(f"{home.strip()} vs {away.strip()}  ·  {sport}")
    st.caption(weather_line)
    if (parsed or {}).get("injury_notes"):
        st.caption(f"🩑 Injuries: {parsed['injury_notes']}")
    if (parsed or {}).get("source_quotes"):
        with st.expander("Audit — exact source quotes used by the extractor"):
            for q in parsed["source_quotes"]:
                st.write(f"“{q}”")

    # ---- Charts -------------------------------------------------------------
    left, right = st.columns(2)
    with left:
        st.plotly_chart(report.chart_novig(
            analysis["fair"]["home"], analysis["fair"]["away"],
            analysis["implied"]["home"], analysis["implied"]["away"],
        ), use_container_width=True)
    with right:
        pub = (parsed or {}).get("public_ticket_pct_home")
        if pub is not None:
            line_dir = "flat"
            if (parsed or {}).get("sharp_signal") == "rlm":
                line_dir = "up" if pub > 50 else "down"
            st.plotly_chart(report.chart_rlm(pub, line_dir), use_container_width=True)
        elif analysis["kelly"]:
            k = analysis["kelly"]
            st.plotly_chart(
                report.chart_kelly(k["edge_home_pct"], k["quarter_kelly_frac"], k["bankroll"]),
                use_container_width=True,
            )
        else:
            st.info("RLM chart needs public ticket %; Kelly chart needs a model probability. Provide one above.")

    # ---- Verdict ------------------------------------------------------------
    st.subheader("Verdict")
    fh = fair_american(analysis["fair"]["home"])
    fa = fair_american(analysis["fair"]["away"])
    rows = [
        f"**Home** — MARKET {int(ml_home):+d}  vs  FAIR {fh:+d}  (fair prob {analysis['fair']['home'] * 100:.2f}%)",
        f"**Away** — MARKET {int(ml_away):+d}  vs  FAIR {fa:+d}  (fair prob {analysis['fair']['away'] * 100:.2f}%)",
    ]
    if analysis["kelly"]:
        k = analysis["kelly"]
        tag_home = "🟢 +EV" if k["ev_home_pct"] > 0 else "🔴 -EV"
        tag_away = "🟢 +EV" if k["ev_home_pct"] < 0 else "🔴 -EV"
        rows.insert(1, f"Home {tag_home} ({k['ev_home_pct']:+.2f}% EV @ model {k['model_prob_home'] * 100:.0f}%) · Away {tag_away}")
        stake_txt = f"${k['stake']:,.2f}" if k["stake"] > 0 else "$0.00 (no bet)"
        rows.append(f"**Recommended ¼-Kelly stake:** {stake_txt} of ${k['bankroll']:,.2f} bankroll")
    else:
        rows.append("**Kelly sizing:** needs model probability — enter your own win% above to compute real EV/Kelly.")
    rows.append(f"**Book vig:** {analysis['vig_pct']:.2f}%  ·  Spread: {spread if spread is not None else 'Unknown'}  ·  Total: {total if total is not None else 'Unknown'}")
    st.markdown("\n\n".join(rows))


def _tab_deep_scan(sport: str, match: str):
    """🔍 Deep Scan — 200-market board, stale lines, arb highlights."""
    if not match.strip():
        st.info("Enter a match (Home @ Away) in the sidebar to run a deep scan.")
        return
    scanner = MarketScanner()
    with st.spinner(f"Scanning all markets for {match}…"):
        try:
            rows = scanner.scan_match(match, sport=sport)
            comparison = scanner.compare_books(rows)
        except Exception as exc:  # never crash the page
            st.error(f"Scanner failed gracefully: {exc}")
            return

    sources = {r.get("data_source") for r in rows}
    if sources == {"sample"}:
        st.warning(
            "⚠️ SAMPLE DATA — live fetch unavailable (offline/blocked). These prices are "
            "DEMO-ONLY placeholders labeled data_source='sample'. Stratum never presents "
            "invented numbers as real. Verify at your book before acting.",
            icon="🧪",
        )
    if not rows:
        st.info("Empty state: no markets could be fetched for this match. Nothing invented.")
        return

    # Fair probs from the best-priced ML pair so EV ranking has a REAL baseline.
    prob_map = {}
    ml_rows = [r for r in rows if r.get("market_type") == "ML"]
    home_best = away_best = None
    for r in ml_rows:
        if r.get("selection") == "Home" and (home_best is None or american_to_decimal(r["american_odds"]) > american_to_decimal(home_best)):
            home_best = r["american_odds"]
        if r.get("selection") == "Away" and (away_best is None or american_to_decimal(r["american_odds"]) > american_to_decimal(away_best)):
            away_best = r["american_odds"]
    fair = None
    if home_best and away_best:
        try:
            fair, prob_map = build_fair_probs(home_best, away_best)
        except ValueError as exc:
            st.warning(f"Fair-prob baseline unavailable: {exc}")

    ranked = rank_value_spots(rows, prob_map)
    arb_by_selection = {}
    for flag in comparison["arb_flags"]:
        arb_by_selection[flag["side_a"]] = flag["arb_pct"]
        arb_by_selection[flag["side_b"]] = flag["arb_pct"]

    st.subheader(f"Top value spots — {len(ranked)} markets scanned ({sport}: {match})")
    import pandas as pd
    top10 = ranked[:10]
    df = pd.DataFrame([{
        "Market": r["market_type"], "Selection": r["selection"], "Book": r["bookmaker"],
        "Odds": int(r["american_odds"]) if r["american_odds"] is not None else None,
        "Line": r.get("line"), "EV %": r["ev_pct"] if r["ev_pct"] is not None else "Unknown",
        "Arb %": arb_by_selection.get(r["selection"], 0.0), "Source": r.get("data_source"),
    } for r in top10])

    def _highlight(row):
        green = float(row["Arb %"] or 0) > 0
        return ["background-color: #123B2B; color: #2EE6A6" if green else ""] * len(row)
    st.dataframe(df.style.apply(_highlight, axis=1), use_container_width=True, hide_index=True)

    if comparison["stale_flags"]:
        st.subheader("💤 Potential STALE lines (soft books lagging consensus)")
        st.dataframe(pd.DataFrame(comparison["stale_flags"]), use_container_width=True, hide_index=True)
    else:
        st.caption("No stale-line discrepancies over threshold in this scan.")
    if comparison["arb_flags"]:
        st.success(f"💸 Arbitrage detected: {comparison['arb_flags']}")
    else:
        st.caption("No arbitrage margin across best opposing prices (implied sum ≥ 100%).")
    if fair:
        st.caption(
            f"Baseline: best ML {int(home_best):+d}/{int(away_best):+d} → fair "
            f"{fair['home'] * 100:.2f}% / {fair['away'] * 100:.2f}% (quant_engine, no-vig)."
        )


def _tab_signals(sport: str, match: str):
    """📊 Signals Dashboard — steam / RLM / arb feed for tracked games."""
    st.subheader("Live signal feed")
    if not match.strip():
        st.info("Enter a match in the sidebar to track its signals.")
        return

    scanner = MarketScanner()
    try:
        rows = scanner.scan_match(match, sport=sport)
    except Exception as exc:
        st.error(f"Scanner failed gracefully: {exc}")
        return

    # Build a snapshot from current best prices per side (real fetched quotes only).
    books = {}
    for r in rows:
        if r.get("market_type") == "ML" and r.get("selection") in ("Home", "Away"):
            b = books.setdefault(r["bookmaker"], {})
            key = "home_american" if r["selection"] == "Home" else "away_american"
            if key not in b:
                b[key] = r["american_odds"]
    from datetime import datetime, timezone
    current = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "books": books}

    # Obsidian memory: previous scans persist snapshots for steam detection.
    history = st.session_state.setdefault("snapshot_history", {})
    prior = list(history.get(match, []))
    signals = scan_signals_for_match(prior + [current])
    history.setdefault(match, []).append(current)
    history[match] = history[match][-50:]  # keep last 50 snapshots per game

    cols = st.columns(3)
    with cols[0]:
        st.metric("🔥 Steam", "DETECTED" if signals["steam"] else "None")
    with cols[1]:
        st.metric("⚖️ RLM", "SHARP_ON_AWAY" if signals["rlm"] != "NONE" else "None")
    with cols[2]:
        st.metric("💸 Arb margin", f"{signals['arb_pct']:.2f}%" if signals["arb_pct"] > 0 else "0.00%")

    if signals["notes"]:
        for n in signals["notes"]:
            st.write(n)
    else:
        st.caption("No signals right now — silence is data, we don't fabricate alerts.")

    st.caption(
        "First-run note: steam detection needs ≥2 timestamped snapshots inside a 5-min window; "
        "re-scan after prices move. Public ticket % and opening lines come from live fetch only — "
        "when unavailable they stay Unknown (manual fields below)."
    )
    with st.expander("Manual signal inputs (only if you can SEE the data)"):
        pub = st.number_input("Public ticket % on Home (0 = unknown)", min_value=0, max_value=100, value=0, step=1)
        open_ml = st.number_input("Opening Home ML (American, 0 = unknown)", value=0, step=5)
        cur_ml = st.number_input("Current Home ML (American, 0 = unknown)", value=0, step=5)
        if pub and open_ml and cur_ml:
            man = scan_signals_for_match([], public_ticket_pct_home=pub,
                                         opening_home_american=open_ml, current_home_american=cur_ml)
            st.write("Manual RLM check →", "⚖️ " + man["rlm"])


def _tab_clv():
    """💰 Bankroll & CLV — log bets, settle them, audit long-term edge."""
    st.subheader("Bet logger (Obsidian memory)")
    with st.form("log_bet_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            m = st.text_input("Match", placeholder="Chiefs @ Ravens")
            market = st.selectbox("Market", ["ML", "Spread", "Total", "PlayerProps", "Alternates", "Halves", "Quarters"])
        with c2:
            selection = st.text_input("Selection", placeholder="Home -3.5")
            odds = st.number_input("Odds placed (American)", value=0, step=5, help="Real price you actually got. 0 = invalid.")
        with c3:
            stake = st.number_input("Stake ($)", min_value=0.0, value=0.0, step=10.0)
            logged_from = st.selectbox("Data source", ["manual", "live", "sample"])
        submitted = st.form_submit_button("Log bet")
        if submitted:
            try:
                if odds == 0:
                    raise ValueError("Odds of 0 are not a price — enter the American odds you got.")
                bet_id = record_bet(m, market, selection, odds, stake, data_source=logged_from)
                st.success(f"Logged bet #{bet_id}. Never guessed, always audited.")
            except (ValueError, KeyError) as exc:
                st.error(str(exc))

    st.subheader("Ledger")
    bets = list_bets()
    if not bets:
        st.info("No bets logged yet. The scoreboard starts empty — like every honest one.")
        return
    import pandas as pd
    settled = []
    for b in bets:
        row = dict(b)
        if row["status"] == "won" and row.get("odds_placed"):
            row["profit"] = round(row["stake"] * (american_to_decimal(float(row["odds_placed"])) - 1.0), 2)
        elif row["status"] == "lost":
            row["profit"] = -float(row["stake"])
        else:
            row["profit"] = 0.0
        settled.append(row)
    st.dataframe(pd.DataFrame(settled)[[
        "id", "match_id", "market", "selection", "odds_placed", "stake",
        "status", "closing_odds", "clv_value", "profit", "created_at",
    ]], use_container_width=True, hide_index=True)

    with st.expander("Update a bet (closing line / result)"):
        bid = st.number_input("Bet id", min_value=1, step=1)
        close = st.number_input("Closing odds (American, 0 = skip)", value=0, step=5)
        status = st.selectbox("Settle as", ["(keep)", "open", "won", "lost", "void"])
        if st.button("Apply"):
            try:
                if close:
                    v = update_closing_line(int(bid), float(close))
                    st.success(f"CLV recorded: {v:+.2f}% ({'beat the close 🟢' if v > 0 else 'behind the close 🔴'})")
                if status != "(keep)":
                    settle_bet(int(bid), status)
                    st.success(f"Bet #{int(bid)} set to {status}.")
            except (KeyError, ValueError) as exc:
                st.error(str(exc))

    st.subheader("Performance (last 30 days)")
    rep = get_performance_report(days=30)
    if not rep["has_data"]:
        st.info("No bets in window — nothing to report yet.")
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("ROI", f"{rep['roi_pct']:.1f}%", f"{rep['net_profit']:+,.0f}$")
    m2.metric("Win rate", f"{rep['win_rate_pct']:.1f}%", f"{rep['n_settled']} settled")
    m3.metric("Avg CLV", f"{rep['avg_clv_pct']:+.2f}%" if rep["avg_clv_pct"] is not None else "Unknown",
              f"beat close {rep['beat_close_rate_pct']:.0f}%" if rep["beat_close_rate_pct"] is not None else "needs closing lines")
    m4.metric("Bets logged", rep["n_bets"], f"${rep['total_staked']:,.0f} staked")
    best = rep["best_market"]
    worst = rep["worst_market"]
    if best and worst:
        st.caption(f"Best market: {best['market']} ({best['roi_pct']:+.1f}% ROI) · Worst: {worst['market']} ({worst['roi_pct']:+.1f}% ROI)")

    left, right = st.columns(2)
    with left:
        st.plotly_chart(report.chart_cumulative_profit(settled), use_container_width=True)
    with right:
        st.plotly_chart(report.chart_clv_histogram([b["clv_value"] for b in bets]), use_container_width=True)


def render() -> None:
    st.set_page_config(page_title="Stratum v0.1", page_icon="⛰️", layout="wide")
    st.title("⛰️ Stratum v0.1 — Quant Engine")
    st.caption("Free-tier edge: extract real prices → strip the vig → size with Kelly → audit against the close. "
               "The AI only reads; the math is ours. Stratum doesn't guess; it verifies against market makers.")

    with st.sidebar:
        st.header("Setup")
        sport = st.selectbox("Sport", SPORTS, help="Filters scanner results efficiently")
        match = st.text_input("Match (Home @ Away)", placeholder="Chiefs @ Ravens")
        st.subheader("Weather (optional)")
        col1, col2 = st.columns(2)
        with col1:
            lat = st.number_input("Lat", value=None, format="%.4f")
        with col2:
            lon = st.number_input("Lon", value=None, format="%.4f")

    tab_scan, tab_sig, tab_clv, tab_old = st.tabs(
        ["🔍 Deep Scan", "📊 Signals Dashboard", "💰 Bankroll & CLV", "🎯 Single-Market Analyze"]
    )
    try:
        with tab_old:
            _tab_analyze(sport, match, lat, lon)
        with tab_scan:
            _tab_deep_scan(sport, match)
        with tab_sig:
            _tab_signals(sport, match)
        with tab_clv:
            _tab_clv()
    except BrainUnavailableError as exc:
        st.warning(f"AI brain unavailable ({exc}) — falling back to manual entry. The page stays usable.")
    except Exception as exc:  # the page must ALWAYS remain usable
        logging.getLogger("stratum.ui").exception("UI error")
        st.error(f"Unexpected error (degraded to manual mode): {exc}")


if _HAVE_STREAMLIT:
    render()
else:  # headless fallback keeps `python main.py` working without streamlit
    print("Stratum Ready")
