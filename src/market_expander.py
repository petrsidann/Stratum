"""Stratum Market Expander (Phase 5) — the Depth Charge.

Moves the board beyond ML/Spread/Total: systematically fetches and prices
Player Props (points, rebounds, assists, yards), Team Totals, Halves/Quarters
and Alternate Lines — the ~200 markets per game that big books price lazily
and low-volume books price *softly*. That is where a small account finds edge.

Honesty contract (unchanged from Phases 1-4):
  * ``expand_to_props`` returns [] when a source has no props. It NEVER
    invents players, lines or prices. Only names literally present in the
    fetched feed become rows.
  * Live fetch goes through an injectable ``fetcher(query) -> str|dict`` so
    tests (and offline runs) can feed deterministic fixtures; production
    wires it to ``src.scraper``.
  * Prop expansion is heavy, so every match_id result is cached for
    PROP_CACHE_TTL_SECONDS (5 min). Repeated scans hit memory instantly.

``price_derivatives`` implements framework Section 2.3: theoretical 1H/1Q
lines derived from the full-game total and historical tempo splits, compared
against actual retail derivative lines to surface mispriced quarters/halves.
All probability math stays in src.quant_engine.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("stratum.expander")

PROP_CACHE_TTL_SECONDS = 300.0   # 5 minutes, per spec

# Stat types recognized on the prop board (framework's granular taxonomy).
STAT_TYPES = ("pts", "reb", "ast", "yards", "pass_yds", "rec_yds", "td", "threes", "dbl_dbl")

# --- Framework Section 2.3 tempo splits -------------------------------------
# Share of the full-game total expected in each segment, by sport, derived
# from historical pace data (NBA scoring is back-loaded vs NFL which front-
# loads relative to halftime because of the OT-free clock structure).
TEMPO_SPLITS = {
    "NBA":  {"1h": 0.495, "1q": 0.245, "team_total": 0.50},
    "NFL":  {"1h": 0.455, "1q": 0.230, "team_total": 0.50},
    "default": {"1h": 0.48, "1q": 0.25, "team_total": 0.50},
}

# Tempo rating modulation: a +1.0 pace rating (vs league avg) adds ~1% points.
TEMPO_RATING_COEFF = 0.01

# A derivative fires as "mispriced" when retail deviates this many points
# from the tempo-theoretical line (halves tolerance tighter than quarters).
DERIVATIVE_MISPRICE_PTS = {"1h": 2.5, "1q": 1.5}


def _clean(text: str) -> str:
    """Strip markdown artifacts and emojis from any inbound/outbound prose."""
    text = re.sub(r"[*_`#]", "", text or "")
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class MarketExpander:
    """Deep-board fetcher with a TTL cache and derivative pricer."""

    def __init__(self, fetcher: Optional[Callable[[str], Any]] = None,
                 cache_ttl: float = PROP_CACHE_TTL_SECONDS):
        """``fetcher(query)`` returns either raw text or a nested JSON dict
        from a deep endpoint (scraped or mocked). Defaults to src.scraper."""
        self._fetcher = fetcher
        self._cache_ttl = float(cache_ttl)
        self._cache: Dict[str, tuple] = {}   # match_id -> (monotonic_ts, rows)
        self.cache_hits = 0
        self.cache_misses = 0

    # -- public API ----------------------------------------------------------

    def expand_to_props(self, match_id: str, sport: str = "NBA") -> List[Dict]:
        """Return granular prop rows for one match.

        Row schema (per spec):
            {player_name, stat_type, line_value, odds_over, odds_under,
             bookmaker, market_type='PlayerProps', match_id, data_source}

        ``odds_home``/``odds_away`` are kept as aliases of over/under for
        backward-compatible callers. Empty list means the source genuinely
        had no props — never a fabricated fallback roster.
        """
        if not match_id or not str(match_id).strip():
            return []
        key = f"{str(match_id).strip().lower()}|{sport}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and (now - cached[0]) < self._cache_ttl:
            self.cache_hits += 1
            return [dict(r) for r in cached[1]]
        self.cache_misses += 1

        try:
            payload = self._fetch_raw(match_id, sport)
        except Exception as exc:  # network/scrape failure -> graceful empty
            logger.warning("Prop expansion fetch failed for %s: %s", match_id, exc)
            payload = None

        rows = self._parse_payload(payload, match_id) if payload else []
        # Cache even the EMPTY answer — hammering a dead endpoint every scan
        # would be rude and slow; a negative result is still a result.
        self._cache[key] = (now, rows)
        return [dict(r) for r in rows]

    def price_derivatives(
        self,
        full_game_line: float,
        team_tempo_rating: float,
        retail_1h_line: Optional[float] = None,
        retail_1q_line: Optional[float] = None,
        sport: str = "NBA",
    ) -> dict:
        """Theoretical 1H/1Q totals from tempo splits vs actual retail lines.

        Framework Section 2.3: segment_total ≈ full_game_line * split *
        (1 + TEMPO_RATING_COEFF * tempo_rating), where tempo_rating is points
        per team above/below league pace. Rounding snaps to the .5 grid books
        actually post. When a retail line is supplied we compute the deviation
        and flag it as a candidate mispricing past the per-segment tolerance.
        Missing retail lines yield deviation=None — unknown, never guessed.
        """
        if full_game_line is None or float(full_game_line) <= 0:
            raise ValueError("full_game_line must be a positive total")
        splits = TEMPO_SPLITS.get(sport, TEMPO_SPLITS["default"])
        tempo_mult = 1.0 + TEMPO_RATING_COEFF * float(team_tempo_rating or 0.0)

        def theoretical(frac: float) -> float:
            raw = float(full_game_line) * frac * tempo_mult
            return round(raw * 2.0 - 0.5) / 2.0  # snap down-ish to nearest .5 grid

        def compare(theory: float, retail: Optional[float], seg: str) -> dict:
            out = {"theoretical": theory, "retail": retail, "deviation": None,
                   "mispriced": False}
            if retail is not None:
                dev = round(float(retail) - theory, 2)
                out["deviation"] = dev
                tol = DERIVATIVE_MISPRICE_PTS[seg]
                out["mispriced"] = abs(dev) >= tol
            return out

        th_1h = theoretical(splits["1h"])
        th_1q = theoretical(splits["1q"])
        tt = round(float(full_game_line) * splits["team_total"] * tempo_mult * 2.0 - 0.5) / 2.0
        return {
            "full_game_line": float(full_game_line),
            "sport": sport,
            "tempo_rating": float(team_tempo_rating or 0.0),
            "first_half": compare(th_1h, retail_1h_line, "1h"),
            "first_quarter": compare(th_1q, retail_1q_line, "1q"),
            "team_total": {"theoretical": tt},
        }

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- fetch + parse ---------------------------------------------------------

    def _fetch_raw(self, match_id: str, sport: str) -> Any:
        if self._fetcher is not None:
            return self._fetcher(f"{match_id} {sport} props")
        try:
            from src import scraper
            return scraper.fetch_match_context(f"{match_id} {sport} player props alt lines")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Scraper unavailable for props: %s", exc)
            return None

    # Nested-JSON shape accepted from deep endpoints:
    # {"props": [{"player": "...", "stat": "pts", "line": 30.5,
    #              "over": -115, "under": -105, "book": "FanDuel"}, ...]}
    _NESTED_KEYS = ("props", "player_props", "data", "markets")

    # Flat text line format: "Luka Doncic pts O/U 30.5 @ FanDuel: -115 / -105"
    _TEXT_RE = re.compile(
        r"(?P<player>[A-Za-z][A-Za-z .'\-]{2,40}?)\s+"
        r"(?P<stat>pts|reb|ast|yards?|pass[_ ]?yds|rec[_ ]?yds|td|threes?|dbl[_ ]?dbl)\s+"
        r"(?:O/?U|over[/ ]under)?\s*(?P<line>\d+(?:\.\d+)?)\s*"
        r"(?:@|at)\s*(?P<book>[A-Za-z][A-Za-z .]{1,20}?)\s*:?\s*"
        r"(?P<over>[+-]\d{2,4})\s*/\s*(?P<under>[+-]\d{2,4})",
        re.IGNORECASE,
    )

    def _parse_payload(self, payload: Any, match_id: str) -> List[Dict]:
        """Accept nested JSON (dict/list/str) OR flat text; never hallucinate."""
        rows: List[Dict] = []
        if isinstance(payload, str):
            stripped = payload.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    payload = json.loads(stripped)
                except ValueError:
                    pass  # fall through to regex text parsing
        if isinstance(payload, (dict, list)):
            rows.extend(self._parse_nested(payload, match_id))
        if isinstance(payload, str):
            rows.extend(self._parse_text(payload, match_id))
        return rows

    def _parse_nested(self, node: Any, match_id: str, depth: int = 0) -> List[Dict]:
        """Walk arbitrary nesting to find prop leaf objects. Bounded depth."""
        out: List[Dict] = []
        if depth > 6:
            return out
        if isinstance(node, list):
            for item in node:
                out.extend(self._parse_nested(item, match_id, depth + 1))
            return out
        if not isinstance(node, dict):
            return out
        # Is THIS dict a prop row? Require a real player name AND a line.
        player = (node.get("player") or node.get("player_name") or node.get("name"))
        line = node.get("line") if node.get("line") is not None else node.get("line_value")
        stat = (node.get("stat") or node.get("stat_type") or "").lower().replace(" ", "_")
        if player and line is not None and stat:
            norm = self._normalize_stat(stat)
            try:
                line_val = float(line)
            except (TypeError, ValueError):
                return out
            over = node.get("over", node.get("odds_over", node.get("odds_home")))
            under = node.get("under", node.get("odds_under", node.get("odds_away")))
            if over is None and under is None:
                return out  # a line without a price is not an opportunity
            out.append({
                "player_name": _clean(str(player)),
                "stat_type": norm,
                "line_value": line_val,
                "odds_over": int(over) if over is not None else None,
                "odds_under": int(under) if under is not None else None,
                "odds_home": int(over) if over is not None else None,   # alias
                "odds_away": int(under) if under is not None else None,  # alias
                "bookmaker": _clean(str(node.get("book") or node.get("bookmaker") or "unknown")),
                "market_type": "PlayerProps",
                "selection": f"{_clean(str(player))} {norm} Over {line_val}",
                "match_id": match_id,
                "data_source": "live",
            })
            return out
        for key in self._NESTED_KEYS:
            if key in node:
                out.extend(self._parse_nested(node[key], match_id, depth + 1))
        # Also walk unknown dict/list children defensively.
        if not out:
            for val in node.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._parse_nested(val, match_id, depth + 1))
        return out

    def _parse_text(self, text: str, match_id: str) -> List[Dict]:
        out: List[Dict] = []
        for m in self._TEXT_RE.finditer(text or ""):
            stat = self._normalize_stat(m.group("stat").lower().replace(" ", "_"))
            player = _clean(m.group("player"))
            line_val = float(m.group("line"))
            over, under = int(m.group("over")), int(m.group("under"))
            out.append({
                "player_name": player,
                "stat_type": stat,
                "line_value": line_val,
                "odds_over": over,
                "odds_under": under,
                "odds_home": over,
                "odds_away": under,
                "bookmaker": _clean(m.group("book")),
                "market_type": "PlayerProps",
                "selection": f"{player} {stat} Over {line_val}",
                "match_id": match_id,
                "data_source": "live",
            })
        return out

    @staticmethod
    def _normalize_stat(raw: str) -> str:
        raw = raw.replace(" ", "_")
        if raw in ("yard", "yds", "rush_yds"):
            return "yards"
        if raw in ("3pm", "three", "threes"):
            return "threes"
        if raw in ("double_double", "dbl dbl"):
            return "dbl_dbl"
        return raw
