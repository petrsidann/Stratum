"""KENYAN WELL — headless-browser scout for Kenyan bookmakers (Betika, Odibets).

Kenyan books price many international fixtures that ESPN's keyless scoreboard
leaves without odds lines. They are JS SPAs, so plain HTTP sees nothing — we
drive a headless Chromium via Playwright.

Contract:
  scout_fixture(home, away, timeout_s=60) -> (rows, status)
    rows   : list of {market, selection, decimal_odds, source}
    status : "ok(N)" | "blocked_by_waf" | "timeout" | "not_found" | "crash(msg)"

NEVER raises. Any failure returns ([], status). The playwright import is
guarded so a missing browser/library can never crash a hunt — in that case
every call returns ([], "crash(playwright not installed)") instantly.
"""

import re
import time

# --- guarded import: missing playwright must never crash a hunt -------------
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    _PLAYWRIGHT_OK = True
except Exception:  # ImportError or anything weird at load time
    sync_playwright = None
    _PLAYWRIGHT_OK = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# SPEAK-UP: mobile UA + iPhone viewport — Kenyan WAFs treat desktop headless
# chromium on GitHub runner IPs as bots and drop the connection outright
# (observed crash(nav): net::ERR_EMPTY_RESPONSE / ERR_CONNECTION_RESET).
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")

BETIKA_URLS = ["https://www.betika.com/en-ke/", "https://www.betika.co.ke/"]
ODIBETS_URLS = ["https://odibets.com/league/Soccer", "https://odibets.com/"]

# SPEAK-UP: JSON XHR fallback endpoints (best-effort — probe statuses logged).
# These are tried when HTML navigation is WAF-blocked or crashes.
_JSON_FALLBACKS = {
    "betika": ["https://www.betika.com/api/v1/sports-menus"],
    "odibets": ["https://odibets.com/api/list",
                "https://api.odibets.com/v1/matches/upcoming"],
}

_SOURCES = {
    "betika": {"urls": BETIKA_URLS, "tag": "BETIKA"},
    "odibets": {"urls": ODIBETS_URLS, "tag": "ODIBETS"},
}

# market tabs worth expanding inside the match page (best-effort clicks)
_MARKET_TABS = ["Main", "Goals", "Corners", "Cards", "Players", "Halves"]

_ODD_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")


def _valid_odd(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if 1.01 <= v <= 50.0 else None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _dismiss_cookie_banner(page):
    """Best-effort cookie-banner dismissal; never raises."""
    texts = ["accept all", "accept", "i agree", "agree", "allow all",
             "got it", "okay", "close"]
    for t in texts:
        try:
            el = page.query_selector(f"text={t}")
            if el and el.is_visible():
                el.click(timeout=1500)
                page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


def _find_fixture_element(page, home, away):
    """Element whose text contains both team names (case-insensitive)."""
    h, a = _norm(home), _norm(away)
    # 1) search box if the site offers one
    try:
        sel = page.query_selector("input[type=search], input[placeholder*='earch' i]")
        if sel and sel.is_visible():
            sel.fill(f"{home} {away}")
            page.wait_for_timeout(2500)
    except Exception:
        pass
    # 2) scan visible elements for both tokens
    try:
        handles = page.query_selector_all(
            "div, section, li, article, a, tr")
    except Exception:
        return None
    best = None
    for el in handles:
        try:
            txt = _norm(el.inner_text(timeout=800))
        except Exception:
            continue
        if not txt or len(txt) > 900:
            continue
        if h in txt and a in txt:
            # prefer the smallest (most specific) container
            if best is None or len(txt) < len(best[0]):
                best = (txt, el)
    return best[1] if best else None


def _extract_rows(page, source_tag):
    """Per visible market section: nearest header text = market name.
    Odds buttons: class contains odd/coef, data-odds attr, or button text
    parsing as float in [1.01, 50]."""
    js = """() => {
      const out = [];
      const isOddText = (t) => {
        const v = parseFloat(t);
        return /^[0-9]{1,2}(\\.[0-9]{1,2})?$/.test((t||'').trim()) && v >= 1.01 && v <= 50;
      };
      const oddEls = Array.from(document.querySelectorAll(
        "button, [class*='odd' i], [class*='coef' i], [data-odds], [role='button']"))
        .filter(e => e.offsetParent !== null);
      // find the nearest preceding header for a market name
      function marketFor(el) {
        let node = el, hops = 0;
        while (node && hops++ < 8) {
          let sib = node.previousElementSibling, sHops = 0;
          while (sib && sHops++ < 6) {
            const hd = sib.matches("h1,h2,h3,h4,[class*='market' i],[class*='title' i],[class*='header' i]")
              ? sib : sib.querySelector("h1,h2,h3,h4,[class*='market' i],[class*='title' i],[class*='header' i]");
            if (hd) { const t = (hd.innerText || '').trim(); if (t && t.length < 80) return t.split('\\n')[0]; }
            sib = sib.previousElementSibling;
          }
          node = node.parentElement;
        }
        return 'Main';
      }
      // selection label: closest small text before the odd within its group
      function selFor(el) {
        let p = el.closest("div,li,tr,section") || el.parentElement;
        for (let i = 0; p && i < 3; i++) {
          const t = (p.innerText || '').trim();
          if (t && t.length < 120) {
            const parts = t.split('\\n').map(s => s.trim()).filter(Boolean);
            for (const s of parts) if (!isOddText(s)) return s.slice(0, 60);
          }
          p = p.parentElement;
        }
        return '?';
      }
      const seen = new Set();
      for (const e of oddEls) {
        let val = null;
        const dattr = e.getAttribute && e.getAttribute('data-odds');
        if (dattr && isOddText(dattr)) val = parseFloat(dattr);
        if (val === null) { const t = (e.innerText || '').trim(); if (isOddText(t)) val = parseFloat(t); }
        if (val === null) continue;
        const market = marketFor(e);
        const selection = selFor(e);
        const key = market + '|' + selection + '|' + val;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({market, selection, decimal_odds: val});
      }
      return out;
    }"""
    rows = []
    try:
        raw = page.evaluate(js) or []
    except Exception:
        raw = []
    for r in raw:
        try:
            mkt = str(r.get("market") or "Main")[:80]
            sel = str(r.get("selection") or "?")[:60]
            odd = _valid_odd(r.get("decimal_odds"))
        except Exception:
            continue
        if odd is None:
            continue
        rows.append({"market": mkt, "selection": sel,
                     "decimal_odds": odd, "source": source_tag})
    return rows


def _expand_market_tabs(page):
    """Click up to 6 market tabs if present (never raises)."""
    clicked = 0
    for tab in _MARKET_TABS:
        if clicked >= 6:
            break
        try:
            els = page.query_selector_all(
                f"[role='tab'], a, button, div >> text={tab}")
            target = None
            for e in els:
                try:
                    t = _norm(e.inner_text(timeout=500))
                    if t == tab.lower() and e.is_visible():
                        target = e
                        break
                except Exception:
                    continue
            if target:
                target.click(timeout=2000)
                page.wait_for_timeout(1200)
                clicked += 1
        except Exception:
            continue
    return clicked


def _classify_nav_error(err_str):
    """SPEAK-UP: map a raw playwright navigation exception to an honest status.
    waf      -> TLS/HTTP-level blocks & connection resets (Kenyan WAF behaviour)
    timeout  -> navigations that exceeded the budget
    nav      -> everything else (DNS, crash, protocol errors)"""
    en = (err_str or "").lower()
    if "timeout" in en or "exceeded" in en:
        return "waf" if ("403" in en or "cloudflare" in en) else "timeout"
    if any(k in en for k in ("403", "429", "405", "cloudflare", "access denied",
                             "forbidden")):
        return "waf"
    # connection reset / empty response / handshake failures == WAF fingerprinting
    if any(k in en for k in ("empty response", "connection reset",
                             "connection closed", "ssl", "tls",
                             "certificate", "handshake", "net::err_",
                             "target closed")):
        return "waf"
    return "nav"


def _json_fallback(source_key, home, away, deadline):
    """SPEAK-UP: when HTML nav is blocked, probe the book's JSON XHR endpoints
    with plain urllib (mobile UA). Best-effort: returns (rows, status_or_None);
    status None means 'fallback also failed, keep the HTML status'."""
    import json as _json
    import urllib.request as _ur
    tag = _SOURCES[source_key]["tag"]
    h, a = _norm(home), _norm(away)
    best = None
    for u in _JSON_FALLBACKS.get(source_key, []):
        if time.time() > deadline:
            break
        try:
            req = _ur.Request(u, headers={"User-Agent": MOBILE_UA,
                                          "Accept": "application/json"})
            with _ur.urlopen(req, timeout=10) as r:
                ctype = (r.headers.get("Content-Type") or "")
                body = r.read()
            if "json" not in ctype.lower():
                best = best or "html_not_json"
                continue  # SPA fallback shell — not a real API
            data = _json.loads(body.decode("utf-8", "replace"))
        except Exception as e:
            cls = _classify_nav_error(str(e))
            best = {"waf": "waf", "timeout": "timeout"}.get(cls, "nav")
            best = f"{best}:{u.split('/')[2]}"
            continue
        rows = []

        def walk(node):
            try:
                if isinstance(node, dict):
                    txt = _norm(json.dumps(list(node.values()))[:600]) \
                        if node else ""
                    if (h and a and h in txt and a in txt) or \
                       (h and a and a in txt and h in txt):
                        for v in node.values():
                            ov = _valid_odd(v)
                            if ov:
                                rows.append({"market": "Main",
                                             "selection": "?",
                                             "decimal_odds": ov,
                                             "source": tag})
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            except Exception:
                pass
        walk(data)
        if rows:
            return rows, f"ok({len(rows)}) [json-fallback]"
        best = best or "json_no_fixture"
    return [], None


def _scout_one(source_key, home, away, deadline):
    """Drive chromium for one source. Returns (rows, status).
    SPEAK-UP hardening: mobile UA + iPhone viewport, per-URL retry (one extra
    attempt), longer nav budget, wait-for-selector on odds elements, and a
    JSON XHR fallback when HTML nav looks WAF-blocked. Statuses distinguish
    waf | timeout | nav."""
    cfg = _SOURCES[source_key]
    tag = cfg["tag"]
    if not _PLAYWRIGHT_OK:
        return [], "crash(playwright not installed)"
    remaining = deadline - time.time()
    if remaining <= 5:
        return [], "timeout"
    rows = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"])
            try:
                ctx = browser.new_context(user_agent=MOBILE_UA, viewport={
                    "width": 390, "height": 844}, locale="en-GB",
                    is_mobile=True, has_touch=True)
                ctx.set_default_timeout(20000)
                page = ctx.new_page()
                loaded = False
                last_cls = "nav"
                for url in cfg["urls"]:
                    if time.time() >= deadline - 8:
                        break
                    for attempt in range(2):  # SPEAK-UP: one retry per URL
                        if time.time() >= deadline - 8:
                            break
                        try:
                            resp = page.goto(
                                url, wait_until="domcontentloaded",
                                timeout=min(45000,
                                            max(10000, int((deadline - time.time()) * 1000))))
                            if resp and resp.status() in (403, 405, 429, 521):
                                last_cls = "waf"
                                continue
                            loaded = True
                            break
                        except Exception as e:
                            last_cls = _classify_nav_error(str(e))
                            if last_cls == "timeout":
                                break  # no point retrying a dead clock
                            time.sleep(1.5 + attempt)  # brief backoff, then retry
                    if loaded:
                        break
                if not loaded:
                    # SPEAK-UP: HTML nav blocked -> try JSON XHR fallback
                    jrows, jstat = _json_fallback(source_key, home, away,
                                                  deadline - 5)
                    if jstat:
                        return jrows, jstat
                    return [], f"{last_cls} ({tag} html+json both failed)"
                try:
                    # SPEAK-UP: wait for odds elements instead of a blind sleep
                    page.wait_for_selector(
                        "[class*='odd'], [class*='coef'], [data-odds], button",
                        timeout=12000)
                except Exception:
                    try:
                        page.wait_for_timeout(4000)  # SPA hydration grace
                    except Exception:
                        pass
                _dismiss_cookie_banner(page)
                el = _find_fixture_element(page, home, away)
                if el is None:
                    return [], "not_found"
                try:
                    el.scroll_into_view_if_needed(timeout=4000)
                    el.click(timeout=4000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                # extraction loop across market tabs, capped by deadline
                seen_keys = set()
                while time.time() < deadline - 3:
                    batch = _extract_rows(page, tag)
                    for r in batch:
                        k = (r["market"], r["selection"], r["decimal_odds"])
                        if k not in seen_keys:
                            seen_keys.add(k)
                            rows.append(r)
                    if not _expand_market_tabs(page):
                        break
                    # after expanding once, keep extracting until cap; guard loops
                    nxt = _extract_rows(page, tag)
                    grew = False
                    for r in nxt:
                        k = (r["market"], r["selection"], r["decimal_odds"])
                        if k not in seen_keys:
                            seen_keys.add(k)
                            rows.append(r)
                            grew = True
                    if not grew:
                        break
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        msg = re.sub(r"\s+", " ", str(e))[:60]
        cls = _classify_nav_error(msg)
        if cls == "timeout":
            return rows, "timeout"
        return rows, f"crash({cls}:{msg})"
    if time.time() >= deadline and not rows:
        return [], "timeout"
    return rows, f"ok({len(rows)})"


def scout_fixture(home, away, timeout_s=60, source="betika"):
    """Scout one fixture on a Kenyan bookmaker. NEVER raises.

    source: "betika" | "odibets". Returns (rows, status) where each row is
    {market, selection, decimal_odds, source:"BETIKA"|"ODIBETS"}.
    """
    try:
        if source not in _SOURCES:
            return [], "crash(unknown source)"
        deadline = time.time() + max(10, float(timeout_s))
        return _scout_one(source, home, away, deadline)
    except Exception as e:
        return [], f"crash({re.sub(chr(10), ' ', str(e))[:60]})"


def scout_both(home, away, betika_timeout_s=60, odibets_timeout_s=45):
    """Betika first, then Odibets best-effort. Returns (rows, statuses dict)."""
    rows, statuses = [], {}
    try:
        r, s = scout_fixture(home, away, betika_timeout_s, source="betika")
        statuses["BETIKA"] = s
        rows.extend(r)
    except Exception as e:  # belt & braces — scout_fixture never raises
        statuses["BETIKA"] = f"crash({type(e).__name__})"
    try:
        r, s = scout_fixture(home, away, odibets_timeout_s, source="odibets")
        statuses["ODIBETS"] = s
        rows.extend(r)
    except Exception as e:
        statuses["ODIBETS"] = f"crash({type(e).__name__})"
    return rows, statuses
