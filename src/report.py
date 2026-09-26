"""Stratum visual report generator (Phase 2) — Plotly, dark brand theme.

All functions return a ``plotly.graph_objects.Figure`` and never call
``.show()``; Streamlit renders them. Pure drawing: no network, no math beyond
percent formatting of values the caller already computed in quant_engine.
"""

from __future__ import annotations

import plotly.graph_objects as go

# Brand palette
BG = "#0E1116"          # paper/plot background
GREEN = "#2EE6A6"       # accent positive
RED = "#FF4D4D"         # accent negative
GREY = "#8B94A3"
FONT = dict(color="#E6E9EF")


def _base_layout(fig: go.Figure, title: str) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=title, font=dict(size=16, color="#E6E9EF")),
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color="#E6E9EF"),
        margin=dict(l=40, r=30, t=60, b=40),
        height=380,
    )
    return fig


def chart_novig(
    fair_home: float,
    fair_away: float,
    imp_home: float,
    imp_away: float,
) -> go.Figure:
    """Grouped bars: implied probabilities (with vig) vs no-vig fair probs.

    Probabilities are fractions (0..1); the book's vig% is annotated on top.
    """
    vig_pct = max(0.0, (imp_home + imp_away - 1.0)) * 100.0
    fig = go.Figure(
        [
            go.Bar(
                name="Implied (with vig)",
                x=["Home", "Away"],
                y=[imp_home * 100, imp_away * 100],
                marker_color=RED,
                text=[f"{imp_home * 100:.1f}%", f"{imp_away * 100:.1f}%"],
                textposition="outside",
            ),
            go.Bar(
                name="Fair (no-vig)",
                x=["Home", "Away"],
                y=[fair_home * 100, fair_away * 100],
                marker_color=GREEN,
                text=[f"{fair_home * 100:.1f}%", f"{fair_away * 100:.1f}%"],
                textposition="outside",
            ),
        ]
    )
    _base_layout(fig, "Vig Breakdown — Implied vs Fair")
    fig.add_annotation(
        x=0.5, y=1.12, xref="paper", yref="paper", showarrow=False,
        text=f"Book vig: <b>{vig_pct:.2f}%</b>", font=dict(color=GREEN, size=13),
    )
    fig.update_yaxes(range=[0, 100], title="Probability (%)")
    fig.update_xaxes(title=None)
    return fig


def chart_kelly(edge_pct: float, stake: float, bankroll: float) -> go.Figure:
    """Horizontal bar showing the recommended quarter-Kelly stake vs bankroll."""
    stake_amt = stake * bankroll
    remaining = max(bankroll - stake_amt, 0.0)
    fig = go.Figure(
        [
            go.Bar(
                orientation="h",
                y=["Bankroll"],
                x=[stake_amt],
                name="Quarter-Kelly stake",
                marker_color=GREEN if edge_pct > 0 else RED,
                text=f"${stake_amt:,.2f}",
                textposition="inside" if stake_amt > 0 else "outside",
            ),
            go.Bar(
                orientation="h",
                y=["Bankroll"],
                x=[remaining],
                name="Untouched",
                marker_color="#2A3140",
                text=f"${remaining:,.2f}",
                textposition="inside",
            ),
        ]
    )
    fig.update_layout(barmode="stack")
    _base_layout(fig, f"Recommended Stake — edge {edge_pct:+.2f}%")
    fig.update_xaxes(title="$ Bankroll")
    return fig


def chart_rlm(public_pct: float, line_dir: str) -> go.Figure:
    """Public ticket % bar with an arrow annotation for reverse line movement.

    ``public_pct`` is 0..100. ``line_dir``: "up" | "down" | "flat" describing
    which way the home line moved. When the public leans one side but the line
    moves OPPOSITE, that is the RLM (sharp) signal — flagged with an arrow.
    """
    public_pct = float(public_pct)
    lean_side = "Home" if public_pct >= 50 else "Away"
    opposite = False
    if line_dir == "up" and public_pct > 50:
        opposite = True   # public on home, line moved up against home
    elif line_dir == "down" and public_pct < 50:
        opposite = True   # public on away, line moved down toward home
    color = RED if opposite else GREEN

    fig = go.Figure(
        [
            go.Bar(
                x=["Public tickets"],
                y=[public_pct],
                marker_color=color,
                text=f"{public_pct:.0f}% on {lean_side}",
                textposition="outside",
            )
        ]
    )
    _base_layout(fig, "Public Money vs Line Movement")
    fig.update_yaxes(range=[0, 100], title="Public %")

    if opposite:
        fig.add_annotation(
            x=0, y=min(public_pct + 12, 96), showarrow=True, arrowhead=2,
            ax=0, ay=-40,
            text="<b>RLM:</b> line moved OPPOSITE the public (sharp side)",
            font=dict(color=RED, size=12),
            arrowcolor=RED,
        )
    else:
        fig.add_annotation(
            x=0, y=min(public_pct + 10, 95), showarrow=False,
            text=f"Line {line_dir or 'flat'} — consistent with public, no RLM flag",
            font=dict(color=GREY, size=12),
        )
    return fig
