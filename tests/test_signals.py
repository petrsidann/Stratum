"""Tests for Phase 3 — Signal Detectors, Market Scanner, CLV Auditor.

All offline: mock snapshots and temp SQLite only. Zero network, zero keys.
Spec coverage:
  * synchronized Pinnacle/Circa/BetMGM movement -> detect_steam() True
  * public 75% Home + line moving away         -> detect_rlm() SHARP_ON_B
  * odds implying ~98% total                   -> arb margin ≈ 2%
Plus stale-line detection, graceful empty states, CLV math and the
performance report. No fabricated data anywhere.
"""

import pytest

import main
from src import database
from src.clv_auditor import (
    get_performance_report,
    list_bets,
    record_bet,
    settle_bet,
    update_closing_line,
)
from src.market_scanner import MARKET_ML, MARKET_PROPS, MarketScanner
from src.quant_engine import american_to_decimal, arbitrage_pct, clv_pct, implied_probability
from src.signal_detector import (
    detect_rlm,
    detect_steam,
    find_arbitrage_opportunities,
    scan_signals_for_match,
)


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    database.init_db(path)
    return path


# ---------------------------------------------------------------------------
# STEAM
# ---------------------------------------------------------------------------

PREV = {
    "timestamp": "2026-09-26T10:00:00+00:00",
    "books": {
        "Pinnacle": {"home_american": -140, "away_american": 120},
        "Circa": {"home_american": -138, "away_american": 122},
        "BetMGM": {"home_american": -135, "away_american": 125},
    },
}
CUR = {
    "timestamp": "2026-09-26T10:03:00+00:00",
    "books": {
        "Pinnacle": {"home_american": -155, "away_american": 135},
        "Circa": {"home_american": -152, "away_american": 132},
        "BetMGM": {"home_american": -150, "away_american": 130},
    },
}


def test_detect_steam_true_on_synchronized_major_movement():
    assert detect_steam(CUR, [PREV]) is True


def test_detect_steam_false_when_movement_outside_window():
    stale_prev = dict(PREV, timestamp="2026-09-26T09:00:00+00:00")
    assert detect_steam(CUR, [stale_prev]) is False


def test_detect_steam_false_when_only_one_book_moves():
    partial = {
        "timestamp": "2026-09-26T10:03:00+00:00",
        "books": {
            "Pinnacle": {"home_american": -155, "away_american": 135},
            "Circa": {"home_american": -138, "away_american": 122},   # unchanged
            "BetMGM": {"home_american": -135, "away_american": 125},  # unchanged
        },
    }
    assert detect_steam(partial, [PREV]) is False


def test_detect_steam_false_without_history():
    assert detect_steam(CUR, []) is False


# ---------------------------------------------------------------------------
# RLM
# ---------------------------------------------------------------------------

def test_detect_rlm_sharp_on_b_when_public_home_line_moves_away():
    # Public 75% on Team A (Home), line moved toward Team B => sharp on B.
    assert detect_rlm(75.0, -1) == "SHARP_ON_B"


def test_detect_rlm_none_when_line_follows_public():
    assert detect_rlm(75.0, 1) == "NONE"


def test_detect_rlm_none_below_public_threshold():
    assert detect_rlm(60.0, -1) == "NONE"   # must be STRICTLY > 60%
    assert detect_rlm(55.0, -1) == "NONE"


def test_detect_rlm_handles_garbage_gracefully():
    assert detect_rlm(None, -1) == "NONE"
    assert detect_rlm("abc", -1) == "NONE"
    assert detect_rlm(75.0, None) == "NONE"


# ---------------------------------------------------------------------------
# ARBITRAGE
# ---------------------------------------------------------------------------

def test_arb_margin_is_2pct_when_implied_sums_to_98():
    # Construct two prices whose implied probabilities sum to exactly 0.98:
    # +100 => 50%, and p = 0.48 => decimal 2.0833 => American +108.33.
    home, away = 100.0, 108.333333
    total = implied_probability(home) + implied_probability(away)
    assert total == pytest.approx(0.98, abs=1e-4)
    assert find_arbitrage_opportunities(home, away) == pytest.approx(2.0, abs=0.05)


def test_no_arb_when_implied_sum_exceeds_one():
    assert find_arbitrage_opportunities(-150, 130) == 0.0
    assert arbitrage_pct(-110, -110) == 0.0


def test_arb_zero_on_missing_prices():
    assert find_arbitrage_opportunities(None, 130) == 0.0
    assert find_arbitrage_opportunities(0, 0) == 0.0


# ---------------------------------------------------------------------------
# scan_signals_for_match aggregator
# ---------------------------------------------------------------------------

def test_scan_signals_aggregates_all_three():
    res = scan_signals_for_match(
        [PREV, CUR],
        public_ticket_pct_home=75.0,
        opening_home_american=-140,   # open
        current_home_american=-155,   # shortened toward home... wait: decimal down => moved WITH public
        best_home=100.0, best_away=108.333333,
    )
    assert res["steam"] is True
    assert res["arb_pct"] == pytest.approx(2.0, abs=0.05)
    # Line moved TOWARD home (public side) => no RLM flag.
    assert res["rlm"] == "NONE"


def test_scan_signals_flags_rlm_when_line_reverses():
    res = scan_signals_for_match(
        [], public_ticket_pct_home=75.0,
        opening_home_american=-150, current_home_american=-135,  # home decimal lengthened
    )
    assert res["rlm"] == "SHARP_ON_B"


def test_scan_signals_never_fabricates_without_inputs():
    res = scan_signals_for_match([])
    assert res["steam"] is False and res["rlm"] == "NONE" and res["arb_pct"] == 0.0


# ---------------------------------------------------------------------------
# Market scanner
# ---------------------------------------------------------------------------

def test_scan_match_returns_sample_board_labeled_when_fetch_fails():
    rows = MarketScanner(fetcher=lambda q: "").scan_match("Chiefs @ Ravens")
    assert len(rows) >= 20  # ML/Spread/Total/Halves/Quarters/Props/Alternates
    assert all(r["data_source"] == "sample" for r in rows)
    props = [r for r in rows if r["market_type"] == MARKET_PROPS]
    assert props and all("QB" in r["selection"] or "RB" in r["selection"] for r in props)


def test_scan_match_parses_live_feed_rows():
    feed = "ML : Home @ Pinnacle : -150\nSpread : Home @ Circa : -110 line -3.5\n"
    rows = MarketScanner(fetcher=lambda q: feed).scan_match("X @ Y")
    assert rows and all(r["data_source"] == "live" for r in rows)
    assert rows[0]["american_odds"] == -150
    assert rows[1]["line"] == -3.5


def test_compare_books_flags_stale_spread_and_price():
    scanner = MarketScanner(fetcher=lambda q: "")
    rows = scanner.scan_match("A @ B")
    res = scanner.compare_books(rows)
    kinds = {(f["market_type"], f["kind"]) for f in res["stale_flags"]}
    assert ("Spread", "line") in kinds          # BetMGM lags by 2 pts > 1.5 threshold
    assert ("ML", "price") in kinds             # +140 vs consensus +132 > 5%
    for f in res["stale_flags"]:
        assert f["bookmaker"] and f["reason"]


def test_compare_books_detects_arb_across_best_ml_sides():
    rows = [
        {"market_type": MARKET_ML, "selection": "Home", "bookmaker": "Pinnacle", "american_odds": 100, "line": None},
        {"market_type": MARKET_ML, "selection": "Away", "bookmaker": "Circa", "american_odds": 108.333333, "line": None},
    ]
    res = MarketScanner().compare_books(rows)
    assert len(res["arb_flags"]) == 1
    assert res["arb_flags"][0]["arb_pct"] == pytest.approx(2.0, abs=0.05)


def test_rank_value_spots_sorts_by_ev_via_quant_engine():
    _, prob_map = main.build_fair_probs(-150, 130)
    rows = [
        {"market_type": MARKET_ML, "selection": "Home", "bookmaker": "Soft", "american_odds": -135},
        {"market_type": MARKET_ML, "selection": "Home", "bookmaker": "Sharp", "american_odds": -160},
        {"market_type": "Prop", "selection": "Unknown thing", "bookmaker": "X", "american_odds": 100},
    ]
    ranked = main.rank_value_spots(rows, prob_map)
    assert ranked[0]["bookmaker"] == "Soft"           # better price on same side = higher EV
    assert ranked[0]["ev_pct"] > ranked[1]["ev_pct"]
    assert ranked[-1]["ev_pct"] is None               # unknown prob -> Unknown, never guessed


# ---------------------------------------------------------------------------
# CLV auditor
# ---------------------------------------------------------------------------

def test_record_bet_inserts_open_row(db):
    bid = record_bet("A @ B", "ML", "Home", -150, 100, db_path=db)
    row = next(b for b in list_bets(db_path=db) if b["id"] == bid)
    assert row["status"] == "open" and row["closing_odds"] is None


def test_record_bet_rejects_bad_input(db):
    with pytest.raises(ValueError):
        record_bet("A @ B", "ML", "Home", 0, 100, db_path=db)      # odds 0 is not a price
    with pytest.raises(ValueError):
        record_bet("A @ B", "ML", "Home", -150, 0, db_path=db)     # zero stake


def test_update_closing_line_beat_the_close_is_positive(db):
    bid = record_bet("A @ B", "ML", "Home", -150, 100, db_path=db)
    v = update_closing_line(bid, -140, db_path=db)                 # improved payout at close
    expected = (american_to_decimal(-140) / american_to_decimal(-150) - 1.0) * 100.0
    assert v == pytest.approx(expected, rel=1e-6)
    assert v == pytest.approx(2.857, abs=0.01)                     # beat the close
    assert clv_pct(-150, -140) > 0


def test_update_closing_line_bad_close_is_negative(db):
    bid = record_bet("A @ B", "ML", "Home", -150, 100, db_path=db)
    v = update_closing_line(bid, -170, db_path=db)
    assert v < 0                                                   # behind the close


def test_update_closing_line_unknown_bet_raises(db):
    with pytest.raises(KeyError):
        update_closing_line(9999, -120, db_path=db)


def test_performance_report_aggregates_roi_winrate_clv(db):
    b1 = record_bet("A @ B", "ML", "Home", -150, 100, db_path=db)
    b2 = record_bet("A @ B", "Spread", "Away +3.5", -110, 50, db_path=db)
    update_closing_line(b1, -140, db_path=db)     # +2.857% CLV
    update_closing_line(b2, -125, db_path=db)     # -5.714% CLV
    settle_bet(b1, "won", db_path=db)
    settle_bet(b2, "lost", db_path=db)
    rep = get_performance_report(days=30, db_path=db)
    assert rep["has_data"] and rep["n_bets"] == 2 and rep["n_settled"] == 2
    assert rep["win_rate_pct"] == pytest.approx(50.0)
    # profit +66.67 on 100 staked, -50 on 50 staked => ROI = 16.67/150
    assert rep["roi_pct"] == pytest.approx(11.11, abs=0.05)
    assert rep["avg_clv_pct"] == pytest.approx((2.857 - 5.714) / 2, abs=0.01)
    assert rep["beat_close_rate_pct"] == pytest.approx(50.0)
    assert rep["best_market"]["market"] == "ML"
    assert rep["worst_market"]["market"] == "Spread"


def test_performance_report_empty_window_is_honest(db):
    rep = get_performance_report(days=30, db_path=db)
    assert rep["has_data"] is False and rep["n_bets"] == 0
    assert rep["avg_clv_pct"] is None


def test_backward_compatible_schema_keeps_phase1_tables(db):
    conn = database.get_connection(db)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"games", "odds", "bets", "bets_log", "scans"} <= tables
