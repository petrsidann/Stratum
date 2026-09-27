/**
 * STRATUM V3.0 — Swarm polling layer.
 *
 * Transport contract (backend: scripts/swarm_runner.py — DO NOT change):
 *   - The swarm runner atomically rewrites data/hunt_status.json every ~1s
 *     while a hunt is live:
 *       { version, query, sport, status, stage, progress, agents,
 *         elapsed_s, updated_at, lines: [{t, agent, msg}], result, message }
 *   - Frontend polls that file (raw.githubusercontent.com in production,
 *     local dev server in development) — no websockets needed for MVP.
 *
 * Deployment setup required for GH-Pages builds (documented placeholder):
 *   1. Deploy a tiny Cloudflare Worker that accepts POST {query, sport} and
 *      calls GitHub's workflow_dispatch API for .github/workflows/deploy-swarm.yml
 *      using a repo-scoped fine-grained PAT (Actions:write). The worker must
 *      also allow CORS from the Pages origin.
 *   2. Put its URL in app/.env.production:
 *        VITE_SWARM_TRIGGER_URL=https://stratum-trigger.<account>.workers.dev/hunt/start
 *      (optional override at runtime: localStorage['stratum.swarmTriggerUrl'])
 *   3. In local dev either set VITE_SWARM_TRIGGER_URL to the embedded dev
 *      server (`python3 scripts/swarm_runner.py serve --port 8788`) or just
 *      rely on the raw-github / same-origin status poll below.
 */

/* ------------------------------------------------------------------ types */

export type SwarmAgent = 'SCOUT' | 'ACTUARY' | 'CONTEXT' | 'STRATEGIST';

/** Console line as consumed by HunterConsole. SWARM = orchestrator chatter. */
export interface HuntLine {
  agent: SwarmAgent | 'SWARM';
  text: string;
  ts: number; // epoch ms
}

export type HuntStatus = 'idle' | 'running' | 'complete' | 'no_results' | 'error';

/** One edge row — mirrors schemas/hunt_result.schema.json #/definitions/topEdge */
export interface TopEdge {
  market: string;
  selection: string;
  book_odds: number;
  fair_odds: number | null;
  ev_percent: number;
  confidence_score: number;
  kelly_stake_pct: number;
  reasoning_summary: string;
  best_bookmaker?: string;
  signals?: string[];
}

/** Final report — mirrors schemas/hunt_result.schema.json */
export interface HuntResult {
  schema_version?: string;
  query: string;
  sport?: string;
  timestamp_utc: string;
  agents_executed: string[];
  agents_failed?: string[];
  markets_scanned_count: number;
  raw_prices_seen?: number;
  sources?: Record<string, string>;
  context_available?: boolean;
  mode?: string;
  top_edges: TopEdge[];
  visual_reports_png: string[];
  data_policy?: string;
  status: Extract<HuntStatus, 'complete' | 'no_results' | 'error'> | string;
  error_message?: string;
}

/** Poll response — schema fields + live telemetry wrapper. */
export interface HuntSnapshot {
  version?: string;
  query: string;
  sport: string;
  status: HuntStatus;
  stage: string;
  progress: number; // 0..1
  agents?: Record<string, string>;
  elapsed_s?: number;
  updated_at?: string;
  lines: HuntLine[];
  result: HuntResult | null;
  message?: string;
}

/* ----------------------------------------------------------- configuration */

const GH_OWNER = 'petersidann';
const GH_REPO = 'Stratum';
const GH_BRANCH = 'main';

/** Where the swarm runner publishes live telemetry (public repo → no auth). */
export const STATUS_URL_PROD =
  `https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${GH_BRANCH}/data/hunt_status.json`;

/** Same file served by `python3 scripts/swarm_runner.py serve` in local dev. */
export const STATUS_URL_LOCAL = 'http://127.0.0.1:8788/hunt/status';

/** Poll cadence per spec: every 800ms. */
export const POLL_INTERVAL_MS = 800;

/** Hard client-side watchdog per spec: 90s → NetworkError. */
export const HUNT_TIMEOUT_MS = 90_000;

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
    Object.setPrototypeOf(this, NetworkError.prototype);
  }
}

function envTriggerUrl(): string {
  try {
    const stored = localStorage.getItem('stratum.swarmTriggerUrl');
    if (stored) return stored;
  } catch {
    /* SSR / privacy mode */
  }
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  return env.VITE_SWARM_TRIGGER_URL ?? '';
}

function isDev(): boolean {
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  return !!env.DEV;
}

function statusUrl(): string {
  return isDev() ? STATUS_URL_LOCAL : STATUS_URL_PROD;
}

/* ------------------------------------------------------------ start (POST) */

/**
 * Kick off a swarm hunt.
 *  - Local dev: POST http://127.0.0.1:8788/hunt/start (embedded dev server).
 *  - Production PWA: POST to the Cloudflare Worker trigger (env flag above),
 *    which dispatches deploy-swarm.yml via GitHub's workflow_dispatch API.
 * Throws NetworkError when no transport is reachable/configured.
 */
export async function startHunt(query: string, sport: string): Promise<void> {
  const trimmed = query.trim();
  if (!trimmed) throw new NetworkError('Empty hunt query.');
  const body = JSON.stringify({ query: trimmed, sport });

  const candidates: string[] = [];
  if (isDev()) candidates.push('http://127.0.0.1:8788/hunt/start');
  const trigger = envTriggerUrl();
  if (trigger) candidates.push(trigger);

  let lastErr = '';
  for (const url of candidates) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        cache: 'no-store',
      });
      // 202 accepted | 409 already running → both mean "a hunt owns the slot"
      if (res.ok || res.status === 409) return;
      lastErr = `${url} -> HTTP ${res.status}`;
    } catch (e) {
      lastErr = e instanceof Error ? e.message : String(e);
    }
  }
  throw new NetworkError(
    `Unable to deploy hunters (${lastErr || 'no trigger configured'}). ` +
      'Start the local dev server or set VITE_SWARM_TRIGGER_URL.',
  );
}

/* ------------------------------------------------------------- poll (GET) */

interface RawLine {
  t?: number;
  agent?: string;
  msg?: string;
  text?: string;
  ts?: number;
}

function normalizeLines(raw: unknown, updatedMs: number): HuntLine[] {
  if (!Array.isArray(raw)) return [];
  const out: HuntLine[] = [];
  for (const l of raw as RawLine[]) {
    const text = typeof l.msg === 'string' ? l.msg : typeof l.text === 'string' ? l.text : '';
    if (!text) continue;
    const agentRaw = String(l.agent ?? 'SWARM').toUpperCase();
    const agent: HuntLine['agent'] =
      agentRaw === 'SCOUT' || agentRaw === 'ACTUARY' || agentRaw === 'CONTEXT' || agentRaw === 'STRATEGIST'
        ? agentRaw
        : 'SWARM';
    // Backend logs relative seconds (t); fall back to snapshot update time.
    const ts = typeof l.ts === 'number' ? l.ts : updatedMs + (typeof l.t === 'number' ? Math.round(l.t * 1000) : 0);
    out.push({ agent, text, ts });
  }
  return out;
}

function normalizeSnapshot(json: unknown): HuntSnapshot {
  const s = (json ?? {}) as Record<string, unknown>;
  const updatedMs = Date.parse(String(s.updated_at ?? '')) || Date.now();
  const status = (String(s.status ?? 'idle') as HuntStatus) || 'idle';
  return {
    version: s.version as string | undefined,
    query: String(s.query ?? ''),
    sport: String(s.sport ?? ''),
    status,
    stage: String(s.stage ?? status),
    progress: typeof s.progress === 'number' ? Math.min(Math.max(s.progress, 0), 1) : 0,
    agents: s.agents as Record<string, string> | undefined,
    elapsed_s: s.elapsed_s as number | undefined,
    updated_at: s.updated_at as string | undefined,
    lines: normalizeLines(s.lines, updatedMs),
    result: (s.result as HuntResult | null) ?? null,
    message: s.message as string | undefined,
  };
}

/**
 * Single-shot status fetch (typed + normalized). Throws NetworkError on
 * transport/parse failure so callers can decide whether to keep retrying.
 */
export async function fetchStatus(signal?: AbortSignal): Promise<HuntSnapshot> {
  let res: Response;
  try {
    res = await fetch(statusUrl(), { cache: 'no-store', signal });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new NetworkError(`Status poll failed: ${(e as Error).message}`);
  }
  if (!res.ok) throw new NetworkError(`Status poll returned HTTP ${res.status}.`);
  try {
    return normalizeSnapshot(await res.json());
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new NetworkError('Status payload is not valid JSON.');
  }
}

/**
 * Poll data/hunt_status.json every 800ms until the swarm reports a terminal
 * state ('complete' | 'no_results' | 'error'). Transient network blips are
 * tolerated (the file is rewritten atomically every second); the whole loop
 * aborts with NetworkError("Hunt stalled or network unavailable.") after 90s.
 */
export async function pollStatus(signal?: AbortSignal): Promise<HuntSnapshot> {
  const deadline = Date.now() + HUNT_TIMEOUT_MS;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (signal?.aborted) throw new DOMException('Polling aborted', 'AbortError');
    let snap: HuntSnapshot | null = null;
    try {
      snap = await fetchStatus(signal);
    } catch (e) {
      if ((e as Error)?.name === 'AbortError') throw e;
      snap = null; // transient — retry until deadline
    }
    if (snap && snap.status !== 'idle' && snap.status !== 'running') return snap;
    if (Date.now() >= deadline) {
      throw new NetworkError('Hunt stalled or network unavailable.');
    }
    await new Promise<void>((resolve) => {
      const id = window.setTimeout(resolve, POLL_INTERVAL_MS);
      signal?.addEventListener(
        'abort',
        () => {
          window.clearTimeout(id);
          resolve();
        },
        { once: true },
      );
    });
  }
}
