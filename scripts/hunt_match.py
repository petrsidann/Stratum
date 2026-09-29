#!/usr/bin/env python3
"""Stratum on-demand hunter: scans TODAY, streams progress to an issue comment,
publishes diagrams + data/hunts/latest.json. Always exits 0."""
import json, os, re, sys, time, base64, argparse, datetime as dt
from urllib import request as urlreq, error as urlerr

REPO = os.environ.get("GITHUB_REPOSITORY", "petrsidann/Stratum")
TK = os.environ.get("GITHUB_TOKEN", "")
API = f"https://api.github.com/repos/{REPO}"
CHANNEL_TITLE = "STRATUM_HUNT_CHANNEL"

# QUANT BRAIN: the repo's src/ modules (model_poisson, context_llm) are stdlib-only.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
try:
    import model_poisson          # Dixon-Coles-lite pricer
    import context_llm            # guarded one-call-per-fixture adjuster
    QUANT_OK = True
except Exception as _qe:          # never break the hunt if imports fail
    model_poisson = context_llm = None
    QUANT_OK = False
IS_SOCCER = {"soccer", "football_soccer"}

# QUANT BRAIN: football taxonomy rows (owner CSV) for coverage accounting
TAX_FOOTBALL = set()
if QUANT_OK:
    try:
        _, _tax_rows = model_poisson.load_taxonomy()
        TAX_FOOTBALL = {m for (s, m) in _tax_rows if s.lower().startswith("football")}
    except Exception:
        TAX_FOOTBALL = set()

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
    body = "```json\n" + json.dumps({"hunt_id": hunt_id, "status": status, "stage": stage,
                                     "progress": progress, "lines": LINES[-80:],
                                     "result": result}, indent=1)[:59900] + "\n```"
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


def verify_delivery():
    """GET our own raw latest.json URL and log HTTP status + first 200 chars,
    so every future run proves delivery from the log itself."""
    raw = f"https://raw.githubusercontent.com/{REPO}/main/data/hunts/latest.json"
    try:
        req = urlreq.Request(raw, headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"})
        with urlreq.urlopen(req, timeout=15) as r:
            head = r.read(200).decode("utf-8", "replace")
            log("SYSTEM", f"delivery proof: GET {raw} -> HTTP {r.status}; head={head[:200]!r}")
    except Exception as e:
        log("SYSTEM", f"delivery proof FAILED: GET {raw} -> {e}")


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


def _tax_name(book_market):
    """Map a book market name to its taxonomy row (owner CSV), or None."""
    if not TAX_FOOTBALL:
        return None
    bm = (book_market or "").lower()
    if "1x2" in bm or "winner" in bm or "result" in bm and "half" not in bm \
            and "first" not in bm and "last" not in bm:
        return "Full-Time Result (1X2)"
    if "over/under" in bm or "total" in bm and ("over" in bm or "under" in bm or "/under" in bm):
        return "Over/Under Total Goals (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)"
    if "spread" in bm or "handicap" in bm:
        return "Asian Handicap"
    return None


def merge_model_into_fixture(fx, tax_rows=None):
    """QUANT BRAIN: price the fixture with the Poisson model, apply one guarded
    LLM context pass, then fuse model probs into every book row
    (edge_vs_model = model_prob*book_odds - 1) and emit MODEL-ONLY rows for
    priced markets that have no book line. Never raises."""
    try:
        base_rows, meta = model_poisson.build_fixture_model(
            fx, team_ids=fx.get("team_ids"))
    except Exception as e:
        log("ACTUARY", f"model skipped: {type(e).__name__}: {e}")
        fx["model_status"] = "error"
        return
    ctx = {"adjustments": [], "confidence": 0.0, "provider": "offline"}
    try:
        ctx = context_llm.get_context_adjustments(fx, meta)
        meta = context_llm.apply_adjustments(meta, ctx.get("adjustments"))
        adj_rows, _ = model_poisson.price_all_markets(
            meta["lambda_home"], meta["lambda_away"],
            meta.get("corner_rates"), meta.get("card_rates"),
            league=fx.get("league") or "eng.1",
            sample_games=meta.get("sample_games", 0))
    except Exception as e:
        log("CONTEXT", f"llm skipped ({type(e).__name__}) — math-only mode")
        adj_rows = base_rows
    cov = meta.get("coverage", {})
    fx["model_coverage"] = {"priced": cov.get("priced", 0), "denominator": 200}
    fx["context_provider"] = ctx.get("provider", "offline")
    fx["context_confidence"] = ctx.get("confidence", 0.0)
    fx["context_adjustments"] = ctx.get("adjustments", [])
    hist_src = (meta.get("fit") or {}).get("source") or \
        ("league_prior" if not meta.get("sample_games") else "n/a")
    log("ACTUARY", f"fitted λ home={meta.get('lambda_home')} away={meta.get('lambda_away')} "
                   f"(ρ={meta.get('rho')}, half_scale={meta.get('half_scale')}); "
                   f"sample_games={meta.get('sample_games')} [history: {hist_src}]")
    log("ACTUARY", f"priced {cov.get('priced', 0)}/200 taxonomy markets "
                   f"({len(adj_rows)} selection rows)")
    if fx.get("context_adjustments"):
        parts = ", ".join(f"{a['target']}:{a['delta']:+.2f}" for a in fx["context_adjustments"])
        log("CONTEXT", f"{fx['context_provider']} applied [{parts}] "
                       f"conf={fx['context_confidence']}")
    else:
        log("CONTEXT", "no adjustments accepted — math-only mode "
                       f"(provider={fx['context_provider']})")

    # index model rows by taxonomy market + normalized selection
    idx = {}
    for r in adj_rows:
        tn = r.get("taxonomy_market") or model_poisson._tax_name(
            r["market"], TAX_FOOTBALL)
        if not tn:
            continue
        idx[(tn, str(r["selection"]).lower())] = r

    def match_row(book_market, book_sel):
        tn = _tax_name(book_market)
        if not tn:
            return None
        bs = (book_sel or "").lower()
        cands = [(k, v) for k, v in idx.items() if k[0] == tn]
        best = None
        if "Full-Time Result" in tn:
            key = ("home" if bs.startswith("home") else
                   "away" if bs.startswith("away") else
                   "draw" if bs.startswith("draw") else None)
            for k, v in cands:
                if k[1] == key:
                    best = v
        elif "Over/Under Total" in tn:
            want_over = bs.startswith("over")
            num = None
            mnum = re.search(r"(\d+(?:\.\d+)?)", bs)
            if mnum:
                num = float(mnum.group(1))
            for k, v in cands:
                if (k[1] == "over") != want_over:
                    continue
                mk = re.search(r"(\d+(?:\.\d+)?)", v["market"])
                if num is not None and mk and abs(float(mk.group(1)) - num) < 0.01:
                    return v
                if best is None:
                    best = v
        elif "Handicap" in tn:
            for k, v in cands:
                if k[1].startswith(bs.split()[0][:4]):
                    best = v
                    break
        return best

    used = set()
    for row in fx["top_edges"]:
        mr = match_row(row.get("market", ""), row.get("selection", ""))
        if mr and mr.get("model_prob") is not None and row.get("book_odds"):
            mp_ = float(mr["model_prob"])
            row["model_prob"] = round(mp_, 4)
            row["model_fair_odds"] = mr.get("model_fair_odds")
            row["edge_vs_model"] = round(mp_ * float(row["book_odds"]) - 1.0, 4)
            row["model_confidence"] = mr.get("confidence")
            used.add((mr.get("taxonomy_market"), str(mr["selection"]).lower()))
        else:
            row.setdefault("model_prob", None)
            row.setdefault("edge_vs_model", None)

    extra = []
    for r in adj_rows:
        tn = r.get("taxonomy_market") or model_poisson._tax_name(
            r["market"], TAX_FOOTBALL)
        key = (tn, str(r["selection"]).lower())
        if not tn or key in used:
            continue
        extra.append({"market": r["market"], "selection": r["selection"],
                      "book_odds": None, "model_prob": r["model_prob"],
                      "model_fair_odds": r["model_fair_odds"],
                      "edge_vs_model": None, "ev_percent": None,
                      "fair_odds": r["model_fair_odds"],
                      "confidence_score": int(max(1, min(99, round(
                          (r["model_prob"] or 0) * 100)))),
                      "kelly_stake_pct": 0.0, "model_confidence": r["confidence"],
                      "reasoning_summary": "MODEL-ONLY (no line yet)",
                      "label": "MODEL-ONLY (no line yet)"})
    fx["model_only_count"] = len(extra)
    allrows = fx["top_edges"] + extra
    allrows.sort(key=lambda x: (x.get("edge_vs_model") is None,
                                -(x.get("edge_vs_model") or 0)))
    fx["top_edges"] = allrows[:20]
    fx["markets_scanned"] = len(allrows)


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
            home_id = away_id = None
            for c in (comp.get("competitors") or []):
                if not isinstance(c, dict):
                    continue
                tm = c.get("team") or {}
                nm = tm.get("displayName") or "?"
                if c.get("homeAway") == "home":
                    home = nm
                    home_id = tm.get("id")
                else:
                    away = nm
                    away_id = tm.get("id")
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
                                     "team_ids": {"home": home_id, "away": away_id},
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
        verify_delivery()
        return

    for i, fx in enumerate(fixtures):
        log("ACTUARY", f"{fx['home']} vs {fx['away']}: de-vigged {fx['markets_scanned']} market lines")
        if QUANT_OK and fx.get("sport") in IS_SOCCER:
            try:
                merge_model_into_fixture(fx)
            except Exception as e:
                log("ACTUARY", f"model merge skipped: {type(e).__name__}: {e}")
                fx["model_status"] = "error"
        elif QUANT_OK:
            log("ACTUARY", f"{fx['home']} vs {fx['away']}: Poisson pricer is football-only "
                           f"(sport={fx.get('sport')}) — book consensus rows unchanged")
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
    verify_delivery()


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
            verify_delivery()
        except Exception:
            pass
    sys.exit(0)
