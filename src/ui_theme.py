"""Stratum Design System — the single source of truth for every pixel.

Rules enforced here (and nowhere else):
  * Palette: Deep Charcoal #0E1116 canvas, #161B22 surfaces, Electric Mint
    #2EE6A6 for positive data, Soft Red #FF4D4D for negative, Slate
    #8B9BB4 for secondary text, #30363D hairline borders.
  * Typography: sans-serif (Inter/Helvetica) for labels and body; monospace
    ('Roboto Mono'/'SF Mono') for EVERY number so columns align optically.
  * Zero emojis anywhere. Indicators use clean geometric glyphs only:
    ▲ ▼ ● ○ ◆ —.
  * Mobile-first: every grid collapses to a single column below 768px.

Public API:
  inject_css()                 -> str  (full <style> block for st.markdown)
  render_header(title, sub)             branded top bar
  render_metric_card(...)               reusable KPI card (EV / odds / profit)
  bottom_nav_html(active, keys)         fixed native-style tab bar
  status_dot(running)                   pulsing live-status indicator
  money() / signed() / fmt_odds()       monospace-safe number formatters
"""

from __future__ import annotations

import html as _html

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#0E1116",            # Deep Charcoal — app canvas
    "surface": "#161B22",       # Card / panel surface
    "surface_alt": "#1C2330",   # Raised surface (inputs, chips)
    "text_primary": "#FFFFFF",
    "text_secondary": "#8B9BB4",  # Slate Grey
    "positive": "#2EE6A6",      # Electric Green/Mint
    "negative": "#FF4D4D",      # Alert Red
    "warning": "#F5A524",       # Amber — stale/sample warnings
    "border": "#30363D",
    "header_bg": "#0B0E13",
    "nav_bg": "#11161D",
}

FONTS = {
    "sans": "'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif",
    "mono": "'Roboto Mono', 'SF Mono', 'JetBrains Mono', 'Courier New', monospace",
}

SPACING = {
    "xs": "4px",
    "sm": "8px",
    "md": "16px",
    "lg": "24px",
    "xl": "32px",
    "radius_card": "12px",
    "radius_pill": "999px",
    "mobile_breakpoint": "768px",
}

# Light theme overrides (Settings can switch; Dark is the factory default).
LIGHT_COLORS = {
    "bg": "#F4F6F9",
    "surface": "#FFFFFF",
    "surface_alt": "#EDF0F4",
    "text_primary": "#0E1116",
    "text_secondary": "#5B6B84",
    "positive": "#0BA97A",
    "negative": "#D93636",
    "warning": "#B5760A",
    "border": "#D4DAE3",
    "header_bg": "#FFFFFF",
    "nav_bg": "#FFFFFF",
}

GOOGLE_FONTS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Inter:wght@400;500;600;700;800"
    "&family=Roboto+Mono:wght@400;500;700&display=swap"
)


def palette(theme: str = "dark") -> dict:
    """Return the token map for the requested theme."""
    if theme == "light":
        merged = dict(COLORS)
        merged.update(LIGHT_COLORS)
        return merged
    return dict(COLORS)


def esc(text) -> str:
    """HTML-escape any dynamic string before injection into markup."""
    return _html.escape(str(text), quote=True)


# ---------------------------------------------------------------------------
# Formatters — numbers are ALWAYS rendered through these (monospace discipline)
# ---------------------------------------------------------------------------

def money(value, decimals: int = 2, sign: bool = False) -> str:
    """$1,234.56 / +$12.00 / -$80.00. None -> em dash, never a fake zero."""
    if value is None:
        return "\u2014"
    v = float(value)
    body = f"${abs(v):,.{decimals}f}"
    if v < 0:
        return f"-\u200a{body}"
    return f"+\u200a{body}" if (sign and v > 0) else body


def signed(value, decimals: int = 2, suffix: str = "") -> str:
    """Always-signed numeric string for deltas/percentages."""
    if value is None:
        return "\u2014"
    v = float(value)
    s = f"{v:+,.{decimals}f}{suffix}"
    return s


def fmt_odds(american) -> str:
    """American odds display: +130 / -150 / EVEN. None -> unknown glyph."""
    if american is None:
        return "\u2014"
    v = int(round(float(american)))
    return "+{0}".format(v) if v >= 0 else str(v)


def pct(value, decimals: int = 1, sign: bool = True) -> str:
    if value is None:
        return "\u2014"
    v = float(value)
    return f"{v:+.{decimals}f}%" if sign else f"{v:.{decimals}f}%"


def trend_glyph(trend: str) -> str:
    """Single geometric glyph per trend direction. NO emojis, ever."""
    mapping = {"up": "\u25B2", "down": "\u25BC", "flat": "\u25C6",
               "neutral": "\u25C6", "on": "\u25CF", "off": "\u25CB"}
    return mapping.get(str(trend or "").lower(), "\u25C6")


def trend_color(trend: str) -> str:
    t = str(trend or "").lower()
    if t in ("up", "positive", "beat"):
        return COLORS["positive"]
    if t in ("down", "negative", "miss"):
        return COLORS["negative"]
    return COLORS["text_secondary"]


# ---------------------------------------------------------------------------
# CSS injection
# ---------------------------------------------------------------------------

_CSS_TEMPLATE = """
/* ================= Stratum design system ================= */
@import url('{fonts_url}');

html, body, .stApp {{
  background-color: {bg} !important;
  color: {text_primary};
  font-family: {sans};
  -webkit-font-smoothing: antialiased;
}}
#MainMenu {{ visibility: hidden; }}
footer {{ visibility: hidden; }}

/* Native app shell: kill the default chrome, own the top/bottom ourselves. */
[data-testid="stHeader"] {{
  background: {header_bg} !important;
  height: 0 !important;
  min-height: 0 !important;
  padding: 0 !important;
}}
header[data-testid="stHeader"] .main-header {{ display: none !important; }}
[data-testid="stAppViewContainer"] {{ padding-top: 0; }}
[data-testid="stAppViewBlockContainer"],
[data-testid="stMainBlockContainer"] {{
  padding-top: 0.5rem !important;
  padding-left: 1rem !important;
  padding-right: 1rem !important;
  padding-bottom: 110px !important;   /* clearance for the bottom nav */
  max-width: 1180px !important;
  margin: 0 auto !important;
}}

/* ---------- Brand header ---------- */
.stratum-topbar {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 14px 4px 10px 4px;
  border-bottom: 1px solid {border}; margin-bottom: 14px;
}}
.stratum-brand {{ display: flex; align-items: center; gap: 10px; }}
.stratum-logo {{
  width: 34px; height: 34px; border-radius: 8px; flex: 0 0 auto;
  background: linear-gradient(135deg, {positive} 0%, #17A97C 100%);
  display: flex; align-items: center; justify-content: center;
  color: {bg}; font-weight: 800; font-size: 17px;
  font-family: {sans}; letter-spacing: -1px;
}}
.stratum-title {{
  font-size: 19px; font-weight: 800; letter-spacing: 3px;
  color: {text_primary}; line-height: 1.1; text-transform: uppercase;
}}
.stratum-subtitle {{
  font-size: 11px; color: {text_secondary}; letter-spacing: 1.5px;
  text-transform: uppercase; margin-top: 2px; font-weight: 600;
}}
.stratum-live {{ display: flex; align-items: center; gap: 8px;
  font-size: 11px; letter-spacing: 1.5px; font-weight: 700;
  color: {text_secondary}; text-transform: uppercase; }}
.stratum-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
.stratum-dot-on {{ background: {positive}; animation: stratum-pulse 1.6s ease-in-out infinite; }}
.stratum-dot-off {{ background: {text_secondary}; opacity: 0.55; }}
@keyframes stratum-pulse {{
  0%   {{ box-shadow: 0 0 0 0 rgba(46, 230, 166, 0.55); }}
  70%  {{ box-shadow: 0 0 0 9px rgba(46, 230, 166, 0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(46, 230, 166, 0); }}
}}

/* ---------- Metric cards ---------- */
.stratum-card {{
  background: {surface}; border: 1px solid {border};
  border-radius: {radius_card}; padding: 16px 16px 14px 16px;
  margin: 0; height: 100%;
  display: flex; flex-direction: column; gap: 6px;
  -webkit-tap-highlight-color: transparent;
}}
.stratum-card:hover {{ border-color: {text_secondary}; }}
.stratum-card-label {{
  font-size: 10.5px; font-weight: 700; letter-spacing: 1.6px;
  text-transform: uppercase; color: {text_secondary};
}}
.stratum-card-value {{
  font-family: {mono}; font-size: 26px; font-weight: 700;
  color: {text_primary}; line-height: 1.15; font-variant-numeric: tabular-nums;
}}
.stratum-card-delta {{
  font-family: {mono}; font-size: 13px; font-weight: 600;
  display: flex; align-items: center; gap: 6px;
}}
.stratum-card-meta {{ font-size: 11px; color: {text_secondary}; }}

/* ---------- Opportunity cards ---------- */
.stratum-opp {{
  background: {surface}; border: 1px solid {border};
  border-radius: {radius_card}; padding: 16px; margin-bottom: 12px;
}}
.stratum-opp-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }}
.stratum-opp-game {{ font-size: 15px; font-weight: 700; color: {text_primary}; }}
.stratum-opp-market {{
  font-size: 10px; font-weight: 700; letter-spacing: 1.4px; text-transform: uppercase;
  color: {positive}; border: 1px solid {positive}; border-radius: 999px;
  padding: 2px 8px; white-space: nowrap;
}}
.stratum-opp-grid {{
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 12px 0;
}}
.stratum-opp-cell-k {{ font-size: 10px; letter-spacing: 1.2px; text-transform: uppercase;
  color: {text_secondary}; font-weight: 700; }}
.stratum-opp-cell-v {{ font-family: {mono}; font-size: 16px; font-weight: 700;
  color: {text_primary}; font-variant-numeric: tabular-nums; }}
.stratum-badge {{
  display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: 1px;
  padding: 2px 8px; border-radius: 4px; text-transform: uppercase; margin-right: 6px;
}}
.stratum-badge-hot {{ color: {bg}; background: {positive}; }}
.stratum-badge-stale {{ color: {bg}; background: {warning}; }}
.stratum-badge-sample {{ color: {text_primary}; background: #3A4353; }}

/* ---------- Filter chips ---------- */
.stratum-chips {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 14px 0; }}
.stButton > button.chip {{
  border-radius: {radius_pill}; padding: 6px 16px; font-size: 12px;
  font-weight: 700; letter-spacing: 0.8px; text-transform: uppercase;
  border: 1px solid {border}; background: transparent; color: {text_secondary};
  min-height: 34px; transition: all 120ms ease;
}}
.stButton > button.chip-active {{
  background: {positive}; color: {bg}; border-color: {positive};
}}

/* ---------- Buttons as CTAs ---------- */
/* Touch-target discipline: every interactive control is >= 44px tall
   (Apple HIG / WCAG 2.5.5) so the terminal is thumb-safe on phones. */
.stButton > button, .stFormSubmitButton > button,
.stDownloadButton > button, .stRadio label,
[data-testid="stBaseButton"], [data-baseweb="tab"] {{
  min-height: 44px;
}}
.stButton > button, .stFormSubmitButton > button {{
  font-family: {sans}; font-weight: 700; letter-spacing: 0.6px;
  border-radius: {radius_pill}; padding: 10px 22px; min-height: 44px;
  border: 1px solid {border}; background: {surface_alt}; color: {text_primary};
  transition: filter 120ms ease;
}}
.stButton > button:hover, .stFormSubmitButton > button:hover {{
  border-color: {text_secondary}; filter: brightness(1.15);
}}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {{
  background: {positive}; color: {bg}; border: 1px solid {positive};
}}
.stFormSubmitButton > button {{ width: 100%; }}

/* ---------- Inputs ---------- */
.stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] > div {{
  background: {surface_alt} !important; border: 1px solid {border} !important;
  border-radius: 8px !important; color: {text_primary} !important;
  font-family: {mono}; min-height: 44px;
}}
.stCaption, small {{ color: {text_secondary}; }}

/* ---------- DataFrames: zebra striping + monospace numerals ---------- */
[data-testid="stDataFrame"] {{ border: 1px solid {border}; border-radius: {radius_card}; overflow: hidden; }}
.glRkSM td, [data-testid="stDataTable"] tbody tr:nth-child(even) {{ background: {surface}; }}
[data-testid="stDataTable"] tbody tr:nth-child(odd) {{ background: {header_bg}; }}
[data-testid="stDataTable"] th, [data-testid="stDataTable"] td {{
  font-family: {mono}; font-variant-numeric: tabular-nums; font-size: 12.5px;
  border-color: {border} !important; color: {text_primary};
}}

/* ---------- Mobile-safe tables ---------- */
/* Any raw <table> (and Streamlit's dataframe/grid root) is wrapped in a
   horizontal-scroll container so wide ledgers never break the phone layout
   — content scrolls sideways instead of blowing out the viewport. */
.stratum-table-wrap {{
  overflow-x: auto; -webkit-overflow-scrolling: touch;
  max-width: 100%; border: 1px solid {border}; border-radius: {radius_card};
}}
.stratum-table-wrap table {{ border-collapse: collapse; width: 100%; min-width: 640px; }}
.stratum-table-wrap th, .stratum-table-wrap td {{
  font-family: {mono}; font-size: 12.5px; padding: 8px 10px; text-align: left;
  border-bottom: 1px solid {border}; color: {text_primary}; white-space: nowrap;
}}
.stratum-table-wrap th {{ color: {text_secondary}; text-transform: uppercase;
  letter-spacing: 1px; font-size: 10.5px; }}
div[data-testid="stDataFrame"], div[data-testid="stTable"] {{
  max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch;
}}

/* ---------- Graceful-degradation banner (missing API keys) ---------- */
.stratum-banner {{
  background: rgba(245, 165, 36, 0.10); border: 1px solid {warning};
  border-left: 4px solid {warning}; border-radius: 10px;
  padding: 14px 16px; margin: 0 0 14px 0;
}}
.stratum-banner-title {{
  font-weight: 800; font-size: 12.5px; letter-spacing: 1.4px;
  text-transform: uppercase; color: {warning};
}}
.stratum-banner-body {{ font-size: 13px; color: {text_secondary}; margin-top: 4px; line-height: 1.5; }}

/* ---------- Tabs / expander / alerts ---------- */
.stTabs [data-baseweb="tab-list"] {{ gap: 2px; background: {surface}; border-radius: 10px; padding: 4px; border: 1px solid {border}; }}
.stTabs [data-baseweb="tab"] {{
  border-radius: 8px; font-weight: 700; font-size: 12px; letter-spacing: 1px;
  text-transform: uppercase; color: {text_secondary}; background: transparent;
}}
.stTabs [aria-selected="true"] {{ background: {positive} !important; color: {bg} !important; }}
[data-testid="stAlert"] {{ border-radius: 10px; border: 1px solid {border}; background: {surface}; }}
[data-testid="stExpander"] details {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}

/* ---------- Bottom navigation (native tab bar) ---------- */
.stratum-nav {{
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 999;
  display: flex; justify-content: space-around; align-items: stretch;
  background: {nav_bg}; border-top: 1px solid {border};
  padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px)) 8px;
  max-width: 100%;
}}
.stratum-nav-inner {{ display: flex; justify-content: space-around; width: 100%; max-width: 640px; margin: 0 auto; }}
.stratum-nav-btn {{
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 3px; padding: 8px 4px; min-height: 56px; cursor: pointer;
  color: {text_secondary}; text-decoration: none; user-select: none;
  -webkit-tap-highlight-color: transparent; border-radius: 10px;
}}
.stratum-nav-btn svg {{ width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.8; }}
.stratum-nav-label {{ font-size: 10px; font-weight: 800; letter-spacing: 1.4px; text-transform: uppercase; }}
.stratum-nav-btn.active {{ color: {positive}; }}
.stratum-nav-btn.active .stratum-nav-dotind {{
  width: 5px; height: 5px; border-radius: 50%; background: {positive};
}}
.stratum-nav-dotind {{ width: 5px; height: 5px; border-radius: 50%; background: transparent; }}

/* ---------- Section headers ---------- */
.stratum-section {{
  font-size: 12px; font-weight: 800; letter-spacing: 2px; text-transform: uppercase;
  color: {text_secondary}; margin: 18px 0 8px 0;
}}

/* ---------- Mobile first ---------- */
@media (max-width: 768px) {{
  [data-testid="stAppViewBlockContainer"],
  [data-testid="stMainBlockContainer"] {{ padding-left: 10px !important; padding-right: 10px !important; }}
  .stratum-opp-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .stratum-title {{ font-size: 16px; letter-spacing: 2px; }}
  .stratum-card-value {{ font-size: 22px; }}
  div[data-testid="stHorizontalBlock"] {{ flex-wrap: wrap; gap-bottom: 8px; }}
  div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
  div[data-testid="stHorizontalBlock"] > div[data-testid="stLayoutWrapper"] {{
    flex: 1 1 100% !important; min-width: 100% !important;
  }}
}}
@media (min-width: 769px) {{
  div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
  div[data-testid="stHorizontalBlock"] > div[data-testid="stLayoutWrapper"] {{
    flex: 1 1 0 !important;
  }}
}}
"""


def inject_css(theme: str = "dark") -> str:
    """Return the complete ``<style>`` payload to be passed to st.markdown.

    Pure function: returns a string, touches no Streamlit runtime state, so it
    is trivially unit-testable.
    """
    pal = palette(theme)
    css = _CSS_TEMPLATE.format(
        fonts_url=GOOGLE_FONTS_URL,
        bg=pal["bg"], surface=pal["surface"], surface_alt=pal["surface_alt"],
        text_primary=pal["text_primary"], text_secondary=pal["text_secondary"],
        positive=pal["positive"], negative=pal["negative"], warning=pal["warning"],
        border=pal["border"], header_bg=pal["header_bg"], nav_bg=pal["nav_bg"],
        sans=FONTS["sans"], mono=FONTS["mono"],
        radius_card=SPACING["radius_card"], radius_pill=SPACING["radius_pill"],
    )
    return "<style>" + css + "</style>"


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def header_html(title: str, subtitle: str = None, sentinel_running: bool = False) -> str:
    """Pure HTML builder for the branded top bar (testable without Streamlit)."""
    sub = (
        f'<div class="stratum-subtitle">{esc(subtitle)}</div>'
        if subtitle else ""
    )
    dot_cls = "stratum-dot-on" if sentinel_running else "stratum-dot-off"
    live_txt = "SENTINEL LIVE" if sentinel_running else "SENTINEL OFFLINE"
    return (
        '<div class="stratum-topbar">'
        '<div class="stratum-brand">'
        '<div class="stratum-logo">S</div>'
        '<div><div class="stratum-title">' + esc(title) + "</div>" + sub + "</div>"
        "</div>"
        '<div class="stratum-live">'
        f'<span class="stratum-dot {dot_cls}"></span>{live_txt}'
        "</div>"
        "</div>"
    )


def render_header(title: str, subtitle: str = None, sentinel_running: bool = False) -> None:
    """Inject the branded header into the page."""
    import streamlit as st

    st.markdown(
        header_html(title, subtitle, sentinel_running), unsafe_allow_html=True
    )


def metric_card_html(label: str, value: str, delta=None, trend: str = "flat",
                     meta: str = None) -> str:
    """Pure HTML for one KPI card. Conditional coloring by trend/delta sign."""
    t = str(trend or "").lower()
    if t not in ("up", "down", "flat"):
        # Infer from delta sign when an explicit trend was not supplied.
        t = "flat"
    if t == "flat" and isinstance(delta, (int, float)):
        t = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    color = trend_color(t)
    glyph = trend_glyph(t)
    delta_str = ""
    if delta is not None:
        try:
            delta_str = signed(float(delta))
        except (TypeError, ValueError):
            delta_str = esc(delta)
    meta_html = (
        f'<div class="stratum-card-meta">{esc(meta)}</div>' if meta else ""
    )
    return (
        '<div class="stratum-card">'
        f'<div class="stratum-card-label">{esc(label)}</div>'
        f'<div class="stratum-card-value">{esc(value)}</div>'
        + (
            f'<div class="stratum-card-delta" style="color:{color}">'
            f"<span>{glyph}</span><span>{delta_str}</span></div>"
            if delta_str else ""
        )
        + meta_html
        + "</div>"
    )


def render_metric_card(label: str, value: str, delta: float = None,
                       trend: str = "flat", meta: str = None) -> None:
    """Reusable KPI card: green ▲ for positive, red ▼ for negative, slate ◆ flat."""
    import streamlit as st

    st.markdown(
        metric_card_html(label=label, value=value, delta=delta, trend=trend, meta=meta),
        unsafe_allow_html=True,
    )


def section_label(text: str) -> None:
    import streamlit as st

    st.markdown(f'<div class="stratum-section">{esc(text)}</div>', unsafe_allow_html=True)


def table_html(rows: list, headers: list) -> str:
    """Mobile-safe HTML table wrapped in a horizontal-scroll container.

    Every column stays thumb-readable on phones: the wrapper scrolls
    sideways (overflow-x: auto) instead of letting wide tables break the
    page layout. All cell values are HTML-escaped — no raw injection.
    Pure function: returns a string, safe to unit-test without Streamlit.
    """
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{esc('' if c is None else c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        '<div class="stratum-table-wrap"><table>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


# Inline SVG icon set for the bottom nav — strokes inherit currentColor.
NAV_ICONS = {
    "SCAN": (
        '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/>'
        '<line x1="16.5" y1="16.5" x2="21" y2="21"/></svg>'
    ),
    "AUDIT": (
        '<svg viewBox="0 0 24 24"><path d="M6 3h12l2 4v12a2 2 0 0 1-2 2H6a2 2 0 '
        '0 1-2-2V7z"/><line x1="8" y1="11" x2="16" y2="11"/>'
        '<line x1="8" y1="15" x2="13" y2="15"/></svg>'
    ),
    "PORTFOLIO": (
        '<svg viewBox="0 0 24 24"><rect x="3" y="12" width="4" height="8" rx="1"/>'
        '<rect x="10" y="7" width="4" height="13" rx="1"/>'
        '<rect x="17" y="3" width="4" height="17" rx="1"/></svg>'
    ),
    "ALERTS": (
        '<svg viewBox="0 0 24 24"><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/>'
        '<path d="M10 19a2 2 0 0 0 4 0"/></svg>'
    ),
    "SETTINGS": (
        '<svg viewBox="0 0 24 24"><line x1="4" y1="7" x2="20" y2="7"/>'
        '<line x1="4" y1="17" x2="20" y2="17"/>'
        '<circle cx="9" cy="7" r="2.4"/><circle cx="15" cy="17" r="2.4"/></svg>'
    ),
}

DEFAULT_NAV_ORDER = ["SCAN", "AUDIT", "PORTFOLIO", "ALERTS", "SETTINGS"]


def bottom_nav_html(active: str, views: list = None) -> str:
    """Fixed bottom tab bar. Each tab posts ?view=KEY back to the same page,
    which main.py reads on rerun — works even inside an iframe where JS
    injection is blocked (Streamlit's CSP)."""
    views = views or DEFAULT_NAV_ORDER
    cells = []
    for key in views:
        cls = "stratum-nav-btn active" if key.lower() == str(active).lower() else "stratum-nav-btn"
        icon = NAV_ICONS.get(key.upper(), "")
        cells.append(
            f'<a class="{cls}" href="?view={esc(key.lower())}" target="_self">'
            f'{icon}<span class="stratum-nav-label">{esc(key)}</span>'
            '<span class="stratum-nav-dotind"></span></a>'
        )
    return (
        '<div class="stratum-nav"><div class="stratum-nav-inner">'
        + "".join(cells)
        + "</div></div>"
    )


def render_bottom_nav(active: str, views: list = None) -> None:
    import streamlit as st

    st.markdown(bottom_nav_html(active, views), unsafe_allow_html=True)


def status_dot(running: bool) -> str:
    """Inline pulsing/solid dot HTML for arbitrary placement."""
    cls = "stratum-dot stratum-dot-on" if running else "stratum-dot stratum-dot-off"
    return f'<span class="{cls}"></span>'
