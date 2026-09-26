"""Stratum v0.1 — Streamlit entrypoint (Phase 1 placeholder)."""

try:
    import streamlit as st

    st.title("Stratum v0.1 - Quant Engine")
    st.write("Stratum Ready")
except ImportError:  # Allow `python main.py` without Streamlit installed.
    print("Stratum Ready")
