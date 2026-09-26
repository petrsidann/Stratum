"""Tests for src/llm_router.py — all LLM clients are monkeypatched.

Offline, zero API keys required. Verifies routing (Groq -> Gemini), fallbacks,
BrainUnavailableError, and the non-negotiable rule that the brain NEVER
computes: numbers not present in the source text are rejected as invalid.
"""

import json

import pytest

from src.llm_router import BrainUnavailableError, StratumBrain


GOOD_JSON = {
    "spread_home": -3.5,
    "spread_away": 3.5,
    "ml_home_american": -150,
    "ml_away_american": 130,
    "total_line": 45.5,
    "public_ticket_pct_home": 62,
    "sharp_signal": "rlm",
    "injury_notes": "Kelce questionable",
    "source_quotes": ["Chiefs are -150 favorites.", "The total is 45.5."],
}

SOURCE_TEXT = (
    "Chiefs are -150 favorites over Ravens at +130. Spread: -3.5. "
    "Total is 45.5. 62% of tickets on Kansas City. Kelce questionable."
)


class FakeGroqClient:
    def __init__(self, payload=None, exc=None):
        content = payload if isinstance(payload, str) else json.dumps(payload)
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        resp = type("R", (), {"choices": [choice]})()

        def _create(*args, **kwargs):
            if exc is not None:
                raise exc
            return resp

        completions = type("Comp", (), {"create": staticmethod(_create)})()
        self.chat = type("Chat", (), {"completions": completions})()


class FakeGenaiClient:
    def __init__(self, payload=None, exc=None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        resp = type("R", (), {"text": text})()

        def _gen(model=None, contents=None, config=None):
            if exc is not None:
                raise exc
            return resp

        self.models = type("Models", (), {"generate_content": staticmethod(_gen)})()


def make_brain(groq_payload=None, groq_exc=None, gemini_payload=None, gemini_exc=None):
    """Brain with both backends stubbed via method monkeypatching."""
    brain = StratumBrain(groq_api_key="test-groq", google_api_key="test-google")

    def _groq(prompt):
        if groq_exc is not None:
            raise groq_exc
        if groq_payload is None:
            raise RuntimeError("no canned groq payload")
        return json.loads(groq_payload) if isinstance(groq_payload, str) else groq_payload

    def _gemini(prompt):
        if gemini_exc is not None:
            raise gemini_exc
        if gemini_payload is None:
            raise RuntimeError("no canned gemini payload")
        return json.loads(gemini_payload) if isinstance(gemini_payload, str) else gemini_payload

    brain._groq = _groq
    brain._gemini = _gemini
    return brain


def test_groq_primary_success(monkeypatch):
    calls = []
    brain = StratumBrain(groq_api_key="k", google_api_key=None)

    def fake_groq(prompt):
        calls.append("groq")
        return dict(GOOD_JSON)

    brain._groq = fake_groq
    out = brain.extract_market_context(SOURCE_TEXT, "NFL")
    assert calls == ["groq"]
    assert out["ml_home_american"] == -150
    assert out["ml_away_american"] == 130
    assert out["total_line"] == 45.5
    assert out["public_ticket_pct_home"] == 62
    assert out["sharp_signal"] == "rlm"
    assert out["injury_notes"] == "Kelce questionable"
    assert out["source_quotes"] == GOOD_JSON["source_quotes"]


def test_groq_exception_falls_back_to_gemini():
    brain = make_brain(groq_exc=RuntimeError("429 rate limited"), gemini_payload=GOOD_JSON)
    out = brain.extract_market_context(SOURCE_TEXT, "NFL")
    assert out["ml_home_american"] == -150  # came from Gemini


def test_invalid_json_from_groq_triggers_gemini():
    brain = make_brain(groq_payload="{not valid json", gemini_payload=GOOD_JSON)
    out = brain.extract_market_context(SOURCE_TEXT, "NFL")
    assert out["spread_home"] == -3.5


def test_both_fail_raises_brain_unavailable():
    brain = make_brain(groq_exc=RuntimeError("groq down"), gemini_exc=RuntimeError("gemini down"))
    with pytest.raises(BrainUnavailableError):
        brain.extract_market_context(SOURCE_TEXT, "NFL")


def test_is_configured():
    assert StratumBrain(groq_api_key="x", google_api_key="").is_configured() is True
    assert StratumBrain(groq_api_key="", google_api_key="y").is_configured() is True
    assert StratumBrain(groq_api_key="", google_api_key="").is_configured() is False


def test_extract_never_performs_arithmetic_no_numbers_in_text():
    """Text with NO digits: every numeric field must come back null."""
    brain = make_brain(groq_payload={**GOOD_JSON, "source_quotes": []})  # model tries to guess
    out = brain.extract_market_context("No prices or lines mentioned at all today.", "NFL")
    for key in ("spread_home", "spread_away", "ml_home_american",
                "ml_away_american", "total_line", "public_ticket_pct_home"):
        assert out[key] is None, f"{key} should be null when absent from text"


def test_hallucinated_number_not_in_text_is_nulled():
    """Rule 1: a number we didn't fetch is invalid — even if the rest is fine."""
    hallucinated = dict(GOOD_JSON, ml_home_american=-999)  # -999 not in SOURCE_TEXT
    brain = make_brain(groq_payload=hallucinated)
    out = brain.extract_market_context(SOURCE_TEXT, "NFL")
    assert out["ml_home_american"] is None
    assert out["ml_away_american"] == 130  # legit value survives


def test_invalid_sharp_signal_coerced_to_none():
    bad = dict(GOOD_JSON, sharp_signal="moonshot")
    brain = make_brain(groq_payload=bad)
    out = brain.extract_market_context(SOURCE_TEXT, "NFL")
    assert out["sharp_signal"] == "none"


def test_empty_text_raises_brain_unavailable_without_network():
    brain = StratumBrain(groq_api_key="k", google_api_key="k")
    with pytest.raises(BrainUnavailableError):
        brain.extract_market_context("", "NFL")


def test_real_backend_clients_are_monkeypatchable_classes():
    """Sanity: the fakes emulate the shapes _groq/_gemini expect from SDKs."""
    client = FakeGroqClient(payload=GOOD_JSON)
    resp = client.chat.completions.create(model="x")
    assert json.loads(resp.choices[0].message.content)["ml_home_american"] == -150
    g = FakeGenaiClient(payload=GOOD_JSON)
    assert json.loads(g.models.generate_content().text)["total_line"] == 45.5


def test_groq_key_check_happens_before_sdk_import():
    """No key -> RuntimeError raised before any network/SDK touch (offline-safe)."""
    brain = StratumBrain(groq_api_key="", google_api_key="")
    with pytest.raises(RuntimeError, match="Groq API key not configured"):
        brain._groq("prompt")
    with pytest.raises(RuntimeError, match="Google API key not configured"):
        brain._gemini("prompt")
