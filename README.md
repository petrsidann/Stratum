# Stratum v0.1 - Quant Engine

Stratum is a sports-betting quant engine built with Streamlit. Phase 1 delivers
the core math library (odds conversion, de-vig, Kelly criterion), a SQLite
persistence layer, and a Streamlit app shell.

## Quick Start

```bash
pip install -r requirements.txt
streamlit run main.py
```

## Layout

- `config.py` — safe environment variable loading (via `.env`)
- `src/quant_engine.py` — pure math: american_to_decimal, implied_probability, remove_vig_two_way, kelly_criterion
- `src/database.py` — SQLite init + CRUD helpers
- `tests/test_quant.py` — pytest suite (e.g. -150/+130 -> 57.98%/42.02%)
- `main.py` — Streamlit entrypoint ("Stratum Ready")

## Phase 2 — AI Brain + Free Data Layer

- **The rule:** the LLM (Groq → Gemini Flash fallback) only *reads* text; all math lives in `src/quant_engine.py`. Any number not present in the source text is discarded.
- **Never fabricates prices:** if fetch/extraction is incomplete, a manual-entry form appears prefilled with whatever was found. "Unknown" is valid; a made-up number is not.
- **Free data:** Open-Meteo weather (no key), polite DuckDuckGo HTML snippets (real User-Agent, 1s sleep).
- **Charts:** `src/report.py` — no-vig breakdown, quarter-Kelly stake, RLM signal (dark theme #0E1116, green #2EE6A6 / red #FF4D4D).

```bash
streamlit run main.py          # works with zero keys and zero network
python -m pytest tests/ -q     # fully offline, all network mocked
```
