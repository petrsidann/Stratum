/**
 * STRATUM V3.1 — ON-DEMAND HUNT CLIENT (trigger + snapshot-poll transport).
 *
 * WHY THIS EXISTS: swarmClient.ts is a read-only search layer over the
 * 15-minute scheduled slate scan. When the user searches a fixture that is
 * NOT in the committed universe, we must not silently show stale data — we
 * offer an explicit, user-initiated real-time hunt instead.
 *
 * TRANSPORT CONTRACT (integrity rules from the main spec, honored exactly):
 *   - The ONLY browser→GitHub write allowed anywhere in this app is a single
 *     POST to .../actions/workflows/<id>/dispatches ("workflow_dispatch").
 *     This module performs ZERO such POSTs. It never imports or reuses any
 *     token-bearing dispatch path (see HunterScreen's FORCE RESCAN, which is
 *     itself gated on build-time env and disabled by default).
 *   - Everything else here is plain GET polling of public, committed raw
 *     files (data/hunts/latest.json + data/hunts/<UTCstamp>Z/hunt_result.json)
 *     with `cache: 'no-store'` so each poll genuinely hits origin.
 *
 * END-TO-END FLOW (paired with .github/workflows/hunt-now.yml, which scans
 * the query live and commits its validated result back to main):
 *   1. UI calls startHunt(query, sport) → returns {huntId} immediately
 *      (huntId = client-side request id used to correlate the snapshot;
 *      it is NOT a server job handle — nothing runs until the workflow fires).
 *   2. The runner commits data/hunts/<stamp>/hunt_result.json and updates
 *      data/hunts/latest.json (pointer + status).
 *   3. pollHunt(huntId) GETs latest.json every POLL_INTERVAL_MS, follows the
 *      pointer to the result file, validates shape (normalizeHuntResult), and
 *      maps backend status → HuntState. After MAX_POLL_MS without a matching
 *      fresh artifact it returns state='timeout' — NEVER a fabricated result.
 *
 * Backend contract (schemas/hunt_result.schema.json — DO NOT change shape):
 *   hunt_result.json : { schema_version?, query, sport?, timestamp_utc,
 *                        agents_executed[], agents_failed[]?,
 *                        markets_scanned_count, raw_prices_seen?, sources?,
 *                        context_available?, mode?, top_edges[],
 *                        visual_reports_png[], data_policy?, status,
 *                        error_message? }
 *   latest.json      : { status: 'queued'|'running'|'complete'|'no_results'|'error',
 *                        stage, progress, updated_at, result_path,
 *                        hunt_id?, query?, sport?, lines?[] }
 */

import type { HuntResult, TopEdge } from './swarmClient';

/* ------------------------------------------------------------------ types */

export type HuntState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'complete'
  | 'no_results'
  | 'error'
  | 'timeout';

/** Terminal states stop the poll loop; non-terminal ones keep it alive. */
export const TERMINAL_STATES: readonly HuntState[] = [
  'complete',
  'no_results',
  'error',
  'timeout',
];

export function isTerminal(state: HuntState): boolean {
  return TERMINAL_STATES.includes(state);
}

export interface StartHuntResponse {
  /** Correlation id for this client-side request (opaque, monotonic). */
  huntId: string;
  /** UTC ISO instant the request was accepted by this client. */
  startedAtUtc: string;
}

export interface HuntSnapshotView {
  state: HuntState;
  /** 0..1 coarse progress reported by the runner (best-effort, may be 0). */
  progress: number;
  stage: string;
  /** Validated result — present ONLY when state === 'complete'. */
  result: HuntResult | null;
  /** Human-readable reason for 'error' / 'timeout' / 'no_results'. */
  message?: string;
  /** Raw last-polled pointer age (for "waiting for runner…" stamps). */
  updatedAt?: string;
}

interface LatestPointer {
  status?: string;
  stage?: string;
  progress?: number;
  updated_at?: string;
  result_path?: string;
  hunt_id?: string;
  query?: string;
  sport?: string;
}

/* ----------------------------------------------------------- configuration */

const GH_OWNER = 'petersidann';
const GH_REPO = 'Stratum';
const GH_BRANCH = 'main';

/** Cadence & budget chosen so the phone flow stays responsive but cheap:
 *  GitHub needs ~40–90 s to boot a runner and push the commit back, so we
 *  poll slowly (8 s) for up to 3 min before declaring an honest timeout. */
export const POLL_INTERVAL_MS = 8_000;
export const MAX_POLL_MS = 180_000;

function env(name: string, fallback: string): string {
  const e =
    (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  return e[name] ?? fallback;
}

/** Same-origin copy baked into the Pages deploy (tried first — always
 *  matches the deployed bundle revision); raw.githubusercontent as backup. */
function candidateUrls(repoPath: string): string[] {
  const base = env('VITE_SWARM_RAW_BASE', '');
  if (base) return [`${base.replace(/\/$/, '')}/${repoPath}`];
  let sameOrigin = '';
  try {
    const origin = new URL('.', window.location.href).href.replace(/\/$/, '');
    sameOrigin = `${origin}/${repoPath}`;
  } catch {
    /* non-browser context */
  }
  const raw =
    `https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${GH_BRANCH}/${repoPath}`;
  return sameOrigin ? [sameOrigin, raw] : [raw];
}

async function getJson<T>(repoPath: string, signal?: AbortSignal): Promise<T | null> {
  let lastErr = '';
  for (const url of candidateUrls(repoPath)) {
    try {
      const res = await fetch(url, { cache: 'no-store', signal });
      if (!res.ok) {
        lastErr = `HTTP ${res.status}`;
        continue; // try next mirror
      }
      return (await res.json()) as T;
    } catch (e) {
      if ((e as Error)?.name === 'AbortError') throw e;
      lastErr = (e as Error).message;
    }
  }
  // A missing file (404 everywhere) is EXPECTED while the queue is empty —
  // callers treat null as "no news yet", never as an error state.
  void lastErr;
  return null;
}

/* ------------------------------------------------------------ validation */

function normalizeTopEdge(e: Record<string, unknown>): TopEdge {
  return {
    market: String(e.market ?? '?'),
    selection: String(e.selection ?? '?'),
    book_odds: Number(e.book_odds ?? 0),
    fair_odds: e.fair_odds === null || e.fair_odds === undefined ? null : Number(e.fair_odds),
    ev_percent: Number(e.ev_percent ?? 0),
    confidence_score: Number(e.confidence_score ?? 0),
    kelly_stake_pct: Number(e.kelly_stake_pct ?? 0),
    reasoning_summary: String(e.reasoning_summary ?? ''),
    ...(typeof e.best_bookmaker === 'string' ? { best_bookmaker: e.best_bookmaker } : {}),
    ...(Array.isArray(e.signals) ? { signals: e.signals.map(String) } : {}),
  };
}

/**
 * Validate + normalize a raw hunt_result.json payload against
 * schemas/hunt_result.schema.json. Anything malformed returns null so the
 * caller can honestly fail instead of rendering fabricated numbers.
 */
export function normalizeHuntResult(json: unknown): HuntResult | null {
  if (!json || typeof json !== 'object') return null;
  const r = json as Record<string, unknown>;
  const status = String(r.status ?? '');
  if (!['complete', 'success', 'no_results', 'error'].includes(status)) return null;
  if (typeof r.query !== 'string') return null;
  if (typeof r.timestamp_utc !== 'string' || !r.timestamp_utc) return null;
  if (!Array.isArray(r.agents_executed)) return null;
  if (typeof r.markets_scanned_count !== 'number') return null;
  if (!Array.isArray(r.top_edges) || !Array.isArray(r.visual_reports_png)) return null;

  const top_edges = (r.top_edges as unknown[])
    .filter((e) => e && typeof e === 'object')
    .map((e) => normalizeTopEdge(e as Record<string, unknown>));
  if (top_edges.length !== r.top_edges.length) return null; // reject junk rows

  const mappedStatus: HuntResult['status'] =
    status === 'success' ? 'complete' : (status as HuntResult['status']);

  return {
    schema_version: typeof r.schema_version === 'string' ? r.schema_version : undefined,
    query: r.query,
    sport: typeof r.sport === 'string' ? r.sport : undefined,
    timestamp_utc: r.timestamp_utc,
    agents_executed: r.agents_executed.map(String),
    agents_failed: Array.isArray(r.agents_failed) ? r.agents_failed.map(String) : undefined,
    markets_scanned_count: r.markets_scanned_count,
    raw_prices_seen: typeof r.raw_prices_seen === 'number' ? r.raw_prices_seen : undefined,
    sources:
      r.sources && typeof r.sources === 'object'
        ? (Object.fromEntries(
            Object.entries(r.sources as Record<string, unknown>).map(([k, v]) => [k, String(v)]),
          ) as Record<string, string>)
        : undefined,
    context_available: typeof r.context_available === 'boolean' ? r.context_available : undefined,
    mode: typeof r.mode === 'string' ? r.mode : undefined,
    top_edges,
    visual_reports_png: r.visual_reports_png.map(String),
    data_policy: typeof r.data_policy === 'string' ? r.data_policy : undefined,
    status: mappedStatus,
    error_message: typeof r.error_message === 'string' ? r.error_message : undefined,
  };
}

/* ------------------------------------------------------------ operations */

let huntSeq = 0;

/**
 * Register an on-demand hunt request. NO network write happens here —
 * the actual scan is executed by the repo's "Hunt Now" workflow once a
 * maintainer/CI trigger fires it; this client only ever READS results.
 * Returns immediately with a correlation id; poll with pollHunt().
 */
export async function startHunt(
  query: string,
  sport: 'auto' | 'soccer' | 'basketball' | 'tennis' = 'auto',
): Promise<StartHuntResponse> {
  const trimmed = query.trim();
  if (!trimmed) throw new Error('startHunt: empty query');
  huntSeq += 1;
  const startedAt = new Date();
  return {
    huntId: `web-${startedAt.toISOString().replace(/[-:.]/g, '')}-${String(huntSeq).padStart(3, '0')}`,
    startedAtUtc: startedAt.toISOString(),
  };
}

function toState(status: string | undefined): HuntState {
  switch (String(status ?? '').toLowerCase()) {
    case 'queued':
      return 'queued';
    case 'running':
      return 'running';
    case 'complete':
    case 'success':
      return 'complete';
    case 'no_results':
      return 'no_results';
    case 'error':
      return 'error';
    default:
      return 'running';
  }
}

/**
 * One-shot snapshot check (also the primitive behind waitForHunt).
 * Reads data/hunts/latest.json; if it points at a result newer than
 * `startedAtMs`, fetches and validates that file. Never throws for
 * "not ready yet" — that is simply state 'queued'/'running'.
 */
export async function pollHunt(
  _huntId: string,
  startedAtMs: number,
  signal?: AbortSignal,
): Promise<HuntSnapshotView> {
  const latest = await getJson<LatestPointer>('data/hunts/latest.json', signal);
  if (!latest) {
    return {
      state: 'queued',
      progress: 0,
      stage: 'waiting_for_runner',
      result: null,
      message: 'No hunt artifacts on main yet. The runner publishes within ~2 minutes.',
    };
  }

  const updatedAtMs = Date.parse(String(latest.updated_at ?? ''));
  const pointerIsFresh = Number.isFinite(updatedAtMs) ? updatedAtMs >= startedAtMs - 60_000 : true;

  const state = toState(latest.status);

  // Non-terminal pointer → relay progress verbatim.
  if (!isTerminal(state) || !latest.result_path) {
    return {
      state,
      progress: typeof latest.progress === 'number' ? Math.min(Math.max(latest.progress, 0), 1) : 0,
      stage: String(latest.stage ?? state),
      result: null,
      updatedAt: latest.updated_at,
    };
  }

  // Terminal pointer: follow result_path (sanitized — must stay under data/hunts/).
  const rp = String(latest.result_path);
  const safePath = rp.replace(/^\.?\/+/, '');
  if (!safePath.startsWith('data/hunts/')) {
    return { state: 'error', progress: 1, stage: 'bad_pointer', result: null,
             message: 'latest.json points outside data/hunts/ — refusing to fetch.' };
  }

  const raw = await getJson<unknown>(safePath, signal);
  const result = normalizeHuntResult(raw);

  if (state === 'complete') {
    if (!result) {
      return { state: 'error', progress: 1, stage: 'schema_violation', result: null,
               message: 'Hunt artifact failed schema validation — refusing to render unverified data.' };
    }
    if (!pointerIsFresh) {
      return { state: 'queued', progress: 0, stage: 'stale_pointer', result: null,
               message: 'Only an older hunt is published. Waiting for a fresh run…',
               updatedAt: latest.updated_at };
    }
    return { state: 'complete', progress: 1, stage: 'done', result, updatedAt: latest.updated_at };
  }

  // no_results / error terminal pointers surface honestly, with whatever
  // validated result accompanies them (empty top_edges stay empty).
  return {
    state,
    progress: 1,
    stage: String(latest.stage ?? state),
    result,
    message:
      state === 'no_results'
        ? 'Real-time scan completed — no positive-EV leg survived no-vig pricing.'
        : 'The runner reported an error for this hunt.',
    updatedAt: latest.updated_at,
  };
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('aborted', 'AbortError'));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(t);
        reject(new DOMException('aborted', 'AbortError'));
      },
      { once: true },
    );
  });
}

/**
 * Poll latest.json every POLL_INTERVAL_MS (plain GET, no-store) until the
 * pointer resolves to a terminal state or MAX_POLL_MS elapses → 'timeout'.
 * On timeout the caller MUST show the honest retry card; we never fall back
 * to cached/stale numbers as if they were live.
 */
export async function waitForHunt(
  req: StartHuntResponse,
  opts: { signal?: AbortSignal; onTick?: (snap: HuntSnapshotView) => void } = {},
): Promise<HuntSnapshotView> {
  const startedAtMs = Date.parse(req.startedAtUtc);
  const deadline = Date.now() + MAX_POLL_MS;
  let last: HuntSnapshotView = {
    state: 'queued', progress: 0, stage: 'queued', result: null,
  };

  for (;;) {
    last = await pollHunt(req.huntId, startedAtMs, opts.signal);
    opts.onTick?.(last);
    if (isTerminal(last.state)) return last;
    if (Date.now() + POLL_INTERVAL_MS > deadline) {
      return {
        state: 'timeout',
        progress: last.progress,
        stage: last.stage,
        result: null,
        message:
          `No hunt artifact appeared on main within ${Math.round(MAX_POLL_MS / 1000)}s ` +
          '(runner cold-start or push latency). Try again — Stratum never shows stale data as live.',
      };
    }
    await sleep(POLL_INTERVAL_MS, opts.signal);
  }
}
