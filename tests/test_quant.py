"""Tests for the Stratum quant engine (Phase 1)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.quant_engine import (
    american_to_decimal,
    implied_probability,
    kelly_criterion,
    remove_vig_two_way,
)


def test_american_to_decimal_positive():
    assert american_to_decimal(130) == pytest.approx(2.30)
    assert american_to_decimal(100) == pytest.approx(2.00)


def test_american_to_decimal_negative():
    assert american_to_decimal(-150) == pytest.approx(1.6666667, rel=1e-5)


def test_american_to_decimal_zero_raises():
    with pytest.raises(ValueError):
        american_to_decimal(0)


def test_implied_probability():
    assert implied_probability(-150) == pytest.approx(0.6, rel=1e-5)
    assert implied_probability(130) == pytest.approx(1 / 2.30, rel=1e-5)


def test_remove_vig_two_way_minus150_plus130():
    """The canonical Phase 1 check: -150/+130 -> 57.98% / 42.02%."""
    p_home, p_away = remove_vig_two_way(-150, 130)
    assert round(p_home * 100, 2) == 57.98
    assert round(p_away * 100, 2) == 42.02
    assert p_home + p_away == pytest.approx(1.0)


def test_kelly_positive_edge():
    # Fair p=0.6 at decimal 2.3 odds -> f* = (1.3*0.6 - 0.4)/1.3 ~ 0.2923
    stake = kelly_criterion(0.60, 2.30)
    assert stake == pytest.approx(0.2923077, rel=1e-4)


def test_kelly_no_edge_returns_zero():
    # p=0.4 at even money is negative edge -> no bet
    assert kelly_criterion(0.40, 2.00) == 0.0


def test_kelly_fractional():
    full = kelly_criterion(0.60, 2.30)
    half = kelly_criterion(0.60, 2.30, fraction=0.5)
    assert half == pytest.approx(full / 2)


def test_kelly_invalid_inputs():
    with pytest.raises(ValueError):
        kelly_criterion(1.5, 2.0)
    with pytest.raises(ValueError):
        kelly_criterion(0.5, 1.0)
