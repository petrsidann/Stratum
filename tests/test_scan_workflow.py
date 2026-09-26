"""Stratum Market Scanner — GitHub Actions workflow tests

These tests guard the "Ghost Server" contract: the scheduled workflow must
produce strictly-validated JSON artifacts, degrade gracefully offline, and
never crash on partial failures. All network access is mocked - the suite
runs fully airgapped.

Usage::

    pip install -r requirements-dev.txt
    python -m pytest tests/test_scan_workflow.py -q

Covered contracts: validation rejects malformed docs (missing keys, bad
confidence), a full offline run still exits 0 with valid artifacts,
history.csv only receives above-threshold rows, the context cache serves
the second call within TTL without regenerating, and one exploding match
degrades (but never kills) the whole scan.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.run_daily_scan as rds  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect all module-level output paths into a tmp dir; no real files."""
    data = tmp_path / "data"
    cache = tmp_path / ".cache"
    monkeypatch.setattr(rds, "DATA_DIR", data)
    monkeypatch.setattr(rds, "SCAN_JSON", data / "latest_scan.json")
    monkeypatch.setattr(rds, "STATS_JSON", data / "portfolio_stats.json")
    monkeypatch.setattr(rds, "HISTORY_CSV", data / "history.csv")
    monkeypatch.setattr(rds, "CONTEXT_CACHE", cache / "context_cache.json")
    monkeypatch.setattr(rds, "CACHE_DIR", cache)
    # Silence scraper network entirely: every fetch raises like an airgap.
    import src.scraper as scraper
    def _offline(*a, **k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(scraper, "fetch_weather", _offline)
    monkeypatch.setattr(scraper, "fetch_match_context", _offline)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return tmp_path


def _good_doc():
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "status": "live",
        "summary": {"matches_scanned": 1},
        "results": [{
            "match_id": "X:A-B", "top_confidence": 70,
            "opportunities": [{"confidence": 70, "edge_pct": 1.5}],
        }],
    }


class TestValidation:
    def test_validate_scan_rejects_missing_keys(self):
        errs = rds.validate_scan({"status": "live"})
        assert any("schema_version" in e for e in errs)
        assert any("results" in e for e in errs)

    def test_validate_scan_rejects_bad_confidence(self):
        doc = _good_doc()
        doc["results"][0]["opportunities"][0]["confidence"] = 120
        assert any("out of range" in e for e in rds.validate_scan(doc))
        doc["results"][0]["opportunities"][0]["confidence"] = "high"
        assert rds.validate_scan(doc)

    def test_validate_scan_accepts_good_doc(self):
        assert rds.validate_scan(_good_doc()) == []


class TestEndToEndOffline:
    def test_main_writes_valid_artifacts_offline(self, sandbox, caplog):
        caplog.set_level(logging.INFO)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rds.main()
        assert rc == 0, "offline scan must still succeed (graceful degradation)"
        scan = json.loads((sandbox / "data/latest_scan.json").read_text())
        assert rds.validate_scan(scan) == []
        assert scan["status"] == "degraded"          # sample board = honest label
        assert scan["summary"]["matches_scanned"] == len(rds.DEFAULT_SLATE)
        stats = json.loads((sandbox / "data/portfolio_stats.json").read_text())
        assert stats["has_data"] is False            # empty ledger is a valid state
        assert (sandbox / "data/history.csv").exists()

    def test_history_append_only_above_threshold(self, sandbox):
        results = [{
            "scanned_at": "t", "match_id": "M", "sport": "NFL", "data_source": "sample",
            "opportunities": [
                {"market_type": "ML", "selection": "Home", "offered_odds": -150,
                 "fair_odds": 160.0, "edge_pct": 3.0, "confidence": 90, "above_threshold": True},
                {"market_type": "ML", "selection": "Away", "offered_odds": 130,
                 "fair_odds": 120.0, "edge_pct": -1.0, "confidence": 10, "above_threshold": False},
            ],
        }]
        n = rds.append_history(results)
        assert n == 1
        lines = (sandbox / "data/history.csv").read_text().strip().splitlines()
        assert len(lines) == 2 and "Home" in lines[1] and "Away" not in lines[1]
        # Append mode: second call adds exactly one more row.
        rds.append_history(results)
        assert len((sandbox / "data/history.csv").read_text().strip().splitlines()) == 3

    def test_context_cache_second_call_hits_cache(self, sandbox, monkeypatch):
        calls = {"n": 0}
        orig_write = Path.write_text
        def counting_write(self, *a, **k):
            calls["n"] += 1
            return orig_write(self, *a, **k)
        monkeypatch.setattr(Path, "write_text", counting_write)
        first = rds.get_context()
        second = rds.get_context()
        assert first["fresh"] is False and second["fresh"] is True
        assert calls["n"] == 1, "second call within TTL must not regenerate context"

    def test_per_match_failure_does_not_kill_run(self, sandbox, monkeypatch):
        boom = {"NFL:KC-BUF"}

        class FragileScanner:
            def scan_match(self, match_id, sport=""):
                if match_id in boom:
                    raise RuntimeError("simulated upstream explosion")
                return []
            def compare_books(self, rows):
                return {"groups": [], "stale_flags": [], "arb_flags": []}

        monkeypatch.setattr("src.market_scanner.MarketScanner", lambda *a, **k: FragileScanner())
        rc = rds.main()
        assert rc == 0                                  # workflow stays green
        scan = json.loads((sandbox / "data/latest_scan.json").read_text())
        assert scan["summary"]["matches_failed"] == 1
        assert scan["summary"]["failed_match_ids"] == ["NFL:KC-BUF"]
        assert scan["status"] == "degraded"             # but honestly labeled
