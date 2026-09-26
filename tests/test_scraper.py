"""Tests for src/scraper.py — requests.get/post fully monkeypatched.

Offline, zero keys. Covers: Open-Meteo fixture parsing + bad payload -> None,
DDG good page / empty page / HTTP 500, and parse_odds_from_text returning
correct values or None (never a default number).
"""

import json

import pytest

from src import scraper


class FakeResponse:
    def __init__(self, text="", payload=None, status=200):
        self.text = text
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


OPEN_METEO_GOOD = {
    "current": {
        "temperature_2m": 12.5,
        "wind_speed_10m": 23.4,
        "precipitation": 0.8,
        "weather_code": 61,
    }
}

DDG_GOOD_HTML = """
<html><body>
<div class="result"><h2 class="result__title">Chiefs vs Ravens odds</h2>
<div class="result__snippet">Kansas City opened -150 at home, Baltimore +130. Spread -3.5, total 45.5.</div></div>
<div class="result"><h2 class="result__title">Line movement report</h2>
<div class="result__snippet">62% of tickets are on the Chiefs but the line moved the other way.</div></div>
<div class="result"><h2 class="result__title">Injury news</h2>
<div class="result__snippet">Travis Kelce is questionable with a knee injury.</div></div>
<div class="result"><h2 class="result__title">Noise</h2>
<div class="result__snippet">Fourth result should be ignored.</div></div>
</body></html>
"""

DDG_EMPTY_HTML = "<html><body><p>nothing here</p></body></html>"


def test_fetch_weather_parses_open_meteo_fixture(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        scraper.requests, "get",
        lambda url, **kw: FakeResponse(payload=OPEN_METEO_GOOD),
    )
    w = scraper.fetch_weather(39.0, -76.0)
    assert w == {"temp_c": 12.5, "wind_kmh": 23.4, "precip_mm": 0.8, "condition": "Slight rain"}


def test_fetch_weather_bad_payload_returns_none(monkeypatch):
    monkeypatch.setattr(scraper.requests, "get", lambda url, **kw: FakeResponse(payload={"current": {}}))
    assert scraper.fetch_weather(1.0, 2.0) is None

    def boom(url, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(scraper.requests, "get", boom)
    assert scraper.fetch_weather(1.0, 2.0) is None


def test_fetch_match_context_good_page(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)  # keep tests fast
    monkeypatch.setattr(scraper.requests, "post", lambda url, **kw: FakeResponse(text=DDG_GOOD_HTML))
    ctx = scraper.fetch_match_context("Chiefs @ Ravens")
    assert "Chiefs vs Ravens odds" in ctx
    assert "-150" in ctx
    # Top 3 only — the 4th "Noise" result must not appear.
    assert "Noise" not in ctx


def test_fetch_match_context_empty_page_returns_empty_string(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(scraper.requests, "post", lambda url, **kw: FakeResponse(text=DDG_EMPTY_HTML))
    assert scraper.fetch_match_context("Nothing @ Nothing") == ""


def test_fetch_match_context_http500_returns_empty_string(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        scraper.requests, "post",
        lambda url, **kw: FakeResponse(text="err", status=500),
    )
    assert scraper.fetch_match_context("Chiefs @ Ravens") == ""


def test_parse_odds_from_text_extracts_prices():
    out = scraper.parse_odds_from_text(
        "Kansas City opened -150 at home, Baltimore +130. Spread: -3.5. Total: 45.5."
    )
    assert out is not None
    assert out["ml_home_american"] == -150
    assert out["ml_away_american"] == 130
    assert out["spread_home"] == -3.5
    assert out["total_line"] == 45.5


def test_parse_odds_from_text_no_odds_returns_none():
    assert scraper.parse_odds_from_text("Great game yesterday, fans loved it.") is None
    assert scraper.parse_odds_from_text("") is None
    assert scraper.parse_odds_from_text("   ") is None


def test_parse_never_fills_defaults():
    """Only one side present -> the other stays None, never invented."""
    out = scraper.parse_odds_from_text("Ravens are +130 underdogs tonight.")
    assert out is not None
    assert out["ml_away_american"] == 130
    assert out["ml_home_american"] is None
    assert out["spread_home"] is None
    assert out["total_line"] is None


def test_polite_sleep_between_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(scraper.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(scraper.requests, "post", lambda url, **kw: FakeResponse(text=DDG_GOOD_HTML))
    scraper.fetch_match_context("Chiefs @ Ravens")
    assert slept and slept[0] >= 1.0
