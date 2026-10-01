#!/usr/bin/env python3
"""EDGE ENGINE item (e) backend test: merge_model_into_fixture must propagate
sample_games + ensemble_kind onto EVERY row kind (matched book rows, unmatched
book rows, and MODEL-ONLY rows). Fully offline: mocked histories + fake provider.
Always exits 0 unless an assertion fails."""
import json, os, sys, types, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

# Import hunt_match with network/GitHub disabled at import time.
os.environ.setdefault("GITHUB_TOKEN", "")
import hunt_match as hm  # noqa: E402


def mock_hist(side):
    """12 completed games so sample_games >= 8 for the gate."""
    games = []
    for i in range(12):
        was_home = (i % 2 == 0) if side == "home" else (i % 2 == 1)
        gf, ga = (2, 1) if side == "home" else (1, 1)
        games.append({"date": f"2026-09-{10+i:02d}T19:00Z", "home": was_home,
                      "gf": gf, "ga": ga,
                      "opponent": "X" if side == "home" else "Y"})
    return games


def main():
    assert hm.QUANT_OK, "model_poisson/context_llm failed to import"

    # Force offline context LLM (no env keys in tests).
    fx = {"fixture_id": "test-1", "home": "Arsenal", "away": "Chelsea",
          "league": "eng.1", "sport": "soccer",
          "kickoff_utc": "2026-10-02T19:00Z", "team_ids": {}}

    # Monkeypatch history ingestion to mocks (fully offline).
    hm.model_poisson.ingest_team_history = lambda tid, sport, league=None: {
        "games": [], "sample_games": 0, "standings": None, "source": "test_off"}
    orig_build = hm.model_poisson.build_fixture_model
    hm.model_poisson.build_fixture_model = lambda fixture, team_ids=None, **kw: \
        orig_build(fixture, team_ids=team_ids,
                   mock_histories={"home": mock_hist("home"),
                                   "away": mock_hist("away")})

    fx["top_edges"] = [
        # matched book row (1X2 Home) — should get model fields
        {"market": "Match Result", "selection": "Home Team", "book_odds": 2.10,
         "source": "ESPN"},
        # unmatched book row — fields must still exist (None)
        {"market": "Totally Unknown Market", "selection": "Whatever",
         "book_odds": 3.00, "source": "ESPN"},
    ]
    tmp = tempfile.mkdtemp()
    hm.LEDGER_PATH = os.path.join(tmp, "ledger.jsonl")

    hm.merge_model_into_fixture(fx)

    rows = fx["_all_priced_rows"]
    assert rows, "no priced rows produced"
    kinds = {"book_matched": 0, "book_unmatched": 0, "model_only": 0}
    for r in rows:
        assert "sample_games" in r, f"missing sample_games: {r['market']}/{r['selection']}"
        assert "ensemble_kind" in r, f"missing ensemble_kind: {r['market']}/{r['selection']}"
        if r.get("source") == "MODEL":
            kinds["model_only"] += 1
            assert r["book_odds"] is None and r["label"] == "MODEL-ONLY (no line yet)"
        elif r.get("edge_vs_model") is not None:
            kinds["book_matched"] += 1
            assert r["sample_games"] is not None and r["ensemble_kind"], \
                f"matched row lacks propagated fields: {r}"
        else:
            kinds["book_unmatched"] += 1
            assert r["sample_games"] is None and r["ensemble_kind"] is None

    assert kinds["book_matched"] >= 1, "expected a matched 1X2 book row"
    assert kinds["model_only"] >= 1, "expected MODEL-ONLY rows"

    # Gate sanity: matched rows carry real numbers usable by the UI filter
    mrow = next(r for r in rows if r.get("edge_vs_model") is not None)
    assert isinstance(mrow["sample_games"], int) and mrow["sample_games"] >= 8
    assert mrow["ensemble_kind"] in ("model", "derived", "poisson-only")

    cov = fx["model_coverage"]
    print(json.dumps({"coverage": cov, "row_kinds": kinds,
                      "matched_sample": {"sample_games": mrow["sample_games"],
                                          "ensemble_kind": mrow["ensemble_kind"],
                                          "edge": mrow["edge_vs_model"]}}, indent=1))
    print("PASS: sample_games + ensemble_kind present on every row kind")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
