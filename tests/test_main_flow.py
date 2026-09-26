"""End-to-end flow tests for main.py — pure functions, zero network, zero keys.

The canonical framework example: -150 / +130 -> fair ~57.98% / 42.02%.
Also covers the manual-entry fallback logic and brain-failure degradation.
"""

import pytest

import main
from src.llm_router import BrainUnavailableError, StratumBrain
from src.quant_engine import kelly_criterion


# ---- Step 3 math: quant engine via the app's analyze_market ---------------
def test_analyze_market_canonical_minus150_plus130():
    a = main.analyze_market(-150, 130)
    assert round(a["fair"]["home"] * 100, 2) == 57.98
    assert round(a["fair"]["away"] * 100, 2) == 42.02
    assert a["implied"]["home"] == pytest.approx(0.60)
    assert a["implied"]["away"] == pytest.approx(1 / 2.30, rel=1e-5)
    # vig = 60% + 43.48% - 100% ~ 3.48%
    assert a["vig_pct"] == pytest.approx(3.478, abs=0.01)
    # By definition, fair-vs-implied edge is zero without a model prob:
    assert a["kelly"] is None


def test_analyze_market_with_model_override_computes_ev_and_quarter_kelly():
    # Model says home wins 62% at -150 (decimal 1.6667): positive EV.
    a = main.analyze_market(-150, 130, model_prob_home=0.62, bankroll=1000.0)
    k = a["kelly"]
    assert k is not None
    dec = 1 + 100 / 150
    expected_full = kelly_criterion(0.62, dec)
    assert k["quarter_kelly_frac"] == pytest.approx(expected_full * 0.25, rel=1e-6)
    assert k["stake"] == pytest.approx(expected_full * 0.25 * 1000.0, rel=1e-6)
    assert k["edge_home_pct"] == pytest.approx((0.62 * dec - 1.0) * 100.0, rel=1e-6)
    assert k["ev_home_pct"] > 0


def test_fair_american_display_conversion():
    assert main.fair_american(0.5798) == -138
    assert main.fair_american(0.4202) == 138
    assert main.fair_american(None) is None


# ---- Step 2 fallback logic --------------------------------------------------
def test_required_prices_missing_cases():
    assert main.required_prices_missing(None) is True
    assert main.required_prices_missing({}) is True
    assert main.required_prices_missing({"ml_home_american": -150, "ml_away_american": 130}) is False
    assert main.required_prices_missing({"ml_home_american": -150, "ml_away_american": None}) is True
    # A spread/total alone counts as a fetched price (still needs ML typed manually).
    assert main.required_prices_missing({"spread_home": -3.5}) is False


def test_gather_live_context_degrades_without_network(monkeypatch):
    """Zero keys, zero network -> empty context, None prices, warning list."""
    monkeypatch.setattr(main.scraper, "fetch_match_context", lambda q: "")
    monkeypatch.setattr(main.scraper, "fetch_weather", lambda lat, lon: None)
    brain = StratumBrain(groq_api_key="", google_api_key="")
    context, weather, parsed, warnings = main.gather_live_context("A @ B", "NFL", None, None, brain)
    assert context == ""
    assert weather is None
    assert parsed is None
    assert any("manual entry" in w for w in warnings)


def test_gather_live_context_brain_failure_falls_back_to_regex(monkeypatch):
    """BrainUnavailableError must NOT crash; regex parse fills in instead."""
    text = "Kansas City -150, Baltimore +130."
    monkeypatch.setattr(main.scraper, "fetch_match_context", lambda q: text)
    monkeypatch.setattr(main.scraper, "fetch_weather", lambda lat, lon: None)

    class DeadBrain(StratumBrain):
        def is_configured(self):
            return True
        def extract_market_context(self, raw, sport):
            raise BrainUnavailableError("both backends down")

    _, _, parsed, warnings = main.gather_live_context("KC @ BAL", "NFL", None, None, DeadBrain())
    assert any("brain unavailable" in w.lower() for w in warnings)
    assert parsed is not None
    assert parsed["ml_home_american"] == -150
    assert parsed["ml_away_american"] == 130


def test_full_offline_pipeline_end_to_end(monkeypatch):
    """Context fetch -> regex parse -> analyze_market -> 57.98/42.02, no LLM needed."""
    text = "Chiefs -150 vs Ravens +130 tonight."
    monkeypatch.setattr(main.scraper, "fetch_match_context", lambda q: text)
    monkeypatch.setattr(main.scraper, "fetch_weather", lambda lat, lon: {"temp_c": 5.0, "wind_kmh": 10.0, "precip_mm": 0.0, "condition": "Clear sky"})
    brain = StratumBrain(groq_api_key="", google_api_key="")
    _, weather, parsed, _ = main.gather_live_context("Chiefs @ Ravens", "NFL", 1.0, 2.0, brain)
    assert weather["condition"] == "Clear sky"
    assert main.required_prices_missing(parsed) is False
    a = main.analyze_market(parsed["ml_home_american"], parsed["ml_away_american"])
    assert round(a["fair"]["home"] * 100, 2) == 57.98
    assert round(a["fair"]["away"] * 100, 2) == 42.02
