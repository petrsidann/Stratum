"""Stratum v0.1 — Streamlit entrypoint (Phase 2: AI Brain + free data layer).

Flow: sidebar inputs -> optional live fetch (DDG snippets + Open-Meteo weather)
-> LLM extraction (Groq/Gemini) or regex parse -> manual-entry fallback for any
missing price -> ALL math via src/quant_engine.py -> Plotly charts from
src/report.py -> verdict card. The page is always usable with zero keys and
zero network; Stratum never guesses odds.
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
from src.llm_router import BrainUnavailableError, StratumBrain
from src.quant_engine import (
    american_to_decimal,
    implied_probability,
    kelly_criterion,
    remove_vig_two_way,
)

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
def render() -> None:
    st.set_page_config(page_title="Stratum v0.1", page_icon="⛰️", layout="wide")
    st.title("⛰️ Stratum v0.1 — Quant Engine")
    st.caption("Free-tier edge: extract real prices → strip the vig → size with Kelly. The AI only reads; the math is ours.")

    with st.sidebar:
        st.header("Setup")
        sport = st.selectbox("Sport", SPORTS)
        match = st.text_input("Match (Home @ Away)", placeholder="Chiefs @ Ravens")
        st.subheader("Weather (optional)")
        col1, col2 = st.columns(2)
        with col1:
            lat = st.number_input("Lat", value=None, format="%.4f")
        with col2:
            lon = st.number_input("Lon", value=None, format="%.4f")
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


if _HAVE_STREAMLIT:
    render()
else:  # headless fallback keeps `python main.py` working without streamlit
    print("Stratum Ready")
