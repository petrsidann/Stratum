const OWNER = "petrsidann";
const REPO = "Stratum";
const API = `https://api.github.com/repos/${OWNER}/${REPO}`;
const RAW_LATEST = `https://raw.githubusercontent.com/${OWNER}/${REPO}/main/data/hunts/latest.json`;

export type HuntLine = { agent: string; text: string; ts: number };
export type Edge = { market: string; selection: string; book_odds: number; fair_odds: number; ev_percent: number; confidence_score: number; kelly_stake_pct: number; reasoning_summary: string };
export type TopEdge = Edge;
export type Fixture = { fixture_id: string; home: string; away: string; sport: string; league: string; kickoff_utc: string; markets_scanned: number; top_edges: Edge[]; diagrams?: string[]; visual_reports_png?: string[]; context_note?: string; agent_trace_lines?: HuntLine[] };
export type HuntResult = { status?: string; fixtures: Fixture[]; sources_status?: Record<string, string>; available_today?: string[]; error_message?: string };
export type HuntSnapshot = { hunt_id: string; status: string; stage: string; progress: number; lines: HuntLine[]; result: HuntResult | null };
export type MarketUniverse = { generated_at_utc: string; sources_status: Record<string, string>; fixture_count: number; fixtures: Fixture[] };
export const AGENT_COLORS: Record<string, string> = {
  SCOUT: "#38BDF8", ACTUARY: "#2EE6A6", CONTEXT: "#FACC15", STRATEGIST: "#00E5FF", SYSTEM: "#94A3B8", SWARM: "#C084FC",
};

export function getToken(): string { return localStorage.getItem("stratum_pat") || ""; }
export function setToken(t: string): void { localStorage.setItem("stratum_pat", t.trim()); }

export async function dispatchHunt(query: string, sport: string, huntId: string): Promise<void> {
  const tok = getToken();
  if (!tok) throw new Error("NO_TOKEN: open Settings below and paste your trigger token once.");
  const r = await fetch(`${API}/actions/workflows/hunt-on-demand.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${tok}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: { match_query: query, sport, hunt_id: huntId } }),
  });
  if (!r.ok && r.status !== 204) throw new Error(`Dispatch failed HTTP ${r.status} (check token scope: Actions read+write on Stratum)`);
}

export async function pollHunt(huntId: string): Promise<HuntSnapshot | null> {
  try {
    const r = await fetch(`${API}/issues/comments?per_page=30&sort=created&direction=desc`, { headers: { Accept: "application/vnd.github+json" } });
    if (!r.ok) return null;
    const list = await r.json();
    for (const c of list) {
      const m = (c.body || "").match(/\{[\s\S]*\}/);
      if (!m) continue;
      try { const j = JSON.parse(m[0]); if (j.hunt_id === huntId) return j as HuntSnapshot; } catch {}
    }
  } catch {}
  return null;
}

export async function pollLatest(): Promise<HuntSnapshot | null> {
  try {
    const r = await fetch(`${RAW_LATEST}?t=${Date.now()}`);
    if (!r.ok) return null;
    const j = await r.json();
    return { hunt_id: j.hunt_id || "", status: j.status || "unknown", stage: j.stage || "DONE", progress: j.progress ?? 1, lines: j.lines || [], result: j };
  } catch { return null; }
}

export async function loadUniverse(): Promise<MarketUniverse> {
  return { generated_at_utc: new Date().toISOString(), sources_status: {}, fixture_count: 0, fixtures: [] };
}
export async function refreshUniverse(): Promise<MarketUniverse> { return loadUniverse(); }
export async function pollStatus(): Promise<HuntSnapshot | null> { return pollLatest(); }
