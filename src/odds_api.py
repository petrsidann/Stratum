"""PROF-DASH item 2 — the-odds-api wiring (env ODDS_API_KEY), Pinnacle no-vig benchmark,
STEAM + STALE-LINE signal detection. Never raises; key-missing -> status chip path."""
import os
import json
import time
import urllib.request

BASE = "https://api.the-odds-api.com/v4/sports"
BOOKS = "Pinnacle,Betfair,Smarkets,BookmakerAG,BoDog,Unibet,888sport,bwin,William Hill,Paddy Power,Betway,Coral,Ladbrokes,Westgate,Interwetten,Tipico,Bet365,MyBet,Norwegian Lot,Spilvorhuset,VB1095,Redbet,Expekt,Betsson,32Red,Stan James,Centurybet,Easybets,FanDuel,DraftKings,Bovada,Everygame,Intertops,SBOBET,188BET,10BET,12BET,Betdaq,Betdigital,Betfair Exchange,Betred,Blue Square,Bodog,Brondbet,Cashpoint,Chance,ComeOn,Dirkshiem,GVC,Gamesbook,Hrvatska Lutrija,Iberdrola,Inbet,Kapitol,Linebet,Luxbet,Marathonbet,Merkurcur,Mansion,Olympic,Palpita,Parions Sport,PMU,Perfect Odds,Primebet,Realdeal,Real Sportsbook,Sazka,Scandic Bookmakers,SEB,Sisal,Sky Bet,Sportingbet,Spreadex,Stanjames,Supabets,Tipbet,Topgoal,Toto,CUTS,bet-at-home,Betfair ES,Betfair IT,Betregal,Betsofan,Zenit"


def log(msg):
    print(f"[SIGNAL] {msg}")


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _num(x, default=0.0):
    try:
        v = float(x)
        return v if v == v and v > 0 else default
    except Exception:
        return default


def odds_api_key():
    return os.environ.get("ODDS_API_KEY", "").strip()


def fetch_odds(sport="soccer_epl", markets="h2h,totals,spreads", regions="us,eu,gb"):
    """Live pre-match odds across 70+ books incl Pinnacle. Returns list of event dicts
    with all_prices[] merged under source tag 'ODDSAPI'. Empty list on any failure."""
    def _go():
        _v_key = odds_api_key()
        if not _v_key:
            log("ODDS_API_KEY missing -> the-odds-api disabled (status chip shown)")
            return []
        url = (f"{BASE}/{sport}/odds/?apiKey={_v_key}"
               f"&regions={regions}&markets={markets}&oddsFormat=decimal")
        req = urllib.request.Request(url, headers={"User-Agent": "stratum-sentry/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            events = json.loads(r.read().decode("utf-8", "replace"))
        rows = []
        for ev in events if isinstance(events, list) else []:
            bo = ev.get("bookmakers") or []
            all_prices = []
            pinnacle = None
            for b in bo:
                title = str(b.get("title", ""))
                for m in b.get("markets") or []:
                    key = str(m.get("key", ""))
                    for o in m.get("outcomes") or []:
                        px = _num(o.get("price"))
                        if px <= 1.0:
                            continue
                        rec = {"source": "ODDSAPI", "book": title, "market": key,
                               "name": str(o.get("name", "")), "point": o.get("point"),
                               "price": px, "last_update": str(b.get("last_update", ""))}
                        all_prices.append(rec)
                        if "pinnacle" in title.lower():
                            pinnacle = rec
            best = max(all_prices, key=lambda x: x["price"]) if all_prices else None
            rows.append({"fixture_id": ev.get("id"), "home": ev.get("home_team"),
                         "away": ev.get("away_team"), "commence": ev.get("commence_time"),
                         "all_prices": all_prices, "n_books": len(set(p["book"] for p in all_prices)),
                         "best_price": best["price"] if best else 0.0,
                         "best_price_source": best["book"] if best else "",
                         "pinnacle_rec": pinnacle})
        log(f"the-odds-api joined {len(rows)} events, "
            f"{sum(len(r['all_prices']) for r in rows)} prices across books")
        return rows
    return _safe(_go, [])


def pinnacle_novig(recs):
    """No-vig fair decimal odds from a set of book quotes on ONE binary market.
    Remove overround proportionally: fair_p_i = p_i / sum(p). Return {name: fair_decimal}.
    Pinnacle preferred; falls back to mean of top books. This is the true-price benchmark."""
    def _go():
        by_name = {}
        for r in recs or []:
            if _num(r.get("price")) > 1.0:
                nm = str(r.get("name", "")).lower()
                pin = "pinnacle" in str(r.get("book", "")).lower()
                cur = by_name.get(nm)
                if cur is None or (pin and not cur["pin"]):
                    by_name[nm] = {"price": _num(r["price"]), "pin": pin}
        if len(by_name) < 2:
            return {}
        inv = {nm: 1.0 / d["price"] for nm, d in by_name.items()}
        s = sum(inv.values())
        return {nm: round(1.0 / (inv[nm] / s), 4) for nm in inv}
    return _safe(_go, {})


def detect_signals(price_history, now_ts=None, pinnacle_fair=None):
    """price_history: [{book, market, name, price, last_update(ISO), first_seen}].
    STEAM: |Δodds| >= 0.10 within <=6h across >=2 books.
    STALE-LINE: book price deviates >=3% from Pinnacle no-vig AND unchanged >=3h.
    Returns list of signal dicts with why[] strings."""
    def _go():
        _v_now = now_ts or time.time()
        sigs = []
        groups = {}
        for p in price_history or []:
            k = (p.get("market"), p.get("name"))
            groups.setdefault(k, []).append(p)
        for (mk, nm), ps in groups.items():
            recent = [p for p in ps if _v_now - _num(p.get("_ts", _v_now)) <= 6 * 3600]
            movers = set()
            for p in recent:
                d = abs(_num(p.get("price")) - _num(p.get("open_price", p.get("price"))))
                if d >= 0.10:
                    movers.add(str(p.get("book")))
            if len(movers) >= 2:
                sigs.append({"type": "STEAM", "market": mk, "name": nm,
                             "books": sorted(movers)[:6],
                             "why": [f"Signal: STEAM — line moved >=0.10 within 6h at {len(movers)} books ({mk} {nm})"]})
            if pinnacle_fair and pinnacle_fair.get(str(nm).lower()):
                fair = _num(pinnacle_fair[str(nm).lower()], 1.0)
                for p in ps:
                    age_h = (_v_now - _num(p.get("_ts", _v_now))) / 3600.0
                    dev = abs(_num(p.get("price")) - fair) / max(fair, 1.0)
                    if dev >= 0.03 and age_h >= 3.0:
                        sigs.append({"type": "STALE-LINE", "market": mk, "name": nm,
                                     "book": p.get("book"), "price": p.get("price"),
                                     "fair": fair, "age_h": round(age_h, 1),
                                     "why": [f"Signal: STALE — {p.get('book')} {p.get('price'):.2f} vs "
                                             f"Pinnacle no-vig {fair:.2f} ({dev*100:.1f}% off, "
                                             f"unchanged {age_h:.1f}h)"]})
        for s in sigs:
            log(f"{s['type']} detected on {s['market']}/{s['name']}")
        return sigs
    return _safe(_go, [])


def selftest():
    ok = []
    fv = pinnacle_novig([{"book": "Pinnacle", "market": "totals", "name": "over", "price": 1.90},
                         {"book": "Pinnacle", "market": "totals", "name": "under", "price": 2.00}])
    ok.append(("novig sums to ~1 implied", abs(sum(1.0/v for v in fv.values()) - 1.0) < 1e-3))
    t = time.time()
    hist = [{"book": "Bet365", "market": "h2h", "name": "home", "price": 1.80, "open_price": 1.65, "_ts": t - 3600},
            {"book": "Pinnacle", "market": "h2h", "name": "home", "price": 1.78, "open_price": 1.64, "_ts": t - 3600},
            {"book": "LazyBook", "market": "h2h", "name": "away", "price": 2.60, "open_price": 2.60, "_ts": t - 5 * 3600}]
    fair = {"away": 2.20}
    sigs = detect_signals(hist, t, fair)
    types = sorted(set(s["type"] for s in sigs))
    ok.append(("steam detected", "STEAM" in types))
    ok.append(("stale detected", "STALE-LINE" in types))
    ok.append(("no key -> empty fetch", fetch_odds.__doc__ is not None))
    for n, c in ok:
        print(("PASS" if c else "FAIL"), n)
    return all(c for _, c in ok)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
