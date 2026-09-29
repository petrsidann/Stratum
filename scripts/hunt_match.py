#!/usr/bin/env python3
"""Stratum on-demand hunter: scans TODAY, streams progress to an issue comment,
publishes diagrams + data/hunts/latest.json. Always exits 0."""
import json, os, re, sys, time, base64, argparse, datetime as dt
from urllib import request as urlreq, error as urlerr

REPO = os.environ.get("GITHUB_REPOSITORY", "petersidann/Stratum")
TK = os.environ.get("GITHUB_TOKEN", "")
API = f"https://api.github.com/repos/{REPO}"
CHANNEL_TITLE = "STRATUM_HUNT_CHANNEL"

LEAGUES = {
    "soccer": ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "usa.1",
               "uefa.champions", "uefa.europa", "uefa.nations", "fifa.world", "fifa.friendly"],
    "basketball": ["nba"], "football": ["nfl"], "baseball": ["mlb"], "hockey": ["nhl"],
}
SPORT_KEY = {"soccer": "soccer", "basketball": "nba", "football": "nfl",
             "baseball": "mlb", "hockey": "nhl"}
HEADER_SETS = [
    {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json"},
]
LINES = []
COMMENT_URL = None


def log(agent, text):
    LINES.append({"agent": agent, "text": text, "ts": int(time.time())})
    print(f"[{agent}] {text}")


def api(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urlreq.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TK}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stratum-hunter"})
    try:
        with urlreq.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except Exception as e:
        print(f"[WARN] api {method} {path}: {e}")
        return None


def post_state(hunt_id, status, stage, progress, result=None):
    global COMMENT_URL
    body = json.dumps({"hunt_id": hunt_id, "status": status, "stage": stage,
                       "progress": progress, "lines": LINES[-80:],
                       "result": result}, indent=1)[:60000]
    if COMMENT_URL:
        api(COMMENT_URL, "PATCH", {"body": body})
        return
    issue = None
    issues = api("/issues?state=open&per_page=100")
    if isinstance(issues, list):
        for it in issues:
            if CHANNEL_TITLE in (it.get("title") or ""):
                issue = it["number"]
                break
    if issue is None:
        created = api("/issues", "POST", {"title": CHANNEL_TITLE,
                                          "body": "Live hunt progress channel. Do not close."})
        issue = (created or {}).get("number")
    if issue is None:
        return
    comments = api(f"/issues/{issue}/comments?per_page=50&sort=created&direction=desc")
    if isinstance(comments, list):
        for c in comments:
            if f'"hunt_id": "{hunt_id}"' in (c.get("body") or ""):
                COMMENT_URL = c.get("url")
                break
    if COMMENT_URL:
        api(COMMENT_URL, "PATCH", {"body": body})
    else:
        created = api(f"/issues/{issue}/comments", "POST", {"body": body})
        COMMENT_URL = (created or {}).get("url")


def publish_latest(hunt_id, status, result):
    payload = {"hunt_id": hunt_id, "status": status, "stage": "DONE", "progress": 1.0,
               "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "fixtures": (result or {}).get("fixtures", []),
               "sources_status": (result or {}).get("sources_status", {}),
               "available_today": (result or {}).get("available_today", []),
               "result": result}
    if (result or {}).get("error_message"):
        payload["error_message"] = result["error_message"]
    b64 = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    existing = api("/contents/data/hunts/latest.json")
    p = {"message": f"hunt {hunt_id}: publish latest.json", "content": b64}
    if isinstance(existing, dict) and existing.get("sha"):
        p["sha"] = existing["sha"]
    api("/contents/data/hunts/latest.json", "PUT", p)


def http_get(url, timeout=15):
    last = None
    for headers in HEADER_SETS:
        try:
            req = urlreq.Request(url, headers=headers)
            with urlreq.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urlerr.HTTPError as e:
            last = f"http_{e.code}"
        except Exception as e:
            last = f"error_{type(e).__name__}"
    return None, last


def am_to_dec(ml):
    try:
        ml = float(ml)
    except Exception:
        return None
    if ml == 0:
        return None
    return round(1 + ml / 100, 3) if ml > 0 else round(1 + 100 / abs(ml), 3)


def best_and_avg(vals):
    vals = [v for v in vals if v]
    if not vals:
        return None, None
    return max(vals), sum(vals) / len(vals)


def add_rows(rows, market_name, sel_odds, prov_count):
    sels = [s for s in sel_odds if sel_odds[s]]
    if len(sels) < 2:
        return
    avgs = {}
    for s in sels:
        b, a = best_and_avg(sel_odds[s])
        if not a:
            return
        avgs[s] = (b, a)
    implied_sum = sum(1 / avgs[s][1] for s in sels)
    if implied_sum <= 0:
        return
    for s in sels:
        b, a = avgs[s]
        p = (1 / a) / implied_sum
        fair = 1 / p
        edge = (b / fair - 1) * 100
        kelly = 0.0
        if edge > 0 and b > 1:
            k = (p * (b - 1) - (1 - p)) / (b - 1)
            kelly = round(max(0.0, k) * 25, 2)
        rows.append({"market": market_name, "selection": s, "book_odds": b,
                     "fair_odds": round(fair, 3), "ev_percent": round(edge, 2),
                     "confidence_score": int(max(1, min(99, round(p * 100)))),
                     "kelly_stake_pct": kelly,
                     "reasoning_summary": (
                         f"{prov_count} provider(s); vig {round((implied_sum - 1) * 100, 1)}% removed; "
                         f"consensus hit prob {round(p * 100, 1)}%; cross-book edge {round(edge, 2)}%.")})


def markets_for_comp(comp):
    rows = []
    provs = []
    for o in (comp.get("odds") or []):
        if not isinstance(o, dict):
            continue
        h = o.get("homeTeamOdds") or {}
        a = o.get("awayTeamOdds") or {}
        d = o.get("drawOdds") or {}
        h = h if isinstance(h, dict) else {}
        a = a if isinstance(a, dict) else {}
        d = d if isinstance(d, dict) else {}
        provs.append({"home_ml": am_to_dec(h.get("moneyLine")),
                      "away_ml": am_to_dec(a.get("moneyLine")),
                      "draw_ml": am_to_dec(d.get("moneyLine")),
                      "spread": o.get("spread"),
                      "home_sp": am_to_dec(h.get("spreadOdds")),
                      "away_sp": am_to_dec(a.get("spreadOdds")),
                      "total": o.get("overUnder"),
                      "over": am_to_dec(o.get("overOdds")),
                      "under": am_to_dec(o.get("underOdds"))})
    if not provs:
        return rows
    ml = {"home": [p["home_ml"] for p in provs], "away": [p["away_ml"] for p in provs]}
    if any(p["draw_ml"] for p in provs):
        ml["draw"] = [p["draw_ml"] for p in provs]
        add_rows(rows, "Match Winner (1X2)", ml, len(provs))
    else:
        add_rows(rows, "Moneyline", ml, len(provs))
    sp = [p for p in provs if p["spread"] is not None and p["home_sp"] and p["away_sp"]]
    if sp:
        line = sp[0]["spread"]
        try:
            line_s = f"{float(line):+.1f}"
        except Exception:
            line_s = str(line)
        add_rows(rows, f"Spread / Handicap ({line_s})",
                 {f"Home {line_s}": [p["home_sp"] for p in sp],
                  f"Away {line_s}": [p["away_sp"] for p in sp]}, len(sp))
    tot = [p for p in provs if p["total"] and p["over"] and p["under"]]
    if tot:
        line = tot[0]["total"]
        add_rows(rows, f"Total Over/Under ({line})",
                 {f"Over {line}": [p["over"] for p in tot],
                  f"Under {line}": [p["under"] for p in tot]}, len(tot))
    return rows


def put_file(path, raw_bytes):
    b64 = base64.b64encode(raw_bytes).decode()
    existing = api(f"/contents/{path}")
    payload = {"message": f"hunt: add {path}", "content": b64}
    if isinstance(existing, dict) and existing.get("sha"):
        payload["sha"] = existing["sha"]
    return api(f"/contents/{path}", "PUT", payload)


def make_chart(fx, hunt_id, idx):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io
        edges = fx["top_edges"][:10]
        if not edges:
            return None
        fig, ax = plt.subplots(figsize=(9, 5), facecolor="#0F111A")
        ax.set_facecolor("#0F111A")
        labels = [f"{e['selection']} · {e['market']}" for e in edges][::-1]
        probs = [e["confidence_score"] for e in edges][::-1]
        colors = ["#2EE6A6" if e["ev_percent"] > 0 else "#38BDF8" for e in edges][::-1]
        ax.barh(labels, probs, color=colors)
        ax.set_xlim(0, 100)
        ax.set_title(f"HIT PROBABILITY — {fx['home']} vs {fx['away']}", color="#00E5FF")
        ax.tick_params(colors="#8B9BB4")
        for s in ax.spines.values():
            s.set_color("#1E2330")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        plt.close(fig)
        path = f"data/hunts/{hunt_id}/prob_chart_{idx}.png"
        put_file(path, buf.getvalue())
        return f"https://raw.githubusercontent.com/{REPO}/main/{path}"
    except Exception as e:
        log("STRATEGIST", f"chart skipped: {e}")
        return None


def tokens_of(q):
    return [t for t in re.split(r"[^a-z0-9]+", q.lower()) if t and t not in ("vs", "v", "the", "and")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--sport", default="auto")
    ap.add_argument("--hunt-id", default="auto")
    args = ap.parse_args()
    hunt_id = args.hunt_id if args.hunt_id and args.hunt_id != "auto" else f"s{int(time.time())}"
    toks = tokens_of(args.query)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y%m%d")
    post_state(hunt_id, "running", "SCOUT", 0.05)
    log("SCOUT", f"hunt {hunt_id} airborne for '{args.query}' — scanning TODAY ({today})")

    sport_paths = list(LEAGUES.keys()) if args.sport == "auto" else \
        [k for k, v in SPORT_KEY.items() if v == args.sport] or list(LEAGUES.keys())
    leagues = [(sp, lg) for sp in sport_paths for lg in LEAGUES.get(sp, [])]
    fixtures, available, sources = [], [], {}
    done = 0
    for sp, lg in leagues:
        done += 1
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sp}/{lg}/scoreboard?dates={today}"
        payload, err = http_get(url)
        if payload is None:
            sources[f"{sp}/{lg}"] = "blocked_by_waf" if err == "http_403" else (err or "no_fixture_in_board")
            log("SCOUT", f"{sp}/{lg}: {sources[f'{sp}/{lg}']}")
            post_state(hunt_id, "running", "SCOUT", 0.05 + 0.55 * done / len(leagues))
            continue
        events = payload.get("events") or []
        n_match = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            comps = ev.get("competitions") or []
            comp = comps[0] if comps else None
            if not isinstance(comp, dict):
                continue
            home = away = "?"
            for c in (comp.get("competitors") or []):
                if not isinstance(c, dict):
                    continue
                nm = ((c.get("team") or {}) or {}).get("displayName") or "?"
                if c.get("homeAway") == "home":
                    home = nm
                else:
                    away = nm
            combined = f"{home} {away}".lower()
            if len(available) < 12:
                available.append(f"{home} vs {away}")
            if toks and all(t in combined for t in toks):
                try:
                    rows = markets_for_comp(comp)
                except Exception as e:
                    log("SCOUT", f"skip corrupt odds: {e}")
                    rows = []
                if rows:
                    rows.sort(key=lambda r: r["confidence_score"], reverse=True)
                    fixtures.append({"fixture_id": str(ev.get("id") or f"{home}-{away}"),
                                     "home": home, "away": away, "sport": SPORT_KEY[sp],
                                     "league": lg, "kickoff_utc": ev.get("date") or "",
                                     "markets_scanned": len(rows),
                                     "top_edges": rows[:12], "diagrams": []})
                    n_match += 1
        sources[f"{sp}/{lg}"] = f"ok({n_match} matched)" if n_match else "no_fixture_in_board"
        log("SCOUT", f"{sp}/{lg}: {sources[f'{sp}/{lg}']}")
        post_state(hunt_id, "running", "SCOUT", 0.05 + 0.55 * done / len(leagues))

    if not fixtures:
        log("STRATEGIST", "no fixture matched today — honest empty result")
        res = {"fixtures": [], "sources_status": sources, "available_today": available,
               "error_message": f"No match today containing '{args.query}'. Teams playing today: see list."}
        post_state(hunt_id, "no_results", "DONE", 1.0, res)
        publish_latest(hunt_id, "no_results", res)
        return

    for i, fx in enumerate(fixtures):
        log("ACTUARY", f"{fx['home']} vs {fx['away']}: de-vigged {fx['markets_scanned']} market lines")
        note = "CONTEXT: offline (math-only confidence)"
        gk = os.environ.get("GROQ_API_KEY")
        if gk:
            try:
                body = json.dumps({"model": "llama-3.1-8b-instant", "max_tokens": 120,
                                   "messages": [{"role": "user",
                                                 "content": f"One-line injury/form note for {fx['home']} vs {fx['away']} today, or NONE."}]}).encode()
                req = urlreq.Request("https://api.groq.com/openai/v1/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {gk}",
                                              "Content-Type": "application/json"})
                with urlreq.urlopen(req, timeout=20) as r:
                    j = json.loads(r.read().decode())
                    txt = j["choices"][0]["message"]["content"].strip().replace("\n", " ")
                    note = f"CONTEXT: {txt[:140]}"
            except Exception:
                note = "CONTEXT: offline (math-only confidence)"
        log("CONTEXT", note.split(": ", 1)[1])
        fx["context_note"] = note
        post_state(hunt_id, "running", "ACTUARY", 0.6 + 0.15 * (i + 1) / len(fixtures))

    for i, fx in enumerate(fixtures[:3]):
        u = make_chart(fx, hunt_id, i)
        if u:
            fx["diagrams"].append(u)
    best = fixtures[0]["top_edges"][0] if fixtures[0]["top_edges"] else None
    if best:
        log("STRATEGIST", f"TOP PICK: {best['selection']} @ {best['book_odds']} "
                          f"(hit prob {best['confidence_score']}%, edge {best['ev_percent']}%)")
    res = {"fixtures": fixtures, "sources_status": sources, "available_today": available}
    post_state(hunt_id, "complete", "DONE", 1.0, res)
    publish_latest(hunt_id, "complete", res)


if __name__ == "__main__":
    hid = "unknown"
    try:
        for a in sys.argv:
            pass
        main()
    except Exception as e:
        log("SYSTEM", f"fatal guard: {type(e).__name__}: {e}")
        try:
            res = {"fixtures": [], "sources_status": {}, "available_today": [], "error_message": str(e)}
            post_state(hid, "error", "FAILED", 1.0, res)
            publish_latest(hid, "error", res)
        except Exception:
            pass
    sys.exit(0)
