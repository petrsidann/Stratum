"""PROF-DASH item 6 — correlated parlay builder on the exact Poisson score matrix.

joint p for same-match leg combos computed from P(score matrix region), never independence.
Fair parlay price = 1/p_joint vs book parlay odds. Self-test: joint p <= min(marginals).
Never raises."""
import math
from sport_models import poisson_pmf


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def score_matrix(lam_h, lam_a, max_goals=10):
    def _go():
        lh = max(0.05, float(lam_h)); la = max(0.05, float(lam_a))
        ph = [poisson_pmf(i, lh) for i in range(max_goals + 1)]
        pa = [poisson_pmf(j, la) for j in range(max_goals + 1)]
        return [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    return _safe(_go, [])


def leg_predicate(leg):
    """leg: {type, ...} -> function(h,a)->bool. Types: home_ml, away_ml, draw, over, under, btts_yes, btts_no."""
    t = str(leg.get("type", "")).lower()
    if t == "home_ml":
        return lambda h, a: h > a
    if t == "away_ml":
        return lambda h, a: a > h
    if t == "draw":
        return lambda h, a: h == a
    if t == "over":
        ln = float(leg.get("line", 2.5))
        return lambda h, a: (h + a) > ln
    if t == "under":
        ln = float(leg.get("line", 2.5))
        return lambda h, a: (h + a) < ln
    if t == "btts_yes":
        return lambda h, a: h >= 1 and a >= 1
    if t == "btts_no":
        return lambda h, a: h == 0 or a == 0
    return None


def marginal_and_joint(lam_h, lam_a, legs):
    """Returns [{leg, p_marginal}], p_joint. Independence would give product of marginals;
    we report TRUE joint from the matrix plus the correlation label."""
    def _go():
        m = score_matrix(lam_h, lam_a)
        if not m or len(legs) < 2:
            return {"marginals": [], "p_joint": 0.0, "p_indep": 0.0, "label": "insufficient"}
        preds = [leg_predicate(l) for l in legs]
        marg = []
        for leg, pr in zip(legs, preds):
            if pr is None:
                return {"marginals": [], "p_joint": 0.0, "p_indep": 0.0, "label": "unknown-leg"}
            p = sum(m[h][a] for h in range(len(m)) for a in range(len(m[0])) if pr(h, a))
            marg.append({"leg": leg, "p_marginal": round(p, 6)})
        p_joint = sum(m[h][a] for h in range(len(m)) for a in range(len(m[0]))
                      if all(pr(h, a) for pr in preds))
        p_indep = 1.0
        for d in marg:
            p_indep *= d["p_marginal"]
        corr = "POSITIVE" if p_joint > p_indep + 1e-4 else ("NEGATIVE" if p_joint < p_indep - 1e-4 else "NEUTRAL")
        return {"marginals": marg, "p_joint": round(p_joint, 6),
                "p_indep": round(p_indep, 6), "label": f"CORRELATED-{corr}",
                "fair_price": round(1.0 / p_joint, 4) if p_joint > 0 else 0.0}
    return _safe(_go, {"marginals": [], "p_joint": 0.0, "p_indep": 0.0, "label": "error", "fair_price": 0.0})


def parlay_rows(lam_h, lam_a, combos, book_parlays=None):
    """combos: list of leg-lists. book_parlays: {combo_index: book_decimal}. Emits parlay rows
    with correlation label + joint p + fair-vs-book edge + why[] strings."""
    def _go():
        rows = []
        _v_bp = book_parlays or {}
        for i, legs in enumerate(combos or []):
            r = marginal_and_joint(lam_h, lam_a, legs)
            if r["p_joint"] <= 0:
                continue
            bp = float(_v_bp.get(i, 0) or 0)
            edge = (r["p_joint"] - 1.0 / bp) if bp > 1.0 else None
            names = "+".join(str(l.get("type")) + (f"({l['line']})" if "line" in l else "") for l in legs)
            print(f"[ACTUARY] parlay {names}: joint={r['p_joint']:.3f} indep={r['p_indep']:.3f} ({r['label']})")
            row = {"combo": names, "legs": legs, "p_joint": r["p_joint"],
                   "p_indep": r["p_indep"], "correlation": r["label"],
                   "fair_price": r["fair_price"], "book_price": bp or None,
                   "edge": round(edge, 4) if edge is not None else None,
                   "why": [f"Joint p={r['p_joint']*100:.1f}% from exact score matrix vs independence-assumed "
                           f"{r['p_indep']*100:.1f}% ({r['label']})",
                           f"Fair parlay price {r['fair_price']:.2f}" + (f" vs book {bp:.2f} -> edge {edge*100:+.1f}%" if edge is not None else " (no book quote)")]}
            rows.append(row)
        return sorted(rows, key=lambda x: -(x["edge"] if x["edge"] is not None else -9))
    return _safe(_go, [])


def selftest():
    ok = []
    r = marginal_and_joint(1.6, 1.1, [{"type": "home_ml"}, {"type": "over", "line": 2.5}])
    marg = [d["p_marginal"] for d in r["marginals"]]
    ok.append(("joint<=min(marginals)", r["p_joint"] <= min(marg) + 1e-12))
    ok.append(("positive correlation detected", r["label"] == "CORRELATED-POSITIVE"))
    r2 = marginal_and_joint(1.2, 1.2, [{"type": "btts_yes"}, {"type": "under", "line": 1.5}])
    ok.append(("negative pair joint small", r2["p_joint"] < 0.05))
    rows = parlay_rows(1.6, 1.1, [[{"type": "home_ml"}, {"type": "over", "line": 2.5}]], {0: 3.7})
    ok.append(("parlay row emitted w/ positive edge", len(rows) == 1 and (rows[0]["edge"] or 0) > 0))
    for n, c in ok:
        print(("PASS" if c else "FAIL"), n)
    return all(c for _, c in ok)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
