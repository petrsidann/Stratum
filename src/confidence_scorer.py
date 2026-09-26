"""Stratum Confidence Scorer (Phase 5) — the Filter.

Turns a raw edge into a single 0-100 ``confidence_score`` so the UI can show
what matters and hide what doesn't. The weighting encodes the framework's
hierarchy of evidence:

    Book Agreement  >  Edge Size  >  Data Recency  (+ historical win rate)

A price confirmed by many independent books is far more trustworthy than a
big edge seen once on one soft book; a stale quote decays toward zero no
matter how pretty it looks.

Rules enforced here:
  * Hard cap at CONFIDENCE_CAP (95) — Stratum never claims 100% certainty.
  * Hard floor at 0.
  * Pure function, stdlib only, deterministic — trivially unit-testable.
  * Missing inputs are treated as *unknown* (neutral/decayed), never as a
    lucky default that inflates the score.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("stratum.confidence")

# --- Component weights (must sum to 1.0). Spec ordering: agreement first. --
W_AGREEMENT = 0.40   # number of independent books confirming the direction
W_EDGE = 0.30        # size of the edge vs the market mean
W_RECENCY = 0.15     # freshness of the underlying quotes
W_HISTORY = 0.15     # historical win rate of similar signals

CONFIDENCE_CAP = 95          # never claim 100% certainty
CONFIDENCE_FLOOR = 0
DEFAULT_MIN_CONFIDENCE = 60  # UI filter default: only show signals above this

AGREEMENT_FULL_COUNT = 4     # >= 4 agreeing books saturates that component
EDGE_FULL_PCT = 5.0          # >= 5% edge saturates that component
RECENCY_HALF_LIFE_SECONDS = 900.0  # data halves in trust every 15 minutes


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _agreement_component(book_agreement_count: Optional[int]) -> float:
    """0..1 — linear in the number of independent books agreeing, saturating
    at AGREEMENT_FULL_COUNT. Unknown count contributes nothing."""
    if book_agreement_count is None or book_agreement_count <= 0:
        return 0.0
    return _clamp(float(book_agreement_count) / AGREEMENT_FULL_COUNT, 0.0, 1.0)


def _edge_component(edge_pct: Optional[float]) -> float:
    """0..1 — magnitude of the edge normalized against EDGE_FULL_PCT.

    Negative edges (we're behind the market) contribute 0: there is nothing
    to be confident about when the "edge" points the wrong way."""
    if edge_pct is None or edge_pct <= 0:
        return 0.0
    return _clamp(float(edge_pct) / EDGE_FULL_PCT, 0.0, 1.0)


def _recency_component(data_age_seconds: Optional[int]) -> float:
    """1.0 for perfectly fresh data, exponential decay with a fixed half
    life. Unknown age is treated as fully stale (never generous)."""
    if data_age_seconds is None or data_age_seconds < 0:
        return 0.0
    ages = float(data_age_seconds) / RECENCY_HALF_LIFE_SECONDS
    return _clamp(0.5 ** ages, 0.0, 1.0)


def _history_component(historical_win_rate: Optional[float]) -> float:
    """0..1 — historical win rate of *similar* signals, normalized against a
    60% benchmark (a market-beating hit rate). Unknown history contributes
    nothing rather than a flattering 50/50 guess."""
    if historical_win_rate is None or historical_win_rate <= 0:
        return 0.0
    rate = float(historical_win_rate)
    if rate > 1.0:  # tolerate both 0..1 fractions and 0..100 percents
        rate = rate / 100.0
    return _clamp(rate / 0.60, 0.0, 1.0)


def calculate_confidence(
    edge_pct: Optional[float],
    book_agreement_count: Optional[int],
    data_age_seconds: Optional[int],
    historical_win_rate: Optional[float],
) -> int:
    """Weighted confidence score in [0, 95] for one detected edge.

    Formula (weights at module top, Book Agreement > Edge Size > Recency):

        score = 100 * (0.40*agreement + 0.30*edge + 0.15*recency + 0.15*history)

    then clamped to [CONFIDENCE_FLOOR, CONFIDENCE_CAP]. Any input may be
    None — unknowns decay the score toward zero, they never inflate it.
    """
    try:
        composite = (
            W_AGREEMENT * _agreement_component(book_agreement_count)
            + W_EDGE * _edge_component(edge_pct)
            + W_RECENCY * _recency_component(data_age_seconds)
            + W_HISTORY * _history_component(historical_win_rate)
        )
    except (TypeError, ValueError) as exc:  # defensive: bad types -> no score
        logger.warning("calculate_confidence got unusable inputs: %s", exc)
        return CONFIDENCE_FLOOR
    score = int(round(_clamp(composite * 100.0, CONFIDENCE_FLOOR, CONFIDENCE_CAP)))
    return score


def confidence_band(score: Optional[int]) -> str:
    """UI color band for a confidence score: green >80, yellow 60-80, grey <60."""
    if score is None:
        return "grey"
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "grey"
    if s > 80:
        return "green"
    if s >= 60:
        return "yellow"
    return "grey"


def passes_threshold(score: Optional[int], min_confidence: int = DEFAULT_MIN_CONFIDENCE) -> bool:
    """True when a signal is strong enough to surface in the UI."""
    try:
        return int(score if score is not None else -1) >= int(min_confidence)
    except (TypeError, ValueError):
        return False
