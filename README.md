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
