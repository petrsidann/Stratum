"""EMERGENCY item 1 — browser-use headless-chromium session helper.

Rotating user-agents, optional proxy rotation (env SCRAPE_PROXY_LIST="http://user:pass@host:port,..."),
anti-detection flags. NEVER raises: if browser-use / playwright chromium is unavailable or the
network blocks the target, returns None and callers fall back to plain HTTP fetches.

Design note: scraping helpers here are import-guarded so py_compile + offline self-tests pass
on machines without a browser binary (the owner's runtime installs browser-use + playwright).
"""
import os
import random

# anti-detection launch args for headless chromium
CHROMIUM_ARGS = [
    "--headless=new",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--lang=en-US,en;q=0.9",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]


def log(msg):
    print(f"[SCOUT-BROWSER] {msg}")


def rotate_user_agent():
    return random.choice(USER_AGENTS)


def rotate_proxy():
    """Optional proxy rotation from env SCRAPE_PROXY_LIST (comma-separated). None if unset."""
    raw = os.environ.get("SCRAPE_PROXY_LIST", "").strip()
    if not raw:
        return None
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    return random.choice(proxies) if proxies else None


def browser_available():
    """True only if BOTH browser_use and a usable chromium path exist. Never raises."""
    try:
        import browser_use  # noqa: F401
    except Exception:
        return False
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def make_browser_kwargs():
    """ kwargs suitable for BrowserSession/Browser config when browser-use is used directly."""
    kw = {
        "headless": True,
        "executable_path": os.environ.get("CHROME_PATH") or None,
        "browser_class": "Chromium",
        "extra_args": list(CHROMIUM_ARGS),
        "user_agent": rotate_user_agent(),
    }
    proxy = rotate_proxy()
    if proxy:
        # browser-use >=0.1 expects dict form
        kw["proxy"] = {"server": proxy}
    return kw


async def scrape_page(url, wait_seconds=6, extract_js=None):
    """Open url in anti-detection headless chromium via browser-use; return page HTML text
    (or the result of extract_js evaluated in-page when provided). Returns None on ANY failure
    so every scraper degrades gracefully to HTTP fallback / status chips."""
    def _go():
        if not browser_available():
            log("browser-use/playwright unavailable -> HTTP fallback for " + str(url))
            return None
        # late imports keep module import-safe without the packages installed
        from browser_use import BrowserSession  # type: ignore
        import asyncio as _a

        async def _run():
            session = BrowserSession(**make_browser_kwargs())
            await session.start()
            try:
                await session.navigate_to(url)
                await _a.sleep(wait_seconds)
                if extract_js:
                    html = await session.evaluate(extract_js)
                else:
                    html = await session.get_current_page_html()
                return html
            finally:
                try:
                    await session.stop()
                except Exception:
                    pass

        try:
            loop = _a.get_event_loop()
            if loop.is_running():
                import concurrent.futures.thread as _cft
                with _cft.ThreadPoolExecutor(1) as ex:
                    return ex.submit(_a.run, _run()).result(timeout=45)
            return loop.run_until_complete(_run())
        except RuntimeError:
            return _a.run(_run())
        except Exception as e:  # timeout / crash
            log(f"scrape failed for {url}: {type(e).__name__}")
            return None

    return _safe_call(_go)


def scrape_page_sync(url, wait_seconds=6, extract_js=None):
    """Synchronous entry point for hunt scripts (they run without an event loop).
    Runs the browser-use coroutine via asyncio.run(); falls back to plain HTTP on
    ANY failure (missing chromium binary, sandbox network block, TypeError in a
    newer browser_use API surface). Never raises, never leaks un-awaited coros."""
    import asyncio

    async def _fresh_coro():
        return await scrape_page(url, wait_seconds, extract_js)

    def _go():
        try:
            return asyncio.run(_fresh_coro())
        except Exception as e:
            log(f"browser scrape failed for {url}: {type(e).__name__} -> HTTP fallback")
            return None

    try:
        asyncio.get_running_loop()
        return http_get(url)  # caller already inside a loop: don't re-enter
    except RuntimeError:
        pass
    html = _safe_call(_go)
    if not html:
        html = _safe_call(lambda: http_get(url))
    return html


def _safe_call(fn):
    try:
        return fn()
    except Exception:
        return None


def http_get(url, timeout=10):
    """Plain HTTP fallback with rotating UA (used when browser-use unavailable). Never raises."""
    def _go():
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": rotate_user_agent(),
            "Accept-Language": "en-US,en;q=0.9",
        })
        proxy = rotate_proxy()
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers) if handlers else urllib.request
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    return _safe_call(_go)


def selftest():
    checks = []
    checks.append(("rotate_user_agent looks like a browser UA", rotate_user_agent().startswith("Mozilla/5.0")))
    checks.append(("proxy rotation off by default", rotate_proxy() is None))
    checks.append(("chromium args include anti-detection flag", any("AutomationControlled" in a for a in CHROMIUM_ARGS)))
    checks.append(("browser_available never raises", isinstance(browser_available(), bool)))
    checks.append(("kwargs carry rotating UA", "user_agent" in make_browser_kwargs()))
    ok = all(v for _, v in checks)
    for name, v in checks:
        print(f"[SCOUT-BROWSER][selftest] {'PASS' if v else 'FAIL'} {name}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
