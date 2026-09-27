/**
 * Hunter Mode client.
 *
 * One hunt = one real-time, query-scoped scan executed by scripts/hunter_api.py.
 * Transport order (first reachable wins -- NO cached/empty data is ever
 * rendered as if it were live):
 *
 *   1. Local dev trigger:  GET http://127.0.0.1:8787/hunt?query=...&sport=...
 *      (run `python3 scripts/hunter_api.py serve` alongside `npm run dev`).
 *   2. Configured endpoint: localStorage['stratum.huntEndpoint'] -> a proxy in
 *      front of the backend / API gateway, same contract.
 *   3. GitHub Actions fallback: dispatch the "Hunt Now" workflow
 *      (workflow_dispatch ONLY -- no cron) and poll the run for the
 *      `hunt-result.json` artifact. Requires an optional fine-grained PAT in
 *      localStorage['stratum.ghToken'] with Actions:write on the repo;
 *      without a token this transport is skipped (private artifacts also
 *      need auth).
 *
 * Integrity: every response is validated against the hunt schema; anything
 * malformed resolves as a failure so the UI shows the honest ERROR state
 * instead of fabricated numbers.
 */

export interface HuntEdge {
  match_id: string;
  sport: string;
  icon: string;
  home_team: string;
  away_team: string;
  commence: string;
  market_type: string;
  market_name: string;
  line: number | null;
  selection: string;
  best_bookmaker: string;
  best_price: number;
  fair_probability: number;
  raw_probability: number;
  edge_percent: number;
  ev_percent: number;
  kelly_stake: number;
  confidence: number;
  signals: string[];
  num_bookmakers?: number;
}

export interface HuntResult {
  status: 'success' | 'no_results' | 'error';
  query: string;
  sport: string;
  generated_at?: string;
  sources?: Record<string, string>;
  markets_scanned: number;
  picks?: unknown[];
  edges: HuntEdge[];
  message?: string;
}

// Repo backing the "Hunt Now" workflow (same origin as the Pages deploy).
const GH_OWNER = 'petersidann';
const GH_REPO = 'Stratum';
const GH_WORKFLOW = 'hunt-now.yml';

// Hard client-side guard mirroring the backend timeout policy: a hunt that
// drags past this window is aborted so the UI can never hang.
export const HUNT_TIMEOUT_MS = 45_000;

function ls(key: string): string {
  try {
    return localStorage.getItem(key) ?? '';
  } catch {
    return '';
  }
}

function isHuntResult(x: unknown): x is HuntResult {
  const r = x as HuntResult;
  return (
    !!r &&
    typeof r === 'object' &&
    typeof r.status === 'string' &&
    Array.isArray(r.edges) &&
    typeof r.markets_scanned === 'number'
  );
}

async function fetchJson(url: string, signal: AbortSignal, init?: RequestInit): Promise<unknown | null> {
  try {
    const res = await fetch(url, { cache: 'no-store', ...init, signal });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/* ------------------------- transport 1: local server -------------------- */

async function huntViaLocalServer(
  query: string,
  sport: string,
  signal: AbortSignal
): Promise<HuntResult | null> {
  const url = `http://127.0.0.1:8787/hunt?query=${encodeURIComponent(
    query
  )}&sport=${encodeURIComponent(sport)}`;
  const data = await fetchJson(url, signal);
  return isHuntResult(data) ? data : null;
}

/* --------------------- transport 2: configured endpoint ----------------- */

async function huntViaConfiguredEndpoint(
  query: string,
  sport: string,
  signal: AbortSignal
): Promise<HuntResult | null> {
  const base = ls('stratum.huntEndpoint');
  if (!base) return null;
  const sep = base.includes('?') ? '&' : '?';
  const url = `${base}${sep}query=${encodeURIComponent(query)}&sport=${encodeURIComponent(sport)}`;
  const data = await fetchJson(url, signal);
  return isHuntResult(data) ? data : null;
}

/* ------------------ transport 3: GitHub Actions dispatch ---------------- */

function ghHeaders(): Record<string, string> {
  const token = ls('stratum.ghToken');
  const h: Record<string, string> = { Accept: 'application/vnd.github+json' };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(t);
      reject(new DOMException('aborted', 'AbortError'));
    });
  });

async function huntViaWorkflowDispatch(
  query: string,
  sport: string,
  signal: AbortSignal
): Promise<HuntResult | null> {
  const token = ls('stratum.ghToken');
  if (!token) return null; // private artifacts/dispatch need auth; skip honestly

  const api = 'https://api.github.com/repos';

  // Remember the newest run number BEFORE dispatching so we only read fresh scans.
  const before = (await fetchJson(
    `${api}/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/runs?per_page=1`,
    signal,
    { headers: ghHeaders() }
  )) as { workflow_runs?: { run_number: number }[] } | null;
  const lastRunNumber = before?.workflow_runs?.[0]?.run_number ?? 0;

  try {
    const dispatch = await fetch(
      `${api}/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/dispatches`,
      {
        method: 'POST',
        headers: { ...ghHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ref: 'main',
          inputs: { match_query: query, sport },
        }),
        signal,
      }
    );
    if (dispatch.status !== 204) return null;
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    return null;
  }

  // Poll for the new completed run (bounded by the caller's abort signal).
  let runId = 0;
  for (let attempt = 0; attempt < 40; attempt++) {
    await sleep(5000, signal);
    const runs = (await fetchJson(
      `${api}/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/runs?per_page=5`,
      signal,
      { headers: ghHeaders() }
    )) as
      | { workflow_runs?: { id: number; run_number: number; status: string; conclusion: string }[] }
      | null;
    const run = runs?.workflow_runs?.find((r) => r.run_number > lastRunNumber);
    if (!run) continue;
    if (run.status === 'completed') {
      if (run.conclusion !== 'success' && run.conclusion !== 'failure') return null;
      runId = run.id;
      break;
    }
  }
  if (!runId) return null;

  const arts = (await fetchJson(
    `${api}/${GH_OWNER}/${GH_REPO}/actions/runs/${runId}/artifacts`,
    signal,
    { headers: ghHeaders() }
  )) as { artifacts?: { name: string; download_url: string }[] } | null;
  const artifact = arts?.artifacts?.find((a) => a.name === 'hunt-result');
  if (!artifact) return null;

  try {
    const zipRes = await fetch(artifact.download_url, {
      headers: ghHeaders(),
      signal,
    });
    if (!zipRes.ok) return null;
    const json = await extractHuntJsonFromZip(await zipRes.arrayBuffer());
    return json && isHuntResult(json) ? json : null;
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    return null;
  }
}

/**
 * Minimal ZIP reader (stored + deflate entries) sufficient to pull
 * hunt-result.json out of the artifact archive without any dependency.
 */
async function extractHuntJsonFromZip(buf: ArrayBuffer): Promise<unknown | null> {
  try {
    const dv = new DataView(buf);
    const u8 = new Uint8Array(buf);
    // Locate End Of Central Directory (signature 0x06054b50) scanning backwards.
    let eocd = -1;
    for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 66000); i--) {
      if (dv.getUint32(i, true) === 0x06054b50) {
        eocd = i;
        break;
      }
    }
    if (eocd < 0) return null;
    const cdCount = dv.getUint16(eocd + 10, true);
    let ptr = dv.getUint32(eocd + 16, true); // central directory offset

    const inflate = async (data: Uint8Array): Promise<Uint8Array> => {
      // DecompressionStream('deflate-raw') is available in all evergreen browsers.
      const ds = new DecompressionStream('deflate-raw');
      const stream = new Blob([data as unknown as BlobPart]).stream().pipeThrough(ds);
      return new Uint8Array(await new Response(stream).arrayBuffer());
    };

    for (let n = 0; n < cdCount; n++) {
      if (dv.getUint32(ptr, true) !== 0x02014b50) break;
      const method = dv.getUint16(ptr + 10, true);
      const compSize = dv.getUint32(ptr + 20, true);
      const nameLen = dv.getUint16(ptr + 28, true);
      const extraLen = dv.getUint16(ptr + 30, true);
      const commentLen = dv.getUint16(ptr + 32, true);
      const localOff = dv.getUint32(ptr + 42, true);
      const name = new TextDecoder().decode(u8.subarray(ptr + 46, ptr + 46 + nameLen));
      ptr += 46 + nameLen + extraLen + commentLen;

      if (!name.endsWith('hunt-result.json')) continue;

      // Jump to local header to find actual data start.
      const lNameLen = dv.getUint16(localOff + 26, true);
      const lExtraLen = dv.getUint16(localOff + 28, true);
      const dataStart = localOff + 30 + lNameLen + lExtraLen;
      const data = u8.subarray(dataStart, dataStart + compSize);
      const bytes = method === 0 ? data : await inflate(data);
      return JSON.parse(new TextDecoder().decode(bytes));
    }
    return null;
  } catch {
    return null;
  }
}

/* --------------------------------- public API --------------------------- */

/**
 * Runs ONE hunt for the given fixture. Resolves with the structured result;
 * throws only on abort/timeout so the UI can distinguish user cancellation
 * from "no markets detected".
 */
export async function runHunt(query: string, sport: string, outerSignal?: AbortSignal): Promise<HuntResult> {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new DOMException('hunt timeout', 'TimeoutError')),
    HUNT_TIMEOUT_MS
  );
  const onOuterAbort = () => controller.abort(new DOMException('hunt cancelled', 'AbortError'));
  outerSignal?.addEventListener('abort', onOuterAbort);

  const transports = [
    () => huntViaLocalServer(query, sport, controller.signal),
    () => huntViaConfiguredEndpoint(query, sport, controller.signal),
    () => huntViaWorkflowDispatch(query, sport, controller.signal),
  ];

  let lastErr: unknown = null;
  try {
    for (const t of transports) {
      const res = await t().catch((e) => {
        if ((e as Error)?.name === 'AbortError') throw e;
        lastErr = e;
        return null;
      });
      if (res) {
        return { ...res, query: res.query || query };
      }
      if (controller.signal.aborted) break;
    }
    if (controller.signal.aborted) {
      throw new DOMException(
        outerSignal?.aborted ? 'hunt cancelled' : 'Hunt timed out across all transports',
        outerSignal?.aborted ? 'AbortError' : 'TimeoutError'
      );
    }
    // No transport reachable and nothing scraped: honest empty result. The
    // UI maps this to its ERROR state -- never fake rows.
    return {
      status: 'no_results',
      query,
      sport,
      markets_scanned: 0,
      edges: [],
      message:
        lastErr instanceof Error
          ? `No markets detected for '${query}'. Try exact team names. (${lastErr.message})`
          : `No markets detected for '${query}'. Try exact team names. ` +
            `(Start the hunter with: python3 scripts/hunter_api.py serve --port 8787, ` +
            `or configure a hunt endpoint / GitHub token.)`,
    };
  } finally {
    clearTimeout(timer);
    outerSignal?.removeEventListener('abort', onOuterAbort);
  }
}
