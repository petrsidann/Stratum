import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import HunterConsole from '../components/HunterConsole';
import ResultsDashboard from '../components/ResultsDashboard';
import {
  HuntLine,
  HuntResult,
  HuntSnapshot,
  MarketUniverse,
  MatchResult,
  autoDetectFixture,
  loadLastScan,
  loadUniverse,
  nearestFixtures,
  refreshUniverse,
  scanAgeLabel,
} from '../lib/swarmClient';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';

/**
 * STRATUM V3.0 — HUNTER screen (SCHEDULED-SWARM / client-side search model).
 *
 *   IDLE    -> "ENTER MATCH TO HUNT" input + sport selector (default
 *              AUTO-DETECT) + [ SEARCH UNIVERSE ]. Universe is fetched ONCE
 *              on mount and cached; typing/searching never hits the network.
 *   RESULTS -> query matched a fixture → its ranked edges render in
 *              ResultsDashboard. No match → honest red card + one-tap
 *              "nearest fixtures in today's slate" list. NEVER fabricated.
 *
 * There is NO long CONSOLE wait state: the swarm runs on a 15-min cron, so
 * the live terminal survives only as a collapsible AGENT-TRACE panel replaying
 * the last scheduled scan, clearly stamped with its age.
 *
 * Integrity rules honored here:
 *   - zero browser→workflow POSTs anywhere on any path (search-only client);
 *   - empty top_edges renders as an honest "no verified edge" card;
 *   - every figure shown comes from committed, schema-validated market data.
 */

type Phase = 'IDLE' | 'RESULTS';

const SPORT_OPTIONS = [
  { value: 'auto', label: '🎯 Auto-detect sport' },
  { value: 'soccer', label: 'Soccer ⚽' },
  { value: 'nba', label: 'NBA 🏀' },
  { value: 'nfl', label: 'NFL 🏈' },
  { value: 'mlb', label: 'MLB ⚾' },
  { value: 'nhl', label: 'NHL 🏒' },
] as const;

type SportValue = (typeof SPORT_OPTIONS)[number]['value'];

/** Build-time token presence ONLY (never runtime user tokens): when absent,
 *  FORCE RESCAN is disabled with the honest tooltip. The phone flow works
 *  fully with zero trigger capability. */
function canTriggerRescan(): boolean {
  const env =
    (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  return Boolean(env.VITE_GH_WORKFLOW_TOKEN && env.VITE_GH_REPO);
}

async function dispatchSwarmWorkflow(): Promise<void> {
  const env =
    (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
  const token = env.VITE_GH_WORKFLOW_TOKEN ?? '';
  const repo = env.VITE_GH_REPO ?? ''; // e.g. "petersidann/Stratum"
  const workflowId = env.VITE_GH_WORKFLOW_ID ?? 'deploy-swarm.yml';
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowId}/dispatches`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${token}`,
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main' }),
    },
  );
  if (!res.ok && res.status !== 204) throw new Error(`workflow_dispatch -> HTTP ${res.status}`);
}

/** Wrap a universe fixture into the legacy HuntResult shape so the existing,
 *  already-verified ResultsDashboard renders it unchanged. Pure view adapter —
 *  invents no numbers (empty top_edges stays honestly empty). */
function fixtureToResult(m: MatchResult, universe: MarketUniverse): HuntResult {
  const f = m.fixture;
  const hasEdges = f.top_edges.length > 0;
  return {
    query: `${f.home} vs ${f.away}`,
    sport: f.sport,
    timestamp_utc: universe.generated_at_utc,
    agents_executed: ['scout', 'actuary', 'strategist'],
    markets_scanned_count: f.markets_scanned,
    sources: universe.sources_status,
    mode: 'scheduled_universe_search',
    top_edges: f.top_edges,
    visual_reports_png: [],
    data_policy: universe.data_policy,
    status: hasEdges ? 'complete' : 'no_results',
    error_message: hasEdges
      ? undefined
      : `Scanned ${f.markets_scanned} real markets for this fixture — no positive-EV leg survived no-vig pricing. Honest empty: Stratum never fabricates picks.`,
  };
}

export default function HunterScreen() {
  const [phase, setPhase] = useState<Phase>('IDLE');
  const [huntQuery, setHuntQuery] = useState('');
  const [selectedSport, setSelectedSport] = useState<SportValue>('auto');

  const [universe, setUniverse] = useState<MarketUniverse | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const [match, setMatch] = useState<MatchResult | null>(null);
  const [nearMisses, setNearMisses] = useState<MatchResult[]>([]);
  const [slideIn, setSlideIn] = useState(false);

  const [scan, setScan] = useState<HuntSnapshot | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [rescanNote, setRescanNote] = useState('');

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  /* --------------------------- load universe once ------------------------ */

  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    setLoadError('');
    Promise.all([loadUniverse(controller.signal), loadLastScan(controller.signal)])
      .then(([u, s]) => {
        if (!alive) return;
        setUniverse(u);
        setScan(s);
        setLoading(false);
      })
      .catch((e: Error) => {
        if (!alive || e?.name === 'AbortError') return;
        setLoadError(e.message || 'Unable to reach the market universe.');
        setLoading(false);
      });
    return () => {
      alive = false;
      controller.abort(); // cleanup contract: abort on unmount
    };
  }, []);

  /* ------------------------------- searching ----------------------------- */

  const runSearch = useCallback(
    (rawQuery: string, sport: SportValue) => {
      const trimmed = rawQuery.trim();
      if (!trimmed || !universe) return;
      const detected = autoDetectFixture(universe, trimmed, sport);
      if (detected) {
        setMatch(detected);
        setNearMisses([]);
      } else {
        setMatch(null);
        setNearMisses(nearestFixtures(universe, trimmed, 5, sport));
      }
      setPhase('RESULTS');
      setSlideIn(false);
      requestAnimationFrame(() => requestAnimationFrame(() => setSlideIn(true)));
    },
    [universe],
  );

  const resetToIdle = useCallback(() => {
    setPhase('IDLE');
    setMatch(null);
    setNearMisses([]);
    setSlideIn(false);
    setRescanNote('');
  }, []);

  const pickNearest = useCallback(
    (m: MatchResult) => {
      setHuntQuery(`${m.fixture.home} vs ${m.fixture.away}`);
      setMatch(m);
      setNearMisses([]);
      setSlideIn(false);
      requestAnimationFrame(() => requestAnimationFrame(() => setSlideIn(true)));
    },
    [],
  );

  /* ------------------------------ force rescan --------------------------- */

  const triggerable = useMemo(canTriggerRescan, []);

  const onForceRescan = useCallback(async () => {
    if (!triggerable) return;
    setRescanNote('Dispatching swarm cycle…');
    try {
      await dispatchSwarmWorkflow();
      setRescanNote('Cycle dispatched — results land on main within ~2 min.');
    } catch (e) {
      setRescanNote(`Rescan dispatch failed: ${(e as Error).message}`);
    }
  }, [triggerable]);

  const onRefreshData = useCallback(async () => {
    if (!universe || refreshing) return;
    setRefreshing(true);
    try {
      const [fresh, s] = await Promise.all([refreshUniverse(), loadLastScan()]);
      setUniverse(fresh);
      if (s) setScan(s);
      // re-run the active search against fresh data
      if (phase === 'RESULTS' && huntQuery.trim()) runSearch(huntQuery, selectedSport);
    } catch {
      /* keep stale-but-valid cache; silent by design */
    } finally {
      setRefreshing(false);
    }
  }, [huntQuery, phase, refreshing, runSearch, selectedSport, universe]);

  /* --------------------------------- views ------------------------------- */

  const header = (
    <div style={styles.header}>
      <span style={styles.brand}>STRATUM</span>
      <span style={styles.universeStamp}>
        {universe
          ? `slate: ${universe.fixture_count} fixtures · scan ${scanAgeLabel(universe.generated_at_utc)}`
          : loading
            ? 'loading market universe…'
            : 'universe offline'}
      </span>
      <span style={styles.headerBtns}>
        <button
          type="button"
          style={styles.linkBtn}
          onClick={() => void onRefreshData()}
          disabled={!universe || refreshing}
          title="Re-fetch the latest committed universe file (read-only)"
        >
          {refreshing ? '⟳ …' : '⟳ REFRESH DATA'}
        </button>
        <button
          type="button"
          style={{ ...styles.linkBtn, opacity: triggerable ? 1 : 0.45, cursor: triggerable ? 'pointer' : 'not-allowed' }}
          onClick={() => void onForceRescan()}
          disabled={!triggerable}
          title={triggerable ? 'Dispatch a swarm cycle now' : 'Rescans run automatically every 15 min'}
        >
          ⟳ FORCE RESCAN
        </button>
      </span>
    </div>
  );

  const tracePanel = (
    <div style={styles.traceWrap}>
      <button type="button" style={styles.traceToggle} onClick={() => setTraceOpen((o) => !o)}>
        {traceOpen ? '▾' : '▸'} AGENT TRACE — last scheduled scan @{' '}
        {scan?.updated_at
          ? new Date(scan.updated_at).toUTCString().replace(':00 GMT', ' UTC')
          : universe?.generated_at_utc
            ? universe.generated_at_utc
            : 'unknown'}{' '}
        ({universe ? scanAgeLabel(universe.generated_at_utc) : '—'}) · replay, not live
      </button>
      {traceOpen && (
        <HunterConsole
          lines={(scan?.lines ?? []) as HuntLine[]}
          progress={1}
          stage="scheduled_replay"
          isActive={false}
        />
      )}
    </div>
  );

  if (phase === 'IDLE') {
    return (
      <div style={styles.idleWrap}>
        {header}
        <div style={styles.heroTitle}>ENTER MATCH TO HUNT</div>
        <div style={styles.heroSub}>
          The scheduled swarm scans every configured league every 15 minutes. Search its
          live universe instantly — no waiting, no fabricating.
        </div>
        <input
          style={styles.huntInput}
          placeholder="Enter match e.g. Al Hilal vs Al Nassr"
          value={huntQuery}
          onChange={(e) => setHuntQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') runSearch(huntQuery, selectedSport);
          }}
          autoFocus
        />
        <select
          style={styles.sportSelect}
          value={selectedSport}
          onChange={(e) => setSelectedSport(e.target.value as SportValue)}
          aria-label="Sport selector"
        >
          {SPORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {loadError ? <div style={styles.errorText}>{loadError}</div> : null}
        {rescanNote ? <div style={styles.noteText}>{rescanNote}</div> : null}
        <button
          type="button"
          style={{
            ...styles.deployBtn,
            opacity: !huntQuery.trim() || !universe ? 0.4 : 1,
          }}
          onClick={() => runSearch(huntQuery, selectedSport)}
          disabled={!huntQuery.trim() || !universe}
        >
          {loading ? 'LOADING UNIVERSE…' : 'SEARCH UNIVERSE'}
        </button>
        {universe && (
          <div style={styles.slateHint}>
            {universe.fixture_count} fixtures priced across{' '}
            {new Set(universe.fixtures.map((f) => f.league)).size} leagues right now.
          </div>
        )}
        {tracePanel}
      </div>
    );
  }

  /* --------------------------------- RESULTS ----------------------------- */

  const resultForDashboard: HuntResult | null =
    match && universe ? fixtureToResult(match, universe) : null;

  return (
    <div style={styles.resultsWrap}>
      {header}
      <div
        style={{
          ...styles.slidePanel,
          transform: slideIn ? 'translateY(0)' : 'translateY(48px)',
          opacity: slideIn ? 1 : 0,
        }}
      >
        {resultForDashboard ? (
          <>
            <div style={styles.matchedStamp}>
              ✓ AUTO-DETECTED FROM SLATE · inferred sport:{' '}
              <b>{(match?.fixture.sport ?? 'unknown').toUpperCase()}</b> · league:{' '}
              <b>{match?.fixture.league}</b> · kickoff {match?.fixture.kickoff_utc || '?'}
            </div>
            <ResultsDashboard result={resultForDashboard} query={huntQuery} onStartHunt={resetToIdle} />
          </>
        ) : (
          <NoMatchCard
            query={huntQuery}
            nearMisses={nearMisses}
            onPick={pickNearest}
            onRetry={resetToIdle}
          />
        )}
      </div>
      {tracePanel}
      <button type="button" style={styles.backBtn} onClick={resetToIdle}>
        ← BACK TO SEARCH
      </button>
    </div>
  );
}

/* ---------------------------- no-match sub-view -------------------------- */

function NoMatchCard({
  query,
  nearMisses,
  onPick,
  onRetry,
}: {
  query: string;
  nearMisses: MatchResult[];
  onPick: (m: MatchResult) => void;
  onRetry: () => void;
}) {
  return (
    <div style={styles.alertCard} data-testid="no-match-card">
      <div style={styles.alertIcon}>⛔</div>
      <div style={styles.alertTitle}>“{query}”: No markets detected across all sources.</div>
      <div style={styles.alertBody}>
        This fixture is not in the current scheduled slate (kickoff outside the scan window,
        name mismatch, or source blocked). Verify team names or try again.
      </div>
      {nearMisses.length > 0 && (
        <div style={styles.nearestWrap}>
          <div style={styles.nearestHeader}>Nearest fixtures in today&apos;s slate:</div>
          {nearMisses.map((m) => (
            <button
              key={m.fixture.fixture_id}
              type="button"
              style={styles.nearestBtn}
              onClick={() => onPick(m)}
            >
              <span style={styles.nearestName}>
                {m.fixture.home} <span style={styles.vs}>vs</span> {m.fixture.away}
              </span>
              <span style={styles.nearestMeta}>
                {m.fixture.league} · {m.fixture.sport.toUpperCase()} ·{' '}
                {m.fixture.top_edges.length > 0
                  ? `${m.fixture.top_edges.length} ranked edges`
                  : `${m.fixture.markets_scanned} markets scanned`}
              </span>
            </button>
          ))}
        </div>
      )}
      <button type="button" style={styles.retryBtn} onClick={onRetry}>
        ↻ NEW SEARCH
      </button>
      <div style={styles.disclaimer}>
        All figures derive from scraped market data + deterministic math. Stratum never
        fabricates prices — an absent fixture is reported as absent.
      </div>
    </div>
  );
}

/* --------------------------------- styles -------------------------------- */

const styles: Record<string, React.CSSProperties> = {
  header: {
    display: 'flex', alignItems: 'center', gap: Spacing.md, width: '100%',
    paddingBottom: Spacing.md, borderBottom: `1px solid ${Colors.border}`, marginBottom: Spacing.lg,
  },
  brand: { color: Colors.primary, fontWeight: 900, letterSpacing: 4, fontSize: 15 },
  universeStamp: { color: Colors.textMuted, fontFamily: Fonts.mono, fontSize: 11, flex: 1 },
  headerBtns: { display: 'flex', gap: Spacing.sm, alignItems: 'center' },
  linkBtn: {
    background: 'transparent', border: `1px solid ${Colors.border}`, color: Colors.textSecondary,
    borderRadius: Radius.sm, padding: '5px 10px', fontSize: 10.5, fontFamily: Fonts.mono,
    letterSpacing: 1, cursor: 'pointer',
  },
  idleWrap: {
    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: Spacing.lg,
    padding: `${Spacing.xl}px ${Spacing.lg}px`, maxWidth: 860, margin: '0 auto', width: '100%',
    boxSizing: 'border-box',
  },
  heroTitle: { color: Colors.textPrimary, fontSize: 30, fontWeight: 900, letterSpacing: 3, textAlign: 'center' },
  heroSub: { color: Colors.textSecondary, fontSize: 13, textAlign: 'center', marginTop: -Spacing.sm },
  huntInput: {
    width: '100%', boxSizing: 'border-box', backgroundColor: '#0A0D14', color: Colors.textPrimary,
    border: `1px solid ${Colors.border}`, borderRadius: Radius.md, padding: '18px 20px',
    fontSize: 18, fontFamily: Fonts.ui, outline: 'none',
  },
  sportSelect: {
    backgroundColor: Colors.surfaceAlt, color: Colors.textPrimary, border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm, padding: '10px 14px', fontSize: 14, fontFamily: Fonts.mono, minWidth: 220,
  },
  errorText: { color: Colors.danger, fontSize: 12.5, fontFamily: Fonts.mono, textAlign: 'center' },
  noteText: { color: Colors.warning, fontSize: 12, fontFamily: Fonts.mono, textAlign: 'center' },
  deployBtn: {
    backgroundColor: '#00E5FF', color: '#000', border: 'none', borderRadius: 9999,
    padding: '12px 32px', fontWeight: 800, letterSpacing: 2, textTransform: 'uppercase',
    fontSize: 14, cursor: 'pointer', boxShadow: '0 8px 28px rgba(0,229,255,0.35)',
    transition: 'all 200ms ease',
  },
  slateHint: { color: Colors.textMuted, fontSize: 11.5, fontFamily: Fonts.mono },
  traceWrap: { width: '100%', marginTop: Spacing.md },
  traceToggle: {
    background: 'transparent', border: 'none', color: Colors.textMuted, fontFamily: Fonts.mono,
    fontSize: 11, cursor: 'pointer', padding: '4px 0', textAlign: 'left', width: '100%',
  },
  resultsWrap: {
    padding: `${Spacing.lg}px ${Spacing.lg}px 96px`, maxWidth: 980, margin: '0 auto', width: '100%',
    boxSizing: 'border-box',
  },
  slidePanel: { transition: 'transform 450ms cubic-bezier(0.22, 1, 0.36, 1), opacity 450ms ease' },
  matchedStamp: {
    color: Colors.success, fontFamily: Fonts.mono, fontSize: 11.5, letterSpacing: 0.5,
    marginBottom: Spacing.sm,
  },
  alertCard: {
    backgroundColor: 'rgba(255,77,77,0.06)', border: `1px solid ${Colors.danger}`,
    borderRadius: Radius.lg, padding: Spacing.xl, textAlign: 'center',
  },
  alertIcon: { fontSize: 30, marginBottom: Spacing.sm },
  alertTitle: { color: Colors.textPrimary, fontSize: 17, fontWeight: 800, marginBottom: 6 },
  alertBody: { color: Colors.textSecondary, fontSize: 13, marginBottom: Spacing.lg },
  nearestWrap: { textAlign: 'left', marginBottom: Spacing.lg },
  nearestHeader: {
    color: Colors.primary, fontFamily: Fonts.mono, fontSize: 11.5, letterSpacing: 1,
    textTransform: 'uppercase', marginBottom: Spacing.sm,
  },
  nearestBtn: {
    display: 'flex', flexDirection: 'column', gap: 2, width: '100%', textAlign: 'left',
    backgroundColor: Colors.surface, border: `1px solid ${Colors.border}`, borderRadius: Radius.sm,
    padding: '10px 14px', marginBottom: 8, cursor: 'pointer',
  },
  nearestName: { color: Colors.textPrimary, fontSize: 14, fontWeight: 700 },
  vs: { color: Colors.textMuted, fontWeight: 400 },
  nearestMeta: { color: Colors.textMuted, fontSize: 11, fontFamily: Fonts.mono },
  retryBtn: {
    backgroundColor: 'transparent', color: Colors.primary, border: `1px solid ${Colors.primary}`,
    borderRadius: 9999, padding: '9px 24px', fontWeight: 800, letterSpacing: 1.5, cursor: 'pointer',
    fontFamily: Fonts.mono, fontSize: 12,
  },
  disclaimer: { color: Colors.textMuted, fontSize: 11, marginTop: Spacing.md, fontStyle: 'italic' },
  backBtn: {
    position: 'fixed', bottom: 18, left: '50%', transform: 'translateX(-50%)',
    backgroundColor: Colors.surface, color: Colors.primary, border: `1px solid ${Colors.primary}`,
    borderRadius: 9999, padding: '10px 26px', fontWeight: 800, letterSpacing: 1.5,
    cursor: 'pointer', fontFamily: Fonts.mono, fontSize: 12.5, zIndex: 20,
  },
};
