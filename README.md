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
4. The workflow **`.github/workflows/scan-engine.yml`** ("Stratum Market
   Scanner") now runs automatically on `*/5 * * * *` cron. Trigger one run
   immediately via **Actions → Stratum Market Scanner → Run workflow**
   (manual dispatch). Each run commits fresh `data/*.json` back to `main`.

### 2. Enable the Dashboard (frontend)

Build the static site once per code change (locally or in CI):

```bash
cd frontend
npm install
npm run build        # emits frontend/dist/
```

Then serve it from the branch:

1. Commit `frontend/dist/` (or add a Pages build workflow — see tip below).
2. Go to **Repo → Settings → Pages**.
3. **Source:** *Deploy from branch* → branch `main` → folder **`/frontend/dist`** → Save.
4. Open `https://<user>.github.io/<repo>/` — the app auto-fetches
   `https://raw.githubusercontent.com/<owner>/<repo>/main/data/latest_scan.json`.
   Tip: you can override the data source at runtime with `?dataBase=<url>`.

*(Optional)* Replace "Commit dist" with a second workflow that runs
`npm ci && npm run build && npx gh-pages -d frontend/dist` on pushes touching
`frontend/**`, and stop committing `dist/` to the branch.

### 3. Verify

- **Actions tab**: "Stratum Market Scanner" green every 5 minutes.
- Repo root shows bot commits `chore(scan): update market data …`.
- Pages URL renders the dark dashboard; if the scanner hasn't run yet you'll
  get a friendly red banner, **never a white screen**.

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
| `.github/workflows/scan-engine.yml` | Cron + manual dispatcher; commits validated JSON |
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
