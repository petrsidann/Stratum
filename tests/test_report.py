"""Tests for src/report.py — chart factories only (no rendering, no network)."""

import plotly.graph_objects as go

from src import report


def test_chart_novig_returns_figure_with_two_traces():
    fig = report.chart_novig(0.5798, 0.4202, 0.60, 0.4348)
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 2  # implied + fair bars
    names = [t.name for t in fig.data]
    assert "Implied (with vig)" in names and "Fair (no-vig)" in names
    assert fig.layout.template is not None
    assert fig.layout.paper_bgcolor.upper() == "#0E1116"


def test_chart_kelly_returns_figure_with_two_traces():
    fig = report.chart_kelly(edge_pct=4.3, stake=0.0731, bankroll=1000.0)
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 2  # stake + untouched (stacked bar)
    total_x = sum(t.x[0] for t in fig.data)
    assert total_x == round(1000.0, 6) or abs(total_x - 1000.0) < 1e-9


def test_chart_rlm_flags_reverse_line_movement():
    fig = report.chart_rlm(public_pct=68.0, line_dir="up")
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 1
    # RLM case must carry an arrow annotation.
    anns = [a for a in fig.layout.annotations if getattr(a, "showarrow", False)]
    assert anns and "OPPOSITE" in anns[0].text


def test_chart_rlm_no_flag_when_consistent():
    fig = report.chart_rlm(public_pct=68.0, line_dir="down")
    assert isinstance(fig, go.Figure)
    arrow_anns = [a for a in fig.layout.annotations if getattr(a, "showarrow", False)]
    assert not arrow_anns
