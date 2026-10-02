#!/usr/bin/env python3
"""MULTI-SPORT SOFT-LINE phase item 2 — src/softline_protocol.py.

The soft-line market-class catalog: which exotic markets the swarm is even
ALLOWED to price, and the gates each class must clear before a line goes on
the board. Mapped from data/markets_taxonomy.csv; every class carries the
soccer-blueprint gate set {form, average, model_floor, odds_floor, edge_floor}
where:

  form        -> last-5 consistency requirement on the relevant rate
                 (std-dev ceiling of the per-game rates over the sample),
  average     -> combined model rate must clear the book line by this margin
                 (or cushion |line - model| >= value for alt lines),
  model_floor -> minimum model probability required,
  odds_floor  -> minimum acceptable decimal price (1.15-1.20 per class),
  edge_floor  -> minimum edge = p*odds - 1 required (baseline 0.02).

Design rules: stdlib only, never raises. Unknown sports/classes degrade to
"grey" (needs-data-source) rather than crashing a hunt. Item 3's self-trainer
overlays clamped adjustments onto model_floor / edge_floor via apply_thresholds().
"""

from __future__ import annotations

import csv
import json
import os

try:  # push-aware helpers come from item 1; degrade gracefully if absent
    import sport_models as _sm
except Exception:  # pragma: no cover
    try:
        from . import sport_models as _sm  # type: ignore
    except Exception:
        _sm = None

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "..", "data")
TAXONOMY_CSV = os.path.join(DATA_DIR, "markets_taxonomy.csv")
THRESHOLDS_JSON = os.path.join(DATA_DIR, "thresholds.json")


def log(msg):
    print(f"[SOFTLINE] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Gate helper
# ---------------------------------------------------------------------------

def gate(form=0.9, average=0.0, model_floor=0.70, odds_floor=1.18,
         edge_floor=0.02):
    return {"form": float(form), "average": float(average),
            "model_floor": float(model_floor), "odds_floor": float(odds_floor),
            "edge_floor": float(edge_floor)}


# ---------------------------------------------------------------------------
# MULTI-SPORT MARKET-CLASS CATALOG
# key -> {sport(s), taxonomy market names it maps to, blueprint, gates}
# ---------------------------------------------------------------------------

MARKET_CLASSES = {
    # ---------------- SOCCER (existing blueprint, kept verbatim) ----------
    "soccer_asian_over_175": {
        "sports": ("soccer",),
        "taxonomy": ("Asian Handicap", "Over/Under Total Goals"),
        "selection": "asian_over_1.75",
        "blueprint": "AO1.75: last-5 team-total scoring form + combined "
                     "mean goals vs 1.75 line; quarter-line so max half-push.",
        "gates": gate(form=0.90, average=0.25, model_floor=0.70,
                      odds_floor=1.15, edge_floor=0.02),
    },
    "soccer_match_under_45": {
        "sports": ("soccer",),
        "taxonomy": ("Over/Under Total Goals",),
        "selection": "under_4.5",
        "blueprint": "U4.5: both defences' last-5 conceded rates consistent "
                     "and combined mean total well under 4.5.",
        "gates": gate(form=0.85, average=0.60, model_floor=0.85,
                      odds_floor=1.16, edge_floor=0.02),
    },
    "soccer_team_total_over_10": {
        "sports": ("soccer",),
        "taxonomy": ("Team Total Goals (Over/Under)",),
        "selection": "team_over_1.0",
        "push": True,
        "blueprint": "TTO 1.0 integer push-aware: exact triplet, breakeven "
                     "price = 1 + p_loss/p_win.",
        "gates": gate(form=0.85, average=0.15, model_floor=0.75,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "soccer_tree_over15_under35": {
        "sports": ("soccer",),
        "taxonomy": ("Over/Under Total Goals", "Multi-Goals"),
        "selection": "over_1.5_and_under_3.5",
        "blueprint": "Over1.5 -> Under3.5 decision tree: enter only when "
                     "P(O1.5) clears floor, then price U3.5 conditional leg.",
        "gates": gate(form=0.85, average=0.30, model_floor=0.78,
                      odds_floor=1.18, edge_floor=0.02),
    },
    # ---------------- BASKETBALL ------------------------------------------
    "bb_alt_total_cushion": {
        "sports": ("basketball", "nba"),
        "taxonomy": ("Alternative Totals",),
        "selection": "alt_total",
        "cushion": 6.0,   # |line - model_total| >= 6 points
        "blueprint": "Alt total cushion: only price alt lines 6+ points away "
                     "from the model projection (normal sum distribution).",
        "gates": gate(form=0.80, average=6.0, model_floor=0.82,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "bb_alt_spread_cushion": {
        "sports": ("basketball", "nba"),
        "taxonomy": ("Alternative Spreads", "Point Spread (Handicap)"),
        "selection": "alt_spread",
        "cushion": 8.0,   # |alt_line - model_margin| >= 8
        "blueprint": "Alt spread cushion >= 8 vs model expected margin.",
        "gates": gate(form=0.80, average=8.0, model_floor=0.80,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "bb_team_total_push": {
        "sports": ("basketball", "nba"),
        "taxonomy": ("Team Total Points (Over/Under)",),
        "selection": "team_total_integer",
        "push": True,
        "blueprint": "Integer Team Total Points with exact push mass; "
                     "breakeven = 1 + p_loss/p_win.",
        "gates": gate(form=0.80, average=0.5, model_floor=0.75,
                      odds_floor=1.20, edge_floor=0.02),
    },
    # ---------------- NFL --------------------------------------------------
    "nfl_team_total_push": {
        "sports": ("nfl", "football", "american_football"),
        "taxonomy": ("Team Total Points (Over/Under)",),
        "selection": "team_total_integer",
        "push": True,
        "blueprint": "NFL integer Team Total push-aware triplet.",
        "gates": gate(form=0.80, average=0.5, model_floor=0.75,
                      odds_floor=1.20, edge_floor=0.02),
    },
    "nfl_alt_spread_cushion": {
        "sports": ("nfl", "football", "american_football"),
        "taxonomy": ("Alternative Point Spreads", "Point Spread (Handicap)"),
        "selection": "alt_spread",
        "cushion": 7.0,
        "blueprint": "NFL alt spread cushion >= 7 vs model margin (sigma 13).",
        "gates": gate(form=0.80, average=7.0, model_floor=0.80,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "nfl_first_half_total": {
        "sports": ("nfl", "football", "american_football"),
        "taxonomy": ("1st Half Total Points",),
        "selection": "first_half_total",
        "blueprint": "1H totals are notoriously soft lines: scale full-game "
                     "normal sum by half factor, demand wide cushion.",
        "gates": gate(form=0.75, average=3.5, model_floor=0.78,
                      odds_floor=1.15, edge_floor=0.02),
    },
    # ---------------- MLB ---------------------------------------------------
    "mlb_team_total_push": {
        "sports": ("mlb", "baseball"),
        "taxonomy": ("Team Total Runs (Over/Under)",),
        "selection": "team_total_integer",
        "push": True,
        "blueprint": "MLB integer Team Total runs, Poisson k=5 shrunk lambdas, "
                     "exact push mass.",
        "gates": gate(form=0.80, average=0.5, model_floor=0.75,
                      odds_floor=1.20, edge_floor=0.02),
    },
    "mlb_alt_runline_cushion": {
        "sports": ("mlb", "baseball"),
        "taxonomy": ("Alternative Run Lines", "Run Line (Handicap)"),
        "selection": "alt_runline",
        "cushion": 1.5,
        "blueprint": "Alt run-line cushions vs model expected margin.",
        "gates": gate(form=0.80, average=1.5, model_floor=0.80,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "mlb_first5_total": {
        "sports": ("mlb", "baseball"),
        "taxonomy": ("5-Innings Total Runs",),
        "selection": "first5_total",
        "blueprint": "First-5 innings total: 5/9ths of shrunk team lambdas, "
                     "integer push handled exactly.",
        "gates": gate(form=0.80, average=0.4, model_floor=0.78,
                      odds_floor=1.15, edge_floor=0.02),
    },
    # ---------------- NHL ---------------------------------------------------
    "nhl_alt_total_cushion": {
        "sports": ("nhl", "hockey"),
        "taxonomy": ("Alternative Puck Lines / Totals", "Total Goals Over/Under"),
        "selection": "alt_total",
        "cushion": 1.5,   # |line - model_total| >= 1.5 goals
        "blueprint": "NHL alt total cushion >= 1.5 goals vs Poisson model "
                     "total lambda.",
        "gates": gate(form=0.80, average=1.5, model_floor=0.82,
                      odds_floor=1.18, edge_floor=0.02),
    },
    "nhl_team_total_push": {
        "sports": ("nhl", "hockey"),
        "taxonomy": ("Team Total Goals (Over/Under)",),
        "selection": "team_total_integer",
        "push": True,
        "blueprint": "NHL integer Team Total goals push-aware triplet.",
        "gates": gate(form=0.80, average=0.3, model_floor=0.75,
                      odds_floor=1.20, edge_floor=0.02),
    },
    # ---------------- TENNIS / RUGBY --------------------------------------
    "tennis_price_only": {
        "sports": ("tennis",),
        "taxonomy": ("Match Winner (Moneyline)",),
        "selection": "any",
        "requires_espn_history": True,
        "blueprint": "Tennis: price ONLY when ESPN history exists for the "
                     "fixture; otherwise grey out (needs data source).",
        "gates": gate(form=0.90, average=0.0, model_floor=0.80,
                      odds_floor=1.20, edge_floor=0.02),
    },
    "rugby_price_only": {
        "sports": ("rugby", "rugby_union", "rugby_league"),
        "taxonomy": ("Match Winner", "Moneyline", "Alternative Handicaps",
                     "Alternative Total Points"),
        "selection": "any",
        "requires_espn_history": True,
        "blueprint": "Rugby normal-margin model (sigma 12) but only priced "
                     "with real fixture history; else grey.",
        "gates": gate(form=0.85, average=0.0, model_floor=0.78,
                      odds_floor=1.18, edge_floor=0.02),
    },
    # ---------------- GREY (never priced) ---------------------------------
    "grey_needs_data": {
        "sports": ("cricket", "golf", "motorsports", "darts", "esports", "mma"),
        "taxonomy": (),
        "selection": "none",
        "grey": True,
        "blueprint": "No reliable rate/history feed wired — shown grey as "
                     "'needs data source', never silently dropped.",
        "gates": gate(form=1.0, average=0.0, model_floor=1.01,
                      odds_floor=99.0, edge_floor=1.0),
    },
}

GREY_SPORTS = MARKET_CLASSES["grey_needs_data"]["sports"]


# ---------------------------------------------------------------------------
# Taxonomy mapping
# ---------------------------------------------------------------------------

def load_taxonomy(path=None):
    """sport -> set(market names) from data/markets_taxonomy.csv. Never raises."""
    out = {}
    try:
        with open(path or TAXONOMY_CSV, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sp = str(row.get("Sport") or "").strip()
                mk = str(row.get("Betting Market") or "").strip()
                if sp and mk:
                    out.setdefault(sp, set()).add(mk)
    except Exception as e:
        log(f"taxonomy load failed: {type(e).__name__}")
    return out


def _sport_tokens(sport):
    s = str(sport or "").lower()
    toks = set([s])
    repl = {"football / soccer": "soccer", "soccer": "soccer",
            "basketball": "basketball", "american football (nfl/cfl)": "nfl",
            "baseball (mlb)": "mlb", "ice hockey (nhl)": "nhl",
            "hockey": "nhl", "tennis": "tennis",
            "rugby league / rugby union": "rugby"}
    if s in repl:
        toks.add(repl[s])
    if "nfl" in s or "cfl" in s:
        toks.add("nfl")
    if "mlb" in s:
        toks.add("mlb")
    if "nhl" in s:
        toks.add("nhl")
    if "hockey" in s:
        toks.add("nhl")
    if "basketball" in s:
        toks.add("basketball")
    if "nba" in s:
        toks.add("basketball")
    if "rugby" in s:
        toks.add("rugby")
    if "soccer" in s or "football" in s and "american" not in s:
        toks.add("soccer")
    return toks


def classes_for_sport(sport):
    """All catalog class keys whose sports intersect `sport`. Never raises."""
    out = []
    try:
        toks = _sport_tokens(sport)
        for key, cls in MARKET_CLASSES.items():
            if any(str(sp).lower() in toks for sp in cls.get("sports", ())):
                out.append(key)
    except Exception:
        pass
    return out


def map_taxonomy_to_classes(tax_map=None):
    """Return {class_key: [(sport_row, market_name), ...]} provenance.

    Every catalog class that finds at least one matching taxonomy row is
    'listed'; unmatched ones still exist but carry no listing.
    """
    out = {}
    try:
        tax_map = tax_map or load_taxonomy()
        for key, cls in MARKET_CLASSES.items():
            hits = []
            want = tuple(m.lower() for m in cls.get("taxonomy", ()))
            for sp, markets in tax_map.items():
                if not (set(_sport_tokens(sp)) &
                        {str(x).lower() for x in cls.get("sports", ())}):
                    continue
                for mk in markets:
                    mkl = mk.lower()
                    if any(w in mkl for w in want):
                        hits.append((sp, mk))
            out[key] = hits
    except Exception as e:
        log(f"taxonomy map failed: {type(e).__name__}")
    return out


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def get_gates(class_key, thresholds=None):
    """Gates for a class with optional trainer overlay applied (clamped)."""
    cls = MARKET_CLASSES.get(class_key)
    base = dict(cls["gates"]) if cls else gate()
    if thresholds:
        base = apply_thresholds(base, thresholds.get(class_key) or {})
    return base


def apply_thresholds(gates, adj):
    """Clamped trainer overlay — the honest self-training contract (item 3):

      realized < predicted - 5%  -> model_floor += 0.03  (hard cap 0.95)
      realized > predicted + 5%  -> edge_floor -= 0.005  (hard floor 0.015)

    Only these two fields move, only within their clamp bands; anything else
    in `adj` is ignored. Never raises.
    """
    out = dict(gates or gate())
    try:
        mf = float(out.get("model_floor", 0.70))
        ef = float(out.get("edge_floor", 0.02))
        dm = _numf(adj.get("model_floor_delta"))
        de = _numf(adj.get("edge_floor_delta"))
        mf = max(0.50, min(0.95, mf + dm))
        ef = max(0.015, min(0.10, ef + de))
        out["model_floor"] = round(mf, 4)
        out["edge_floor"] = round(ef, 4)
    except Exception:
        pass
    return out


def _numf(x, default=0.0):
    try:
        v = float(x)
        return v if v == v else default
    except Exception:
        return default


def load_thresholds(path=None):
    """Read data/thresholds.json written by the self-trainer. Never raises."""
    try:
        with open(path or THRESHOLDS_JSON, encoding="utf-8") as fh:
            js = json.load(fh)
        if isinstance(js, dict):
            return js
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"thresholds load skipped: {type(e).__name__}")
    return {}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def check_gates(class_key, features, thresholds=None):
    """Evaluate one candidate line against its class gates.

    features (all optional numbers):
      form_std   : stddev of the relevant last-5 rate (lower = consistent);
      avg_rate   : combined model rate;
      line       : book line;
      model_prob : ensemble probability for the selection;
      odds       : best decimal price found;
      cushion    : |line - model_projection| for alt classes.
    Returns (ok: bool, reasons: list[str]). Never raises.
    """
    ok, reasons = True, []
    try:
        cls = MARKET_CLASSES.get(class_key)
        if not cls:
            return False, ["unknown-class"]
        if cls.get("grey"):
            return False, ["grey: needs data source"]
        g = get_gates(class_key, thresholds)
        # form: last-5 consistency of the relevant rate
        fs = _numf(features.get("form_std"), 99.0)
        form_limit = (1.0 - float(g["form"])) * _numf(features.get("rate_scale"), 1.0) \
                     + 0.05
        if fs > max(0.15, form_limit):
            ok = False
            reasons.append(f"form: rate std {fs:.3f} > {max(0.15, form_limit):.3f}")
        # average / cushion gate
        if "cushion" in cls:
            cush = _numf(features.get("cushion"), 0.0)
            need = float(cls["cushion"])
            if cush < need:
                ok = False
                reasons.append(f"cushion {cush:.2f} < {need:.2f}")
        else:
            avg = _numf(features.get("avg_rate"), 0.0)
            line = _numf(features.get("line"), 0.0)
            side = str(features.get("side", "over")).lower()
            if side.startswith("o") and avg < line + float(g["average"]):
                ok = False
                reasons.append(f"average {avg:.2f} < line {line:.2f}+{g['average']:.2f}")
            if side.startswith("u") and line - float(g["average"]) > avg:
                ok = False
                reasons.append(f"average {avg:.2f} > line {line:.2f}-{g['average']:.2f}")
        # model floor
        mp = _numf(features.get("model_prob"), 0.0)
        if mp < float(g["model_floor"]):
            ok = False
            reasons.append(f"model {mp:.3f} < floor {g['model_floor']:.3f}")
        # odds floor
        od = _numf(features.get("odds"), 0.0)
        if od < float(g["odds_floor"]):
            ok = False
            reasons.append(f"odds {od:.3f} < floor {g['odds_floor']:.3f}")
        # edge floor
        edge = mp * od - 1.0 if od > 0 else -1.0
        if edge < float(g["edge_floor"]):
            ok = False
            reasons.append(f"edge {edge:.3f} < floor {g['edge_floor']:.3f}")
    except Exception as e:
        return False, [f"gate-error:{type(e).__name__}"]
    return ok, reasons


def price_selection(class_key, params, thresholds=None):
    """Route a class + params through sport_models to (p_eff,p_loss,kind),
    then run the gates. Returns dict row or None. Never raises."""
    try:
        if _sm is None:
            return None
        p = params or {}
        mc = class_key
        if "team_total" in mc:
            trip = _sm.estimate_team_total_triplet(p.get("line", 1.0),
                                                   p.get("lam", 1.0),
                                                   p.get("side", "over"))
            pw, pp, pl = trip
            peff = pw + 0.5 * pp
            kind = "model"
            feats = {"model_prob": peff, "p_push": pp,
                     "breakeven": _sm.breakeven_odds(pw, pp, pl),
                     "avg_rate": p.get("lam", 0.0), "line": p.get("line", 0.0),
                     "side": p.get("side", "over"),
                     "form_std": p.get("form_std", 99.0),
                     "odds": p.get("odds", 0.0)}
        elif "alt_total" in mc or "first_half" in mc or "first5" in mc or \
                "game_total" in mc or mc.endswith("_push") and "total" in mc:
            if p.get("lam_a") is not None:
                peff, pl, kind = _sm.estimate_game_total(
                    p.get("line", 6.5), p.get("lam_a", 3.0),
                    p.get("lam_b", 3.0), p.get("side", "over"))
            else:
                peff, pl, kind = _sm.estimate_alt_total(
                    p.get("line", 220.0), p.get("model_total", 220.0),
                    p.get("sport", "nba"), p.get("side", "over"),
                    p.get("n", 0))
            feats = {"model_prob": peff, "cushion": abs(
                        _numf(p.get("line")) - _numf(p.get("model_total",
                                                            p.get("model_lambda_sum", 0.0)))),
                     "odds": p.get("odds", 0.0),
                     "form_std": p.get("form_std", 99.0),
                     "side": p.get("side", "over")}
            if "cushion" in p:
                feats["cushion"] = p["cushion"]
        elif "spread" in mc or "runline" in mc:
            peff, pl, kind = _sm.estimate_alt_spread(
                p.get("line", 0.0), p.get("model_margin", 0.0),
                p.get("sport", "nba"), p.get("home", True), p.get("n", 0)) \
                if p.get("model_margin") is not None else \
                _sm.estimate_spread(p.get("line", 0.0), p.get("ratings", {}),
                                    p.get("sport", "nba"), p.get("home", True),
                                    p.get("n", 0))
            feats = {"model_prob": peff,
                     "cushion": p.get("cushion", abs(_numf(p.get("line")) -
                                                     _numf(p.get("model_margin", 0.0)))),
                     "odds": p.get("odds", 0.0),
                     "form_std": p.get("form_std", 99.0)}
        else:
            peff, pl, kind = _sm.estimate_moneyline(
                p.get("ratings", {}), p.get("sport", "nba"),
                p.get("home", True), p.get("n", 0))
            feats = {"model_prob": peff, "odds": p.get("odds", 0.0),
                     "form_std": p.get("form_std", 99.0)}
        ok, reasons = check_gates(mc, feats, thresholds)
        return {"class": mc, "p_eff": round(peff, 4), "p_loss": round(pl, 4),
                "ensemble_kind": kind, "features": feats,
                "gates": get_gates(mc, thresholds),
                "pass": ok, "reasons": reasons}
    except Exception as e:
        log(f"price_selection {class_key} failed: {type(e).__name__}")
        return None


def hunt_start_report(thresholds=None):
    """Console snapshot the owner watches: floors per class after trainer
    overlay. Called at hunt start. Never raises."""
    th = thresholds if thresholds is not None else load_thresholds()
    lines = []
    try:
        for key in sorted(MARKET_CLASSES):
            g = get_gates(key, th)
            tag = "GREY" if MARKET_CLASSES[key].get("grey") else \
                  ("PRICE-ONLY" if MARKET_CLASSES[key].get("requires_espn_history")
                   else "LIVE")
            lines.append(f"{key:<28} {tag:<10} model>={g['model_floor']:.3f} "
                         f"odds>={g['odds_floor']:.2f} edge>={g['edge_floor']:.3f}")
        for ln in lines:
            print(f"[SOFTLINE] {ln}", flush=True)
    except Exception as e:
        log(f"hunt_start_report failed: {type(e).__name__}")
    return lines
