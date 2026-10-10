"""EMERGENCY item 2 — LOTTERY SUM-TOTALS scraper + model (owner's proven edge #1).

Markets: Belgium KENO, Russia Rapido, All or Nothing, Big 8 — "Sum of All Drawn
Numbers Over/Under X". Not on ESPN. Sources: betika.com lottery section and
sportpesa.co.ke lottery via browser-use deep scrape (src/browser_session), with
plain-HTTP fallback; historical draws parsed from the same pages to fit a
Poisson-ish normal model on the SUM of drawn numbers.

Output rows: {lottery_name, draw_time, sum_line, odds, model_prob, edge}.
NEVER raises — every public entry returns [] / status strings on failure so the
aggregator shows a grey chip instead of silence. stdlib math only for the model.
"""
import json
import math
import os
import re
import time

try:
    import browser_session as _bs
except Exception:  # pragma: no cover
    _bs = None

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "lottery_history.json")

SITES = {
    "BETIKA": "https://www.betika.com/en-gb/sport/lottery",
    "SPORTPESA": "https://www.sportpesa.co.ke/sports/lottery",
}

# league defaults: mean/std of the sum of drawn numbers per game type
LOTTERY_DEFAULTS = {
    "keno_belgium": {"mean": 360.0, "sd": 65.0},
    "rapido": {"mean": 36.0, "sd": 9.0},
    "all_or_nothing": {"mean": 225.0, "sd": 40.0},
    "big8": {"mean": 36.0, "sd": 9.0},
}


def log(msg):
    print(f"[SCOUT-LOTTERY] {msg}")


def _safe(fn, fb):
    try:
        return fn()
    except Exception:
        return fb


def norm_cdf(x, mu, sd):
    if sd <= 0:
        return 0.5
    return 0.5 * (1.0 + math.erf((x - mu) / (sd * math.sqrt(2.0))))


def load_history():
    def _go():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    return _safe(_go, {})


def save_history(hist):
    def _go():
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=1)
        os.replace(tmp, DATA_FILE)
        return True
    return _safe(_go, False)


def parse_draws(html, lottery_key):
    """Extract past winning-number sums from scraped page HTML. Looks for draw
    blocks of digits; stores numeric lists under lottery_key."""
    def _go():
        draws = []
        for m in re.finditer(r"(?:draw|result)[^0-9]{0,40}((?:\d{1,3}[\s,;-]+){3,20}\d{1,3})",
                             html or "", re.I):
            nums = [int(x) for x in re.findall(r"\d{1,3}", m.group(1))]
            if 3 <= len(nums) <= 20 and all(n <= 80 for n in nums):
                draws.append(nums)
        return {lottery_key: draws}
    return _safe(_go, {})


def sum_stats(draws):
    """mean/sd of sums from historical draws (>=10 draws required, else None)."""
    def _go():
        sums = [float(sum(d)) for d in draws if d]
        if len(sums) < 10:
            return None
        mu = sum(sums) / len(sums)
        var = sum((s - mu) ** 2 for s in sums) / max(1, len(sums) - 1)
        return {"mean": mu, "sd": math.sqrt(var), "n": len(sums)}
    return _safe(_go, None)


def model_sum_over(line, draws=None, lottery_key="keno_belgium"):
    """P(lottery number-sum > line). Uses scraped history when >=10 draws exist,
    otherwise sport-default prior; shrinks toward default by sqrt(n/(n+20))."""
    def _go():
        key = lottery_key.lower().replace(" ", "_")
        base = LOTTERY_DEFAULTS.get(key) or {"mean": 100.0, "sd": 30.0}
        st = sum_stats(draws or [])
        if st:
            w = st["n"] / (st["n"] + 20.0)
            mu = w * st["mean"] + (1 - w) * base["mean"]
            sd = max(1.0, w * st["sd"] + (1 - w) * base["sd"])
        else:
            mu, sd = base["mean"], base["sd"]
        p_over = 1.0 - norm_cdf(float(line), mu, sd)
        return round(max(0.001, min(0.999, p_over)), 4)
    return _safe(_go, 0.5)


def scrape_live_lines():
    """browser-use deep scrape of betika/sportpesa lottery sections -> rows of
    {lottery_name, draw_time, sum_line, odds, source}. Empty list if unavailable."""
    def _go():
        rows = []
        for tag, url in SITES.items():
            html = None
            if _bs is not None:
                if _bs.browser_available():
                    html = _bs.scrape_page_sync(url, wait_seconds=8)
                if html is None:
                    html = _bs.http_get(url)
            if not html:
                log(f"{tag}: unreachable -> grey chip (needs data source)")
                continue
            for m in re.finditer(
                    r"(keno|rapido|all\s*or\s*nothing|big\s*8)[^0-9<]{0,80}?"
                    r"(over|under)\s*(\d{1,4})[^0-9<]{0,40}?(\d{1,2}\.\d{1,2})",
                    html, re.I):
                name = re.sub(r"\s+", " ", m.group(1)).title()
                rows.append({"lottery_name": name, "draw_time": "",
                             "sum_line": float(m.group(3)),
                             "side": m.group(2).lower(),
                             "odds": float(m.group(4)), "source": tag})
            log(f"{tag}: {len([r for r in rows if r['source']==tag])} sum-total lines parsed")
        return rows
    return _safe(_go, [])


def hunt_lottery(now_ts=None):
    """Full pipeline: scrape live lines + history, model each, emit ranked rows
    {lottery_name, draw_time, sum_line, odds, model_prob, edge}."""
    def _go():
        hist = load_history()
        out = []
        for r in scrape_live_lines():
            key = re.sub(r"\s+", "_", r["lottery_name"].lower())
            draws = hist.get(key) or hist.get("keno_belgium") or []
            p = model_sum_over(r["sum_line"], draws, key)
            if r.get("side") == "under":
                p = 1.0 - p
            fair = 1.0 / max(p, 1e-6)
            edge = r["odds"] / fair - 1.0
            out.append({"lottery_name": r["lottery_name"],
                        "draw_time": r.get("draw_time", ""),
                        "sum_line": r["sum_line"], "odds": r["odds"],
                        "model_prob": p, "edge": round(edge, 4),
                        "source": r["source"], "market": "SUM_TOTAL"})
        out.sort(key=lambda x: -x["edge"])
        log(f"lottery hunt complete: {len(out)} priced sum-total rows")
        return out
    return _safe(_go, [])


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[SCOUT-LOTTERY][selftest] {'PASS' if cond else 'FAIL'} {name}")

    draws = [[sum([i * 7 % 40 + 1 for i in range(10)])] for _ in range(1)]
    real_draws = [[10, 12, 14, 16, 18, 20, 22, 24, 26, 28]] * 12  # sums=190
    st = sum_stats(real_draws)
    chk("sum_stats needs >=10 draws", st is not None and abs(st["mean"] - 190) < 1e-6)
    chk("short history -> None", sum_stats([[1, 2]]) is None)
    # history mean 190 shrinks toward keno default 360 with w=12/32 -> mu~257.5
    p_lo = model_sum_over(150.0, real_draws, "keno_belgium")
    p_hi = model_sum_over(300.0, real_draws, "keno_belgium")
    chk("P(over) monotone decreasing in line", p_lo > 0.5 > p_hi)
    chk("model prob in (0,1)", 0.0 < p_lo < 1.0 and 0.0 < p_hi < 1.0)
    chk("default prior path works", 0.0 < model_sum_over(360, [], "rapido") < 1.0)
    rows = hunt_lottery()
    chk("hunt never raises, returns list offline", isinstance(rows, list))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
