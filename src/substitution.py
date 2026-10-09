"""PROF-DASH item 4 — MARKET SUBSTITUTION LADDERS. If a primary market is unavailable or
lacks edge, automatically walk its fallback ladder and return the highest-hit-rate available
line. Never raises."""
from softline_protocol import get_gates, check_gates, load_thresholds


LADDERS = {
    "soccer_asian_over_175": ["soccer_tree_over15_under35", "soccer_team_to_score", "soccer_match_under_45"],
    "soccer_match_under_45": ["soccer_tree_over15_under35", "soccer_btts_no", "soccer_dnb"],
    "soccer_team_total_over_10": ["soccer_team_to_score", "soccer_dc_cushion", "soccer_tree_over15_under35"],
    "bb_alt_total_cushion": ["bb_first_half_total", "bb_team_total_push", "bb_ml_favorite"],
    "bb_alt_total_cushion_under": ["bb_first_half_total", "bb_team_total_push", "bb_ml_favorite"],
    "nfl_team_total_push": ["nfl_alt_spread_cushion", "nfl_first_half_total", "nfl_ml_favorite"],
    "mlb_team_total_push": ["mlb_first5_total", "mlb_alt_runline_cushion", "mlb_ml_favorite"],
    "nhl_alt_total_cushion": ["nhl_team_total_push", "nhl_ml_incl_ot"],
}
# taxonomy-facing aliases so ladders map to data/markets_taxonomy.csv rows
LABELS = {  # maps ladder rungs -> data/markets_taxonomy.csv rows
    "soccer_tree_over15_under35": "Over/Under Total Goals (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)",
    "soccer_team_to_score": "Team To Score Anytime", "soccer_match_under_45": "Over/Under Total Goals 4.5 Under",
    "soccer_btts_no": "Both Teams to Score (BTTS)", "soccer_dnb": "Draw No Bet",
    "soccer_dc_cushion": "Double Chance", "soccer_asian_over_175": "Asian Total Over 1.75",
    "soccer_team_total_over_10": "Team Total Goals Over 1.0",
    "bb_first_half_total": "1st Half Game Total", "bb_team_total_push": "Team Total Points",
    "bb_ml_favorite": "Moneyline (1X2)", "bb_alt_total_cushion": "Alternative Game Total",
    "nfl_alt_spread_cushion": "Alternative Spread", "nfl_first_half_total": "1st Half Total Points",
    "nfl_ml_favorite": "Moneyline (1X2)", "nfl_team_total_push": "Team Total Points",
    "mlb_first5_total": "First 5 Innings Total", "mlb_alt_runline_cushion": "Run Line",
    "mlb_ml_favorite": "Moneyline (1X2)", "mlb_team_total_push": "Team Total Runs",
    "nhl_team_total_push": "Team Total Goals", "nhl_ml_incl_ot": "Moneyline incl. OT",
    "nhl_alt_total_cushion": "Alternative Game Total",
}


def _num(x, default=0.0):
    try:
        v = float(x)
        return v if v == v else default
    except Exception:
        return default


def best_available(primary, candidates_by_class, thresholds=None):
    """candidates_by_class: {class_key: row_with_p_eff_edge_price_gates}. Walk ladder; first
    rung that EXISTS and passes its gates wins; among qualified rungs pick highest p_eff
    (hit-rate), tie-break edge. Returns (row|None, ladder_walked[], reason)."""
    try:
        th = thresholds if thresholds is not None else load_thresholds()
        ladder = LADDERS.get(primary, [])
        walked = []
        qualified = []
        for ck in ladder:
            row = candidates_by_class.get(ck)
            if not row:
                walked.append((ck, "unavailable"))
                continue
            passed, failed = check_gates(ck, row.get("features", row), th)
            walked.append((ck, "gates-pass" if passed else "gates-fail:" + ",".join(failed[:3])))
            if passed:
                qualified.append(row)
        if not qualified:
            return (None, walked, "no ladder rung qualified — T3 smell-test only")
        best = max(qualified, key=lambda r: (_num(r.get("p_eff")), _num(r.get("edge"))))
        return (best, walked, f"substituted {primary} -> {best.get('class_key')} (highest hit-rate)")
    except Exception:
        return (None, [], "substitution-error")


def selftest():
    ok = []
    cands = {
        "soccer_tree_over15_under35": {"class_key": "soccer_tree_over15_under35", "p_eff": 0.83, "edge": 0.03,
                          "features": {"form_std": 0.05, "avg_rate": 3.1, "line": 2.5, "side": "over",
                                       "model_prob": 0.83, "odds": 1.30, "sample_games": 12}},
        "soccer_team_to_score": {"class_key": "soccer_team_to_score", "p_eff": 0.91, "edge": 0.02,
                          "features": {"form_flag": True, "avg_rate": 1.9, "line": 1.0, "side": "over",
                                       "model_prob": 0.91, "odds": 1.16, "sample_games": 10}},
    }
    row, walked, reason = best_available("soccer_asian_over_175", cands, thresholds={})
    ok.append(("ladder returns a row when qualified", row is not None))
    ok.append(("walk log recorded", len(walked) >= 1))
    none_row, _, _ = best_available("soccer_ao_175", {}, thresholds={})
    ok.append(("empty candidates -> None", none_row is None))
    for n, c in ok:
        print(("PASS" if c else "FAIL"), n)
    return all(c for _, c in ok)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
