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
