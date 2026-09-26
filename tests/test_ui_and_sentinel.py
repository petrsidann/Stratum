"""Phase 4 tests — design system, Sentinel watcher and the app shell.

Three guarantees:
  1. ui_theme builds valid, emoji-free HTML/CSS (design constraints are
     machine-checked here, not by vibes).
  2. SentinelThread fires send_alert() when fake odds jump >= 1.5 points or
     an arb appears — with a mocked scanner and injected notifier, zero
     network, zero real webhooks.
  3. main.py's pure view-model + lifecycle helpers initialize correctly
     against an EMPTY database.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import main
from src import live_watcher, ui_theme
from src.database import get_connection, init_db


# ---------------------------------------------------------------------------
# Fixtures & factories
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    path = str(tmp_path / "sentinel_test.db")
    init_db(path)
    live_watcher.ensure_schema(path)
    return path


def quote(market="ML", selection="Home", book="Pinnacle", odds=-110, line=None,
          source="live"):
    return {
        "market_type": market, "selection": selection, "bookmaker": book,
        "american_odds": odds, "line": line, "data_source": source,
        "timestamp": "2026-09-26T10:00:00+00:00",
    }


class FakeScanner:
    """Deterministic stand-in for MarketScanner — returns scripted rows."""

    def __init__(self):
        self.rows = []
        self.calls = 0

    def scan_match(self, match_id, sport="NFL"):
        self.calls += 1
        return [dict(r) for r in self.rows]


def make_sentinel(db_path, scanner, notifier, webhook="https://fake.hook/x"):
    return live_watcher.SentinelThread(
        tracked_matches=["Fake @ Rival"], db_path=db_path, scanner=scanner,
        notifier=notifier, webhook_url=webhook, interval=0.05,
    )


# ---------------------------------------------------------------------------
# ui_theme — design system contract
# ---------------------------------------------------------------------------

class TestUiTheme:
    def test_palette_tokens_exact(self):
        assert ui_theme.COLORS["bg"] == "#0E1116"
        assert ui_theme.COLORS["positive"] == "#2EE6A6"
        assert ui_theme.COLORS["negative"] == "#FF4D4D"
        assert ui_theme.COLORS["text_secondary"] == "#8B9BB4"
        assert ui_theme.COLORS["surface"] == "#161B22"
        assert ui_theme.COLORS["border"] == "#30363D"

    def test_inject_css_structure_and_targets(self):
        css = ui_theme.inject_css()
        assert css.startswith("<style>") and css.endswith("</style>")
        # Targets required by the spec: header hidden, buttons as pills,
        # tabs restyled, monospace numerals, mobile breakpoint.
        assert '[data-testid="stHeader"]' in css
        assert ".stButton > button" in css
        assert ".stTabs" in css
        assert "Roboto Mono" in css
        assert "@media (max-width: 768px)" in css
        assert "#0E1116" in css and "#2EE6A6" in css

    def test_light_theme_swaps_tokens(self):
        light = ui_theme.inject_css(theme="light")
        assert "#F4F6F9" in light  # light canvas replaces near-black

    def test_metric_card_html_valid(self):
        html = ui_theme.metric_card_html("Net P&L", "$1,234.00", delta=42.0, trend="up")
        assert html.count('<div class="stratum-card">') == 1
        assert 'class="stratum-card-label"' in html
        assert 'class="stratum-card-value"' in html
        assert "\u25B2" in html                      # up-trend glyph
        assert ui_theme.COLORS["positive"] in html   # green for positive
        down = ui_theme.metric_card_html("EV", "-$80", delta=-80.0)
        assert ui_theme.COLORS["negative"] in down and "\u25BC" in down
        flat = ui_theme.metric_card_html("Count", "0", delta=None, trend="flat")
        assert 'stratum-card-delta' not in flat      # no delta row when nothing to show

    def test_metric_card_escapes_injection(self):
        html = ui_theme.metric_card_html("<script>x</script>", "<b>bold</b>")
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_header_and_bottom_nav(self):
        head = ui_theme.header_html("STRATUM", "QUANT EDGE TERMINAL", sentinel_running=True)
        assert "stratum-dot-on" in head and "STRATUM" in head
        nav = ui_theme.bottom_nav_html("scan")
        for label in ("SCAN", "PORTFOLIO", "ALERTS", "SETTINGS"):
            assert label in nav
        assert '<a class="stratum-nav-btn active" href="?view=scan"' in nav
        assert nav.count("<svg") == 4  # icons are SVG, never emoji

    def test_no_emoji_anywhere_in_design_system_output(self):
        blob = (ui_theme.inject_css()
                + ui_theme.header_html("t", "s", True)
                + ui_theme.bottom_nav_html("alerts")
                + ui_theme.metric_card_html("l", "v", 1.0, "up"))
        emoji = re.findall("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", blob)
        assert emoji == [], f"emoji found: {emoji}"

    def test_formatters(self):
        assert ui_theme.fmt_odds(130) == "+130"
        assert ui_theme.fmt_odds(-150) == "-150"
        assert ui_theme.fmt_odds(None) == "\u2014"
        assert ui_theme.money(1234.5) == "$1,234.50"
        assert ui_theme.money(-80, sign=True) == "-\u200a$80.00"
        assert ui_theme.signed(2.439) == "+2.44"


# ---------------------------------------------------------------------------
# live_watcher — the Sentinel engine
# ---------------------------------------------------------------------------

class TestSentinelDetection:
    def test_line_shift_over_threshold_fires_single_grouped_event(self, tmp_db):
        scanner = FakeScanner()
        sent_events = []
        sentinel = make_sentinel(tmp_db, scanner, lambda m, s: sent_events.append((m, s)) or True)

        scanner.rows = [quote("Spread", "Home", line=-3.0), quote("Spread", "Away", line=3.0)]
        assert sentinel.one_cycle() == []           # baseline: silent, honest

        scanner.rows = [quote("Spread", "Home", line=-6.5), quote("Spread", "Away", line=6.5)]
        events = sentinel.one_cycle()               # 3.5-pt jump >= 1.5 threshold
        assert len(events) == 1                     # both sides merge into ONE alert
        assert events[0]["kind"] == "line_shift"
        assert abs(events[0]["delta_points"]) == 3.5
        assert any("LINE SHIFT" in m for m, _ in sent_events)

    def test_small_move_below_threshold_is_silent(self, tmp_db):
        scanner = FakeScanner()
        calls = []
        sentinel = make_sentinel(tmp_db, scanner, lambda m, s: calls.append(m) or True)
        scanner.rows = [quote("Spread", "Home", line=-3.0)]
        sentinel.one_cycle()
        scanner.rows = [quote("Spread", "Home", line=-3.5)]  # 0.5 < 1.5
        assert sentinel.one_cycle() == []
        assert calls == []

    def test_arb_opportunity_triggers_alert(self, tmp_db):
        scanner = FakeScanner()
        calls = []
        sentinel = make_sentinel(tmp_db, scanner, lambda m, s: calls.append((m, s)) or True)
        scanner.rows = [quote("ML", "Home", "Pinnacle", 105),
                        quote("ML", "Away", "BetMGM", 105)]  # implied 97.6% -> arb
        events = sentinel.one_cycle()
        arb = [e for e in events if e["kind"] == "arb"]
        assert arb and arb[0]["severity"] == live_watcher.SEVERITY_ALERT
        assert any("ARB LIVE" in m for m, _ in calls)

    def test_send_alert_persists_even_when_delivery_fails(self, tmp_db):
        sentinel = make_sentinel(tmp_db, FakeScanner(),
                                 lambda m, s: False)  # webhook rejects
        ok = sentinel.send_alert("manual drill", severity="info", kind="test")
        assert ok is False
        rows = live_watcher.list_alerts(db_path=tmp_db)
        assert len(rows) == 1 and rows[0]["delivered"] == 0
        assert rows[0]["message"] == "manual drill"

    def test_thread_lifecycle_start_and_graceful_stop(self, tmp_db):
        scanner = FakeScanner()
        scanner.rows = [quote("ML", "Home", "Pinnacle", -110)]
        sentinel = make_sentinel(tmp_db, scanner, lambda m, s: True)
        sentinel.start()
        assert sentinel.running
        sentinel.stop(timeout=5)
        assert not sentinel.is_alive()
        assert sentinel.cycles_done >= 1
        assert scanner.calls >= 1

    def test_singleton_manager_restart(self, tmp_db):
        first = live_watcher.start_sentinel(["A @ B"], db_path=tmp_db,
                                            scanner=FakeScanner(), interval=0.05)
        assert live_watcher.get_sentinel() is first and first.running
        second = live_watcher.start_sentinel(["C @ D"], db_path=tmp_db,
                                             scanner=FakeScanner(), interval=0.05)
        assert not first.is_alive()                  # old one stopped cleanly
        assert live_watcher.get_sentinel() is second
        live_watcher.stop_sentinel()
        assert live_watcher.get_sentinel() is None

    def test_post_webhook_never_raises_on_garbage(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("offline")

        monkeypatch.setattr(requests, "post", boom)
        assert live_watcher.post_webhook("https://unreachable.example", "hi") is False
        assert live_watcher.post_webhook("", "hi") is False  # unconfigured = no-op

    def test_detect_line_shifts_pure_unit(self):
        prev = {"Spread|Home": {"consensus_line": 2.5, "best_american": -110}}
        curr = {"Spread|Home": {"consensus_line": 4.5, "best_american": -110}}
        events = live_watcher.detect_line_shifts(prev, curr)
        assert len(events) == 1 and events[0]["delta_points"] == 2.0
        # First sight of a market is NOT movement (no fabricated history).
        assert live_watcher.detect_line_shifts({}, curr) == []


# ---------------------------------------------------------------------------
# main.py — app shell wiring against an EMPTY database
# ---------------------------------------------------------------------------

class TestAppShellInit:
    def test_empty_db_initializes_and_helpers_survive(self, tmp_db, monkeypatch):
        # Boot the DB exactly like ensure_app_boot does, then exercise every
        # pure view-model helper on empty input — none may crash.
        init_db(tmp_db)
        live_watcher.ensure_schema(tmp_db)
        conn = get_connection(tmp_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        finally:
            conn.close()

        assert main.best_ml_pair([]) == (None, None)
        assert main.opportunity_cards([], {}) == []
        assert main.filter_cards([], "ARB") == []
        assert main.ledger_with_pnl([]) == []
        assert main.cumulative_profit_figure([]).data == ()
        assert main.clv_histogram_figure([]).data == ()
        assert live_watcher.list_alerts(db_path=tmp_db) == []

    def test_opportunity_cards_from_real_rows(self):
        rows = [
            quote("ML", "Home", "Pinnacle", -105), quote("ML", "Away", "BetMGM", 105),
            quote("PlayerProps", "Mahomes TD", "DraftKings", -115, source="sample"),
        ]
        comparison = {
            "arb_flags": [{"side_a": "Home", "side_b": "Away", "arb_pct": 1.2}],
            "stale_flags": [{"market_type": "PlayerProps", "bookmaker": "DraftKings"}],
        }
        _, prob_map = main.build_fair_probs(-105, 105)
        cards = main.opportunity_cards(rows, comparison, {"prob_map": prob_map})
        assert len(cards) == 3
        assert cards[0]["arb"] and cards[0]["arb_pct"] == 1.2   # arb sorts first
        prop = next(c for c in cards if c["is_props"])
        assert prop["stale"] and prop["sample"] and prop["ev_pct"] is None

    def test_ledger_pnl_math(self):
        bets = [
            {"status": "won", "stake": 100.0, "odds_placed": 150, "created_at": "1"},
            {"status": "lost", "stake": 50.0, "odds_placed": -110, "created_at": "2"},
            {"status": "open", "stake": 75.0, "odds_placed": 120, "created_at": "3"},
        ]
        rows = main.ledger_with_pnl(bets)
        assert rows[0]["profit"] == pytest.approx(150.0)
        assert rows[1]["profit"] == pytest.approx(-50.0)
        assert rows[2]["profit"] == 0.0

    def test_dark_figures_use_stratum_palette(self):
        fig = main.cumulative_profit_figure([
            {"status": "won", "stake": 100.0, "odds_placed": 150,
             "created_at": "2026-09-01", "profit": 150.0},
        ])
        assert fig.layout.paper_bgcolor == ui_theme.COLORS["surface"]
        assert "Roboto Mono" in fig.layout.font.family          # monospace data discipline
        assert "Roboto Mono" in fig.layout.xaxis.tickfont.family
        assert len(fig.data) == 1

    def test_sync_sentinel_disabled_stops_singleton(self, tmp_db, monkeypatch):
        monkeypatch.setattr(live_watcher, "_SENTINEL", None)
        settings = {"sentinel_enabled": False, "tracked": [], "webhook_url": ""}
        assert main.sync_sentinel(settings) is False
        assert live_watcher.get_sentinel() is None

    def test_query_param_view_whitelist(self):
        assert set(ui_theme.DEFAULT_NAV_ORDER) == {"SCAN", "PORTFOLIO", "ALERTS", "SETTINGS"}
        assert set(main.VIEWS) == {"scan", "portfolio", "alerts", "settings"}

    def test_module_imports_cleanly_headless(self):
        # `import main` at module top already executed everything except the
        # Streamlit render path guards; assert the contract constants exist.
        assert live_watcher.POLL_INTERVAL_SECONDS == 30
        assert live_watcher.LINE_SHIFT_THRESHOLD == 1.5
        assert callable(main.render)
