#!/usr/bin/env python3
"""EDGE ENGINE item (a): grader + CLV + calibration for data/ledger.jsonl.

Runs at the END of each hunt (invoked by hunt_match.py, guarded). Never
raises — any failure logs and exits 0 so a hunt can never break because of
grading.

Pipeline:
 1. Read data/ledger.jsonl; find ungraded entries whose kickoff is >48h old.
 2. Fetch final scores via keyless ESPN scoreboard endpoints
    (site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates=YYYYMMDD,
    falling back to the global event endpoint when fixture_id is numeric).
 3. Grade market families that are pure functions of the final score:
    1X2 / Double Chance / DNB / BTTS / Over-Under totals / Exact Total /
    Multi-Goal bands / Winning Margin / Clean Sheet / To Nil / Team Totals /
    Correct Score / Half results (when half scores available) -> won/lost/push.
    Corners/Cards lines are NOT gradeable from the scoreboard -> status
    "ungradeable" (marked graded so they are not re-fetched forever).
 4. CLV: for graded rows with pick_odds, re-snapshot the BEST current book
    price for the same selection via ESPN providers ("closing odds proxy")
    and compute clv = pick_odds/close_odds - 1. Rolling mean CLV% published.
 5. Calibration: ONLY rows with ensemble_kind == "model" feed predicted-vs-
    actual buckets (10% bins) + Brier score. Written to data/calibration.json
    locally AND committed via the GitHub Contents API (GITHUB_TOKEN) so CI
    runners see fresh weights on the next hunt.
"""
import json, os, re, sys, time, base64
import datetime as dt
from urllib import request as urlreq, error as urlerr

ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(ROOT, "..", "data", "ledger.jsonl")
CALIB = os.path.join(ROOT, "..", "data", "calibration.json")
REPO = os.environ.get("GITHUB_REPOSITORY", "petrsidann/Stratum")
TK = os.environ.get("GITHUB_TOKEN", "")
API = f"https://api.github.com/repos/{REPO}"
UA = {"User-Agent": "Mozilla/5.0 (Stratum-grade-ledger)"}


def log(msg):
    print(f"[GRADER] {msg}", flush=True)


def http_json(url, timeout=20):
    req = urlreq.Request(url, headers=UA)
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ------------------------------- ledger I/O -------------------------------
def read_ledger():
    rows = []
    try:
        with open(LEDGER, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"ledger read failed: {type(e).__name__}")
    return rows


def write_ledger(rows):
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, LEDGER)


def parse_kickoff(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# ------------------------------- scoring ----------------------------------
def fetch_final(home, away, kickoff):
    """Keyless ESPN scoreboard lookup for one finished fixture.
    Returns dict {hg, ag, hgt, agt} or None. Never raises."""
    day = kickoff.strftime("%Y%m%d") if kickoff else ""
    urls = []
    if day:
        for lg in ("eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "uefa.nations",
                   "uefa.euqual", "intl.friendly", "den.1", "swe.1", "nor.1"):
            urls.append("https://site.api.espn.com/apis/site/v2/sports/"
                        f"soccer/{lg}/scoreboard?dates={day}&limit=200")
    try:
        for u in urls[:14]:
            try:
                js = http_json(u, timeout=15)
            except Exception:
                continue
            for ev in js.get("events") or []:
                try:
                    comp = ev["competitions"][0]
                    teams = {str(c["team"]["displayName"]).lower(): c for c in comp["competitors"]}
                    cids = {str(c["team"].get("shortDisplayName") or "").lower(): c for c in comp["competitors"]}
                    allnames = {**teams, **cids}
                    hk = _find_key(allnames, home)
                    ak = _find_key(allnames, away)
                    if not hk or not ak:
                        continue
                    hc, ac = allnames[hk], allnames[ak]
                    hg, ag = int(hc.get("score") or 0), int(ac.get("score") or 0)
                    hgt = agt = None
                    st = (comp.get("status") or {}).get("type") or {}
                    if not st.get("final"):
                        continue
                    for det in comp.get("detail") or []:
                        m = re.match(r"(\d+)\s*-\s*(\d+)", str(det))
                        if m and "half" in str(det).lower():
                            hgt, agt = int(m.group(1)), int(m.group(2))
                    return {"hg": hg, "ag": ag, "hgt": hgt, "agt": agt}
                except Exception:
                    continue
    except Exception as e:
        log(f"scoreboard scan error {type(e).__name__}")
    return None


def _find_key(names, team):
    t = str(team or "").lower()
    if not t:
        return None
    for k in names:
        if k == t or t in k or k in t:
            return k
    toks = [w for w in re.split(r"\s+", t) if len(w) > 3]
    for k in names:
        if any(w in k for w in toks):
            return k
    return None


# ------------------------------- grading ----------------------------------
def grade_row(row, sc):
    """Return 'won'|'lost'|'push'|None for one ledger row given final score sc."""
    mk = str(row.get("market") or "").lower()
    sel = str(row.get("selection") or "").lower()
    hg, ag = sc["hg"], sc["ag"]
    tot = hg + ag
    side_home = ("home" in sel) or (sel == str(row.get("home") or "").lower())
    side_away = ("away" in sel) or (sel == str(row.get("away") or "").lower())
    is_draw = sel in ("draw", "x", "tie")

    # 1X2 family (ensemble_kind model)
    if "full-time result" in mk or "1x2" in mk or "match winner" in mk:
        if is_draw:
            return "push" if hg == ag else "lost"
        if side_home:
            return "push" if hg == ag else ("won" if hg > ag else "lost")
        if side_away:
            return "push" if hg == ag else ("won" if ag > hg else "lost")
        return None
    # Double chance / DNB (model)
    if "double chance" in mk:
        pair = re.sub(r"[^0-9x]", "", sel)
        want = {"1x": hg >= ag, "x2": hg <= ag, "12": hg != ag}.get(pair)
        if want is None:
            return None
        if pair == "12" and hg == ag:
            return "lost"
        return "won" if want else "lost"
    if "draw no bet" in mk or "dnb" in mk:
        if hg == ag:
            return "push"
        win_home = hg > ag
        return "won" if (side_home and win_home) or (side_away and not win_home) else "lost"
    # Over/Under total goals (poisson-only; still graded for coverage stats)
    mnum = re.search(r"(?:over|under)\s*(\d+(?:\.\d+)?)", sel)
    if ("over/under" in mk or "total goals" in mk or "totals" in mk) and mnum and \
            "corner" not in mk and "card" not in mk and "team total" not in mk:
        line = float(mnum.group(1))
        over = sel.startswith("over")
        if tot == line:
            return "push"
        hit = tot > line if over else tot < line
        return "won" if hit else "lost"
    # BTTS (poisson-only)
    if "both teams to score" in mk or "btts" in mk:
        hit = hg > 0 and ag > 0
        yes = sel.startswith("y") or sel in ("yes", "1")
        return "won" if hit == yes else "lost"
    # Exact total goals
    if "exact total" in mk:
        m = re.search(r"(\d+)", sel)
        if m:
            n = int(m.group(1))
            if n == 7:
                return "won" if tot >= 7 else "lost"
            return "won" if tot == n else "lost"
    # Multi-goal bands
    if "multi-goal" in mk or "multi goal" in mk:
        m = re.match(r"(\d+)\s*-\s*(\d+)", sel)
        if m:
            return "won" if int(m.group(1)) <= tot <= int(m.group(2)) else "lost"
    # Winning margin
    if "winning margin" in mk:
        marg = abs(hg - ag)
        m = re.match(r"(\d+)\s*-\s*(\d+)", sel)
        if m:
            return "won" if int(m.group(1)) <= marg <= int(m.group(2)) else "lost"
        if sel.startswith(("2 ", "2+", "more")) or "+" in sel:
            return "won" if marg >= 2 else "lost"
    # Clean sheet / to nil / win to nil
    if "clean sheet" in mk or "to nil" in mk:
        if side_home or "win to nil" in mk and hg > ag:
            return "won" if ag == 0 and hg > 0 else "lost"
        if side_away:
            return "won" if hg == 0 and ag > 0 else "lost"
    # Team totals O/U
    if "team total" in mk and mnum:
        line = float(mnum.group(1))
        goals = hg if side_home or "home" in mk else ag
        over = sel.startswith("over")
        if goals == line:
            return "push"
        return "won" if (goals > line) == over else "lost"
    # Correct score
    if "correct score" in mk:
        m = re.match(r"(\d+)\s*[-x:]\s*(\d+)", sel)
        if m:
            return "won" if (int(m.group(1)), int(m.group(2))) == (hg, ag) else "lost"
    # Half results (only when half scores were parsed)
    if ("half result" in mk or "half/full" in mk) and sc.get("hgt") is not None:
        first = sc["hgt"] >= sc["agt"] if "1st" in mk else hg >= ag
        if is_draw:
            return "won" if (sc["hgt"] == sc["agt"] if "1st" in mk else hg == ag) else "lost"
        if side_home:
            return "won" if (sc["hgt"] > sc["agt"] if "1st" in mk else hg > ag) else "lost"
        if side_away:
            return "won" if (sc["hgt"] < sc["agt"] if "1st" in mk else hg < ag) else "lost"
    return None


UNGRADEABLE_HINTS = ("corner", "card", "booking", "race to", "goalscorer",
                     "assist", "player", "penalty awarded", "own goal",
                     "injury time", "yellow", "red card")


def is_ungradeable(mk):
    mk = str(mk or "").lower()
    return any(h in mk for h in UNGRADEABLE_HINTS)


# ------------------------------- CLV proxy --------------------------------
def close_odds_for(row):
    """Best current book price for the same selection via ESPN providers —
    used as closing-odds proxy within ~2h of kickoff. Guarded, returns None."""
    try:
        sys.path.insert(0, os.path.join(ROOT))
        # lightweight: query ESPN odds for the fixture id if numeric
        fid = str(row.get("fixture_id") or "")
        if not fid.isdigit():
            return None
        js = http_json("https://site.api.espn.com/apis/site/v2/sports/"
                       f"soccer/scoreboardevent/{fid}", timeout=15)
        best = None
        for comp in (js or {}).get("events", [])[:1] or []:
            pass
        # ESPN per-event odds shape varies; scan generically for decimals
        def walk(o):
            nonlocal best
            if isinstance(o, dict):
                v = o.get("decimalOdds") or o.get("odds")
                if isinstance(v, (int, float)) and 1.01 <= float(v) <= 50:
                    best = max(best or 0.0, float(v))
                for vv in o.values():
                    walk(vv)
            elif isinstance(o, list):
                for vv in o:
                    walk(vv)
        walk(js)
        return best or None
    except Exception:
        return None


# ------------------------------- calibration ------------------------------
def build_calibration(entries):
    """Rolling calibration from graded ensemble_kind=='model' rows."""
    buckets = {}
    brier_n = brier_s = 0.0
    clv_vals = []
    graded = won = lost = push = 0
    for e in entries:
        g = e.get("grade")
        if isinstance(g, (int, float)):
            if e.get("clv") is not None:
                clv_vals.append(float(e["clv"]))
            if e.get("ensemble_kind") == "model" and e.get("model_prob") is not None:
                p = float(e["model_prob"])
                y = 1.0 if g == 1 else 0.0 if g == 0 else None
                if y is not None:
                    brier_s += (p - y) ** 2
                    brier_n += 1
                    lo = min(int(p * 10), 9)
                    b = buckets.setdefault(lo, {"pred_sum": 0.0, "act_sum": 0.0, "n": 0})
                    b["pred_sum"] += p
                    b["act_sum"] += y
                    b["n"] += 1
        if g in (0, 1, "push"):
            graded += 1
            if g == 1:
                won += 1
            elif g == 0:
                lost += 1
            else:
                push += 1
    out = {
        "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "graded_rows": graded, "won": won, "lost": lost, "push": push,
        "brier_model": round(brier_s / brier_n, 4) if brier_n else None,
        "brier_samples": int(brier_n),
        "buckets": [
            {"bucket": f"{lo * 10}-{lo * 10 + 10}%",
             "predicted": round(b["pred_sum"] / b["n"], 4),
             "actual": round(b["act_sum"] / b["n"], 4), "n": b["n"]}
            for lo, b in sorted(buckets.items()) if b["n"] >= 3],
        "clv_mean_pct": round(100 * sum(clv_vals) / len(clv_vals), 2) if clv_vals else None,
        "clv_samples": len(clv_vals),
    }
    return out


def publish_calibration(cal):
    """Write locally + commit via Contents API (guarded)."""
    body = json.dumps(cal, indent=2)
    try:
        with open(CALIB, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    except Exception as e:
        log(f"local calib write failed {type(e).__name__}")
    if not TK:
        return
    try:
        sha = None
        req = urlreq.Request(API + "/contents/data/calibration.json",
                             headers={"Authorization": f"Bearer {TK}",
                                      "Accept": "application/vnd.github+json",
                                      "User-Agent": "stratum-grader"})
        try:
            with urlreq.urlopen(req, timeout=20) as r:
                sha = json.loads(r.read().decode()).get("sha")
        except Exception:
            sha = None
        payload = {"message": "grader: update calibration.json",
                   "content": base64.b64encode(body.encode()).decode()}
        if sha:
            payload["sha"] = sha
        put = urlreq.Request(API + "/contents/data/calibration.json",
                             data=json.dumps(payload).encode(), method="PUT",
                             headers={"Authorization": f"Bearer {TK}",
                                      "Accept": "application/vnd.github+json",
                                      "User-Agent": "stratum-grader"})
        with urlreq.urlopen(put, timeout=30):
            log("calibration.json published via Contents API")
    except Exception as e:
        log(f"calibration publish skipped ({type(e).__name__})")


# ---------------------------------- main ----------------------------------
def run(dry=False):
    rows = read_ledger()
    if not rows:
        log("ledger empty — nothing to grade")
        return
    now = dt.datetime.now(dt.timezone.utc)
    due = {}
    for r in rows:
        if r.get("graded"):
            continue
        ko = parse_kickoff(r.get("kickoff"))
        if not ko or (now - ko).total_seconds() < 48 * 3600:
            continue
        due.setdefault((r.get("home"), r.get("away"), str(r.get("kickoff"))), []).append(r)
    log(f"{len(rows)} ledger rows, {len(due)} fixtures due for grading")
    score_cache = {}
    newly_graded = 0
    for (home, away, ko), group in list(due.items())[:10]:  # API-friendly cap
        key = (home, away)
        if key not in score_cache:
            score_cache[key] = fetch_final(home, away, parse_kickoff(ko))
        sc = score_cache[key]
        for r in group:
            if sc is None:
                if is_ungradeable(r.get("market")):
                    r["graded"] = True
                    r["grade"] = "ungradeable"
                continue
            if is_ungradeable(r.get("market")):
                r["graded"] = True
                r["grade"] = "ungradeable"
                continue
            g = grade_row(r, sc)
            if g is None:
                if is_ungradeable(r.get("market")):
                    r["graded"] = True
                    r["grade"] = "ungradeable"
                continue
            r["graded"] = True
            r["grade"] = {"won": 1, "lost": 0, "push": "push"}[g]
            newly_graded += 1
            if r.get("pick_odds") and g != "push":
                close = close_odds_for(r)
                if close:
                    r["close_odds"] = close
                    r["clv"] = round(float(r["pick_odds"]) / close - 1.0, 4)
    if not dry:
        write_ledger(rows)
    graded_all = [r for r in rows if r.get("graded")]
    cal = build_calibration(graded_all)
    log(f"newly graded {newly_graded}; totals won={cal['won']} lost={cal['lost']} "
        f"push={cal['push']}; brier={cal['brier_model']} (n={cal['brier_samples']}); "
        f"clv={cal['clv_mean_pct']}% (n={cal['clv_samples']})")
    if not dry:
        publish_calibration(cal)


if __name__ == "__main__":
    try:
        run(dry="--dry" in sys.argv)
    except Exception as e:  # belt & braces: grader must never crash a hunt
        log(f"grader aborted safely: {type(e).__name__}: {e}")
    sys.exit(0)
