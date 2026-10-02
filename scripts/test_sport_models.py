"""REAL self-tests for src/sport_models.py (MULTISPORT item 1 closure fix).

Guards against the regression where nested _go() closures rebound their
enclosing function's parameters (UnboundLocalError silently swallowed by
_safe -> every estimator degraded to the 0.5/0.5 fallback). The suite
instruments _safe and FAILS if ANY exception is swallowed on valid input.

Run: python3 scripts/test_sport_models.py   (exit 0 = pass)
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import sport_models as m  # noqa: E402

FAILS = []


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


swallowed = []
_orig_safe = m._safe


def _traced(fn, *a, **kw):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - test instrumentation
        swallowed.append((getattr(fn, "__qualname__", str(fn)),
                          type(e).__name__, str(e)))
        return kw.get("fallback")


m._safe = _traced

# --- true math, not fallbacks ---------------------------------------------
chk("poisson_pmf(1,1.4)>0", m.poisson_pmf(1, 1.4) > 0.0)
chk("poisson_pmf exact vs math",
    abs(m.poisson_pmf(1, 1.4) - 1.4 * math.exp(-1.4)) < 1e-12)
chk("norm_cdf(0)==0.5", abs(m.norm_cdf(0.0) - 0.5) < 1e-12)
chk("norm_cdf lower tail real", m.norm_cdf(-5, 0.5, 2) < 0.01)
chk("norm_cdf upper tail exact vs erf",
    abs(m.norm_cdf(5, 0.5, 2)
        - 0.5 * (1 + math.erf((5 - 0.5) / (2 * math.sqrt(2))))) < 1e-12)

vals = [m.norm_cdf(x, 0.5, 2.0)
        for x in [-8, -4, -2, -1, 0, 0.5, 1, 2, 4, 8]]
chk("norm_cdf monotone", all(vals[i] <= vals[i + 1]
                             for i in range(len(vals) - 1))
    and vals[0] < vals[-1])

# --- push-aware triplets ----------------------------------------------------
t = m.push_triplet_poisson_team(1, 1.3)
chk("team triplet sums 1 (1e-9)", abs(sum(t) - 1) < 1e-9)
chk("team triplet non-fallback push mass", t != (0.5, 0.0, 0.5) and t[1] > 0.3)
tt = m.push_triplet_poisson_total(6, 1.6, 1.4)
chk("game triplet sums 1", abs(sum(tt) - 1) < 1e-9)
tn = m.push_triplet_normal(6, 3.0, 13.5)
chk("normal triplet sums 1", abs(sum(tn) - 1) < 1e-9)
chk("normal triplet non-fallback", tn != (0.5, 0.0, 0.5))
th = m.push_triplet_poisson_total(6.5, 1.6, 1.4)
chk("half-line push==0 & sums1", th[1] == 0.0 and abs(sum(th) - 1) < 1e-9)

# --- breakeven ---------------------------------------------------------------
be = m.breakeven_odds(*t)
chk("breakeven true-math value",
    be < 1000.0 and abs(be - (1 + t[2] / t[0])) < 1e-9)
chk("breakeven sentinel p_win=0", m.breakeven_odds(0, 0, 1) == 1000.0)

# --- estimators return real numbers ------------------------------------------
r = {"off_home": 116, "def_away": 112, "off_away": 110, "def_home": 113,
     "home_adj": 2.5}
pe, pl, k = m.estimate_spread(-4.5, r, "nba", True, 20)
chk("estimate_spread real", abs(pe - 0.5) > 1e-6 and k == "model")
pe2, _, _ = m.estimate_moneyline(r, "nba", True, 20)
chk("estimate_moneyline real", abs(pe2 - 0.5) > 1e-6)
pe3, _, _ = m.estimate_total(228.5, {"total_mu": 231.0}, "nba", "over", 20)
chk("estimate_total directional", pe3 > 0.5)
pe4, _, _ = m.estimate_alt_total(240.0, 231.0, "nba", "under", 20)
chk("alt_total under high line", pe4 > 0.5)
pe5, _, _ = m.estimate_alt_spread(-12.5, 4.0, "nba", True, 20)
chk("alt_spread real", abs(pe5 - 0.5) > 1e-6)
pe6, _, _ = m.estimate_game_total(6.5, 1.6, 1.4, "over")
chk("game_total pois real", abs(pe6 - 0.5) > 1e-6)
d = m.estimate("team_total", {"line": 2.5, "lam": 1.9, "side": "over"})
chk("dispatch real", abs(d[0] - 0.5) > 1e-6)

sig = [m.shrink_sigma("nba", n) for n in [0, 5, 10, 20, 40, 80]]
chk("shrink_sigma monotone decreasing toward sport sigma",
    all(sig[i] >= sig[i + 1] for i in range(len(sig) - 1))
    and sig[0] == 21.0 and 13.5 < sig[-1] < 15.0)
lh, lw = m.team_lambdas(170, 160, 150, 165, 82, 82, "nhl")
chk("team_lambdas plausible", 1.0 < lh < 4.0 and 1.0 < lw < 4.0)
chk("margin_params real",
    m.margin_params(116, 112, 110, 113, 2.5, "nba", 20)[0] != 0.0)

# --- never raises contract ----------------------------------------------------
m._safe = _orig_safe
bad = [None, float("nan"), float("inf"), -float("inf"), "abc", {}, [], 1e30]
ok = True
for b in bad:
    try:
        m.norm_cdf(b)
        m.poisson_pmf(b, b)
        m.push_triplet_poisson_team(b, b)
        m.estimate("team_total", {"line": b, "lam": b})
        m.breakeven_odds(b, b, b)
        m.estimate_spread(b, b, b, b, b)
        m.estimate_moneyline(b, b, b, b)
        m.estimate_total(b, b, b, b, b)
        m.team_lambdas(b, b, b, b, b, b, b)
    except Exception as e:  # noqa: BLE001
        ok = False
        print("RAISED:", b, e)
chk("never raises on garbage", ok)

chk("ZERO swallowed exceptions across valid-input suite", len(swallowed) == 0)
if swallowed:
    seen = set()
    for s in swallowed:
        key = s[0] + s[1]
        if key not in seen:
            print("SWALLOWED:", s)
            seen.add(key)

print("\nRESULT:", "ALL PASS" if not FAILS else f"FAILURES({len(FAILS)}): {FAILS}")
sys.exit(0 if not FAILS else 1)
