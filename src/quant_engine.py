"""Stratum Quant Engine — pure math utilities for sports betting analysis.

No I/O, no dependencies beyond the standard library. Everything here is
deterministic and unit-testable.
"""

from __future__ import annotations


def american_to_decimal(american_odds: float) -> float:
    """Convert American (moneyline) odds to decimal odds.

    Examples:
        +100 -> 2.00, -150 -> 1.6667, +130 -> 2.30
    """
    odds = float(american_odds)
    if odds == 0:
        raise ValueError("American odds cannot be zero.")
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def implied_probability(american_odds: float) -> float:
    """Return the bookmaker's implied probability (0..1) from American odds."""
    return 1.0 / american_to_decimal(american_odds)


def remove_vig_two_way(american_odds_a: float, american_odds_b: float) -> tuple[float, float]:
    """Strip the bookmaker's margin from a two-way market.

    Normalizes the two implied probabilities so they sum to 1.0.

    Example:
        (-150, +130) -> (0.5798, 0.4202)  i.e. 57.98% / 42.02%
    """
    p_a = implied_probability(american_odds_a)
    p_b = implied_probability(american_odds_b)
    total = p_a + p_b
    if total <= 0:
        raise ValueError("Implied probabilities must be positive.")
    return p_a / total, p_b / total


def kelly_criterion(probability: float, decimal_odds: float, fraction: float = 1.0) -> float:
    """Full (or fractional) Kelly stake as a proportion of bankroll.

    f* = fraction * (b*p - q) / b, where b = decimal_odds - 1, q = 1 - p.
    Returns 0.0 for negative edge (never bet).
    """
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be in (0, 1).")
    if decimal_odds <= 1.0:
        raise ValueError("decimal_odds must be greater than 1.")
    b = decimal_odds - 1.0
    q = 1.0 - probability
    edge = (b * probability - q) / b
    stake = fraction * edge
    return max(0.0, stake)


# ---------------------------------------------------------------------------
# Phase 3 helpers — every derived number in the scanner / signals / CLV
# auditor flows through these functions. Views and scanners NEVER inline math.
# ---------------------------------------------------------------------------

def vig_pct_two_way(american_odds_a: float, american_odds_b: float) -> float:
    """Overround of a two-way market as a percentage (e.g. 3.48 for ~3.5% vig)."""
    p_a = implied_probability(american_odds_a)
    p_b = implied_probability(american_odds_b)
    return (p_a + p_b - 1.0) * 100.0


def ev_pct(probability: float, american_odds: float) -> float:
    """Expected value (%) per $1 staked at the given American odds."""
    dec = american_to_decimal(american_odds)
    return (probability * (dec - 1.0) - (1.0 - probability)) * 100.0


def arbitrage_pct(best_side_a_american: float, best_side_b_american: float) -> float:
    """Arbitrage margin (%) for a two-way market priced across different books.

    Sum the implied probabilities of the BEST price on each side. If the total
    is under 100%, we have an arb and return the margin (e.g. 2.0 for 2%).
    Otherwise return exactly 0.0. This is the ONLY arb math in the codebase.
    """
    if not best_side_a_american or not best_side_b_american:
        return 0.0
    total = implied_probability(best_side_a_american) + implied_probability(best_side_b_american)
    if total < 1.0:
        return (1.0 - total) * 100.0
    return 0.0


def clv_pct(odds_placed_american: float, closing_american: float) -> float:
    """Closing Line Value (%) in DECIMAL space: (dec_close / dec_placed - 1) * 100.

    Positive CLV means we beat the close. Converting through
    ``american_to_decimal`` first is mandatory — dividing raw American numbers
    (e.g. -110 -> -105) produces meaningless results. All callers route
    through this function so the definition stays consistent.
    """
    if not odds_placed_american or not closing_american:
        raise ValueError("CLV needs both placed and closing American odds.")
    dec_placed = american_to_decimal(odds_placed_american)
    dec_close = american_to_decimal(closing_american)
    return (dec_close / dec_placed - 1.0) * 100.0
