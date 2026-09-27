"""Stratum visual report generator.

Produces dark-theme PNG charts under data/reports/ from an analyzed match
document (the dict shape emitted by src/analyzer.analyze_rows via the feed):

    Chart A  market_depth_<match_id>.png   Market Depth Heatmap
    Chart B  edge_radar_<match_id>.png     Edge Radar (top 5 EV bets, polar)
    Chart C  line_movement_<match_id>.png  Line Movement Tracker (sparklines)

Every chart is built ONLY from scraped prices that exist in the input. If a
chart cannot be produced from real data, it is skipped -- never faked.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # headless (CI-safe)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

# Brand palette
BG = "#0F111A"
SURFACE = "#1E2330"
CYAN = "#00E5FF"
GREEN = "#00FF9D"
RED = "#FF4D4D"
AMBER = "#FFB020"
TEXT = "#C8D0E0"
GRID = "#2A3040"

REPORTS_DIR_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reports")


def _safe_id(match_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", match_id or "unknown")[:80]


def _style(ax, title: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.tick_params(colors=TEXT, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.5, alpha=0.5)
    if title:
        ax.set_title(title, color=CYAN, fontsize=11, fontweight="bold", pad=10)


def _ev_color(ev: float) -> str:
    if ev >= 5:
        return GREEN
    if ev > 0:
        return CYAN
    return RED


# ---------------------------------------------------------------------------
# Chart A: Market Depth Heatmap
# ---------------------------------------------------------------------------

def chart_market_depth(match: Dict[str, Any], out_dir: str) -> Optional[str]:
    """Heatmap of EV% across markets (rows) x bookmakers (columns)."""
    markets = [m for m in match.get("markets", []) if m.get("bookmakers")]
    if len(markets) < 2:
        return None
    markets = markets[:40]  # keep the image readable

    books: List[str] = []
    for m in markets:
        for b in m["bookmakers"]:
            if b["bookmaker"] not in books:
                books.append(b["bookmaker"])
    if len(books) < 1:
        return None

    import numpy as np
    grid = np.full((len(markets), len(books)), np.nan)
    bidx = {b: i for i, b in enumerate(books)}
    for r, m in enumerate(markets):
        fair = (m.get("fair_probability") or 0) / 100.0
        for b in m["bookmakers"]:
            d = b.get("decimal") or 0
            if d > 1 and fair > 0:
                ev = (fair * (d - 1) - (1 - fair)) * 100.0
                grid[r, bidx[b["bookmaker"]]] = round(ev, 2)

    if math.isnan(grid).all():
        return None

    fig, ax = plt.subplots(figsize=(max(6, 0.45 * len(books) + 3),
                                    max(3.5, 0.28 * len(markets) + 1.6)))
    fig.patch.set_facecolor(BG)
    masked = np.ma.masked_invalid(grid)
    cmap = plt.cm.get_cmap("RdYlGn").copy()
    cmap.set_bad(SURFACE)
    vmax = max(6.0, float(np.nanmax(abs(masked)))) if not masked.all_masked() else 6.0
    im = ax.imshow(masked, cmap=cmap, vmin=-vmax, vmax=vmax, aspect="auto")

    labels = [f"{m['name']} :: {m['selection']}"[:38] for m in markets]
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, color=TEXT, fontsize=7, family="monospace")
    ax.set_xticks(range(len(books)))
    ax.set_xticklabels(books, color=TEXT, fontsize=7, rotation=45, ha="right")
    _style(ax, f"MARKET DEPTH HEATMAP - EV% | {match.get('away_team','')} @ {match.get('home_team','')}")
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.ax.tick_params(colors=TEXT, labelsize=7)
    cbar.outline.set_edgecolor(GRID)

    path = os.path.join(out_dir, f"market_depth_{_safe_id(match['id'])}.png")
    fig.savefig(path, dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Chart B: Edge Radar (polar, top 5 EV bets)
# ---------------------------------------------------------------------------

def chart_edge_radar(match: Dict[str, Any], out_dir: str) -> Optional[str]:
    edges = sorted([m for m in match.get("markets", []) if m.get("ev_percent", 0) > 0],
                   key=lambda m: -m["ev_percent"])[:5]
    if len(edges) < 2:
        return None

    axes_names = ["EV %", "Fair Prob", "Confidence", "Books", "Fresh Move"]

    def norm(m: Dict[str, Any]) -> List[float]:
        hist = m.get("movement") or []
        return [
            min(max(m.get("ev_percent", 0), 0) / 15.0, 1.0),
            min(max((m.get("fair_probability") or 0) / 100.0, 0), 1.0),
            min(max((m.get("confidence") or 0) / 100.0, 0), 1.0),
            min(m.get("num_bookmakers", 0) / 5.0, 1.0),
            1.0 if len(hist) >= 2 else 0.0,
        ]

    angles = [n / float(len(axes_names)) * 2 * math.pi for n in range(len(axes_names))]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(6.2, 6.2), subplot_kw=dict(polar=True))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(SURFACE)
    palette = [CYAN, GREEN, AMBER, RED, "#7A5CFF"]

    for i, m in enumerate(edges):
        vals = norm(m) + norm(m)[:1]
        color = palette[i % len(palette)]
        ax.plot(angles, vals, color=color, linewidth=1.8, label=f"{m['selection'][:22]} ({m['ev_percent']:+.1f}%)")
        ax.fill(angles, vals, color=color, alpha=0.10)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(axes_names, color=TEXT, fontsize=8)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["25", "50", "75", "100"], color=GRID, fontsize=6)
    ax.spines["polar"].set_color(GRID)
    ax.grid(color=GRID, linewidth=0.5, alpha=0.6)
    ax.tick_params(axis="x", pad=14)
    ax.set_title(f"EDGE RADAR - TOP {len(edges)} SIGNALS | "
                 f"{match.get('away_team','')} @ {match.get('home_team','')}",
                 color=CYAN, fontsize=11, fontweight="bold", pad=22)
    leg = ax.legend(loc="upper right", bbox_to_anchor=(1.35, 1.1), fontsize=7,
                    facecolor=SURFACE, edgecolor=GRID, labelcolor=TEXT)
    leg.set_zorder(30)

    path = os.path.join(out_dir, f"edge_radar_{_safe_id(match['id'])}.png")
    fig.savefig(path, dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Chart C: Line Movement Tracker (sparklines over scrape window)
# ---------------------------------------------------------------------------

def chart_line_movement(match: Dict[str, Any], out_dir: str) -> Optional[str]:
    movers = [m for m in match.get("markets", [])
              if len(m.get("movement") or []) >= 2][:8]
    if not movers:
        return None

    n = len(movers)
    cols = 2
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(9.5, 1.9 * rows + 0.8))
    fig.patch.set_facecolor(BG)
    axes_flat = [axes] if rows == cols == 1 else list(axes.flat)

    for idx, (ax, m) in enumerate(zip(axes_flat, movers)):
        pts = m["movement"][-48:]
        ys = [float(p[1]) for p in pts]
        xs = list(range(len(ys)))
        up = ys[-1] >= ys[0]
        color = GREEN if up else RED
        ax.set_facecolor(SURFACE)
        ax.plot(xs, ys, color=color, linewidth=1.6)
        ax.fill_between(xs, ys, min(ys) - 0.02, color=color, alpha=0.08)
        ax.scatter([xs[-1]], [ys[-1]], color=CYAN, s=14, zorder=5)
        ax.set_title(f"{m['name']} :: {m['selection']}"[:44], color=TEXT,
                     fontsize=8, loc="left")
        ax.set_xlim(min(xs) - 0.5, max(xs) + 0.5)
        ax.set_ylim(min(ys) - 0.03, max(ys) + 0.03)
        ax.set_xticks([])
        ax.tick_params(colors=TEXT, labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        last_move = round(ys[-1] - ys[0], 3)
        ax.text(0.98, 0.92, f"{last_move:+.2f}", transform=ax.transAxes,
                ha="right", color=color, fontsize=8, family="monospace")

    for ax in axes_flat[n:]:
        ax.axis("off")

    fig.suptitle(f"LINE MOVEMENT TRACKER | {match.get('away_team','')} @ "
                 f"{match.get('home_team','')} (scrape window)",
                 color=CYAN, fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))

    path = os.path.join(out_dir, f"line_movement_{_safe_id(match['id'])}.png")
    fig.savefig(path, dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_reports(feed: Dict[str, Any], out_dir: str = REPORTS_DIR_DEFAULT,
                     max_matches: int = 6) -> Dict[str, List[str]]:
    """Render charts for the highest-signal matches. Returns per-chart file lists.

    Only matches with at least one positive-EV market are rendered; everything
    else stays text/table-only in the app (no fabricated imagery).
    """
    os.makedirs(out_dir, exist_ok=True)
    result: Dict[str, List[str]] = {"market_depth": [], "edge_radar": [],
                                    "line_movement": []}
    matches = feed.get("matches", [])

    def score(m: Dict[str, Any]) -> float:
        return max((mk.get("ev_percent", -999) for mk in m.get("markets", [])),
                   default=-999)

    ranked = sorted(matches, key=score, reverse=True)
    chosen = [m for m in ranked if score(m) > 0][:max_matches]

    for m in chosen:
        try:
            f = chart_market_depth(m, out_dir)
            if f:
                result["market_depth"].append(f)
        except Exception:
            pass
        try:
            f = chart_edge_radar(m, out_dir)
            if f:
                result["edge_radar"].append(f)
        except Exception:
            pass
        try:
            f = chart_line_movement(m, out_dir)
            if f:
                result["line_movement"].append(f)
        except Exception:
            pass

    return result
