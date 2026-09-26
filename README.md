# Stratum v1.0 — Quant Engine · Ghost Server

**Logic in Actions, UI in Pages.** No servers, no sessions, no memory leaks.

Stratum is a sports-betting quant engine: it scans multi-book market boards,
de-vigs prices into fair odds, detects STEAM / Reverse-Line-Movement /
Stale-Line / Arbitrage signals, scores every edge for confidence (capped at
95 — we never claim certainty), and seals results into an immutable audit
ledger. The compute layer runs **inside GitHub Actions**; the dashboard is a
**static React app on GitHub Pages** that only ever fetches committed JSON.

```
┌─────────────────────────────┐     ┌──────────────────────────┐
│  GitHub Actions (cron */5)  │     │   GitHub Pages (static)  │
│  scripts/run_daily_scan.py  │──►──│   frontend/ (React+Vite) │
│  src/ quant engine          │commit│   reads data/*.json     │
│  → data/latest_scan.json    │ push│   Recharts dark dash-   │
│  → data/portfolio_stats.json│     │   board, mobile-first   │
│  → data/history.csv         │     │   NO server-side render │
└─────────────────────────────┘     └──────────────────────────┘
```

## 🚀 Deploy to GitHub Actions + GitHub Pages

### 1. Enable the Scanner (backend)

1. Push this repo to GitHub.
2. Go to **Repo → Settings → Secrets and variables → Actions**.
3. Add repository secrets (both optional — the scan completes without them):
   - `GROQ_API_KEY` — primary LLM for market-context insights
   - `GOOGLE_API_KEY` — Gemini Flash fallback
4. The workflow **`.github/workflows/deploy.yml`** ("Deploy Pipeline
   (Scan → Build → Pages)") is the single pipeline — it fully replaces the
   old scanner workflow.
   Job A runs the scanner on `*/5 * * * *` cron with **verbose logging**
   (`STRATUM_LOG_LEVEL=DEBUG`) and **fails hard** — "Scanner produced
   insufficient data" — if `data/latest_scan.json` comes out under 5 KB or
   with zero matches, so empty/mock JSONs can never be committed or published.

### 2. Enable the Dashboard (frontend)

The pipeline builds and deploys the React app automatically — Job B runs
`npm install && npm run build` in `frontend/` (Node 20), Job C copies the
fresh scan JSON into `dist/data/` and publishes `frontend/dist` straight to
Pages via `actions/upload-pages-artifact@v3`. Local rebuild (optional):

```bash
cd frontend
npm install
npm run build        # emits frontend/dist/
```

> ⚠️ **REQUIRED ONE-TIME SETTING — DO NOT SKIP.** Go to
> **Repo → Settings → Pages → Source** and select **"GitHub Actions"**.
> It MUST be "GitHub Actions" — **NOT "Deploy from a branch"**.
> Choosing "Deploy from a branch" makes GitHub run **Jekyll** (Ruby) over
> the repo, which crashes on our React/Vite output (Liquid parse errors).
> The "GitHub Actions" source disables Jekyll completely; no `_config.yml`
> is needed anywhere. With this setting, the deploy job pushes the static
> bundle directly — nothing else touches Pages.

The app reads its data from `./data/latest_scan.json` (relative to the page,
so it works under any `/repo/` subpath); you can override at runtime with
`?dataBase=<url>`.

### 3. Verify

- **Actions tab**: "Deploy Pipeline (Scan → Build → Pages)" goes green:
  Job A logs "Generated X matches, Y markets, Z signals" plus byte counts,
  Job B uploads `frontend-dist`, Job C deploys.
- Pages URL renders the dark dashboard fed by the bundled scan; if the
  scanner ever produces a thin board, Job A fails and Pages keeps the last
  good deployment — **never a white screen, never an empty feed**.

## Local Development

```bash
python -m pip install -r requirements-dev.txt
python scripts/run_daily_scan.py           # writes data/*.json locally
pytest tests/ -q                           # fully offline, all network mocked

cd frontend && npm install && npm run dev  # Vite dev server on :5173
```

## Layout

| Path | Role |
|---|---|
| `.github/workflows/deploy.yml` | 3-job pipeline: scan (5 KB gate) → Vite build → Pages deploy (no Jekyll) |
| `scripts/run_daily_scan.py` | GHA entry point: scan → fair odds → signals → confidence → JSON |
| `src/quant_engine.py` | Pure math (odds conversion, de-vig, Kelly, EV, arb, CLV) |
| `src/market_scanner.py` | Multi-book, 200-market ingestion + stale-line consensus |
| `src/signal_detector.py` | Steam / RLM / Arbitrage detectors |
| `src/confidence_scorer.py` | Weighted 0–95 confidence (agreement > edge > recency > history) |
| `src/reasoning_engine.py` | Groq → Gemini insight text (numbers must exist in source) |
| `src/clv_auditor.py` / `src/audit_ledger.py` | Immutable bet/scan ledger, ROI & CLV reports |
| `src/database.py` | SQLite schema (`games`, `bets_log`, `scan_history`, `market_stats`) |
| `data/` | Published artifacts (bot-owned; don't hand-edit) |
| `frontend/` | Vite + React + Tailwind + Recharts static dashboard |
| `tests/test_scan_workflow.py` | Ghost-server contract tests (offline, mocked) |

## Reliability Contract

- **Never crashes the workflow:** per-match failures are logged and skipped;
  the run still publishes valid output marked `"status": "degraded"`.
- **Timeouts + cache:** scraper calls carry request timeouts; weather/news
  context is cached (`.cache/context_cache.json`, TTL < cron interval) so
  back-to-back runs make zero redundant API calls.
- **Clean data:** `latest_scan.json` is schema-validated (typed confidences
  in [0,95], required keys) *before* writing; files are written atomically
  (`tmp` + `os.replace`) so readers never observe partial JSON.
- **Honesty:** sample/degraded data is always labeled `data_source="sample"`
  and surfaced with a loud warning badge. Unknown is valid; fabricated is not.

## Legacy Streamlit Shell (retired)

`main.py` + `.streamlit/` remain in-repo as the deprecated interactive
prototype (kept working under `requirements-dev.txt` for local exploration:
`streamlit run main.py`). Production is Actions + Pages — do not deploy the
Streamlit app; it was retired due to Cloud memory-loop instability.
