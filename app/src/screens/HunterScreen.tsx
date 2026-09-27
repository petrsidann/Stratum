import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import OddsCard from '../components/OddsCard';
import { Colors, Fonts, Radius, Spacing, evColor, formatPercent } from '../theme/colors';
import { HuntResult, runHunt } from '../utils/hunterClient';
import { TopEdge } from '../utils/apiClient';

/**
 * HUNTER MODE -- interactive, query-scoped terminal.
 *
 * State machine (the whole app lives here now; no cron feeds, no empty
 * data on load):
 *
 *   IDLE    -> big search input + sport dropdown + cyan [ START HUNT ]
 *   LOADING -> animated progress bar cycling source status messages,
 *              button disabled
 *   RESULTS -> dashboard (top picks table + edge chart + per-edge cards)
 *              populated ONLY by the current hunt session
 *   ERROR   -> red alert "No markets detected for '[Query]'..." + retry
 */

type Phase = 'IDLE' | 'LOADING' | 'RESULTS' | 'ERROR';

const SPORT_OPTIONS = [
  { value: 'soccer', label: 'Soccer ⚽' },
  { value: 'basketball', label: 'Basketball 🏀' },
  { value: 'tennis', label: 'Tennis 🎾' },
  { value: 'auto', label: 'Auto-detect 🛰️' },
] as const;

type SportValue = (typeof SPORT_OPTIONS)[number]['value'];

const STAGES = [
  'Connecting to Betika...',
  'Parsing Odibets...',
  'Scanning Flashscore...',
  'Calculating Fair Odds...',
  'Detecting Steam...',
];

// Progress bar animation speed: stages advance on a timer purely for UX
// pacing; the real completion signal is the hunt promise resolving.
const STAGE_INTERVAL_MS = 1600;

export default function HunterScreen() {
  const [phase, setPhase] = useState<Phase>('IDLE');
  const [query, setQuery] = useState('');
  const [sport, setSport] = useState<SportValue>('soccer');
  const [stageIdx, setStageIdx] = useState(0);
  const [result, setResult] = useState<HuntResult | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const stageTimerRef = useRef<number | null>(null);

  const stopStageTimer = () => {
    if (stageTimerRef.current !== null) {
      window.clearInterval(stageTimerRef.current);
      stageTimerRef.current = null;
    }
  };

  useEffect(() => stopStageTimer, []);

  const startHunt = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed || phase === 'LOADING') return;

    setPhase('LOADING');
    setStageIdx(0);
    setResult(null);
    setErrorMsg('');

    stageTimerRef.current = window.setInterval(() => {
      // Cycle through statuses and hold at the final stage until done.
      setStageIdx((i) => Math.min(i + 1, STAGES.length - 1));
    }, STAGE_INTERVAL_MS);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await runHunt(trimmed, sport, controller.signal);
      stopStageTimer();
      if (controller.signal.aborted) return;

      const edges = res.edges ?? [];
      if (res.status === 'success' && edges.length > 0) {
        setResult(res);
        setPhase('RESULTS');
      } else {
        // No fabricated fallbacks: zero real markets == honest error state.
        setErrorMsg(
          res.message ||
            `No markets detected for '${trimmed}'. Try exact team names.`
        );
        setPhase('ERROR');
      }
    } catch (err) {
      stopStageTimer();
      if (controller.signal.aborted) return;
      setErrorMsg(
        err instanceof Error && /timeout|abort/i.test(err.message)
          ? `Hunt timed out for '${trimmed}'. Sources may be unreachable. Retry.`
          : `Network failure during hunt: ${
              err instanceof Error ? err.message : 'unknown error'
            }. Retry.`
      );
      setPhase('ERROR');
    } finally {
      abortRef.current = null;
    }
  }, [query, sport, phase]);

  const resetToIdle = useCallback(() => {
    abortRef.current?.abort();
    stopStageTimer();
    setPhase('IDLE');
    setResult(null);
    setErrorMsg('');
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') startHunt();
  };

  return (
    <div style={styles.screen}>
      {/* Command bar is always present so you can re-hunt from any state. */}
      <div style={styles.commandBar}>
        <span style={styles.prompt}>{'❯'} HUNTER://</span>
        <input
          style={styles.input}
          placeholder="Match name — e.g. Al Hilal vs Al Nassr"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={phase === 'LOADING'}
          spellCheck={false}
          autoFocus
        />
        <select
          style={styles.select}
          value={sport}
          onChange={(e) => setSport(e.target.value as SportValue)}
          disabled={phase === 'LOADING'}
        >
          {SPORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value} style={{ background: Colors.surface }}>
              {o.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          style={phase === 'LOADING' ? styles.huntBtnDisabled : styles.huntBtn}
          onClick={startHunt}
          disabled={phase === 'LOADING' || query.trim().length === 0}
        >
          {phase === 'LOADING' ? 'HUNTING...' : '[ START HUNT ]'}
        </button>
      </div>

      {phase === 'IDLE' && <IdleHero />}
      {phase === 'LOADING' && <LoadingView stage={STAGES[stageIdx]} pct={stageIdx / (STAGES.length - 1)} />}
      {phase === 'RESULTS' && result && <ResultsView result={result} onNewHunt={resetToIdle} />}
      {phase === 'ERROR' && <ErrorView message={errorMsg} onRetry={startHunt} onEdit={resetToIdle} />}
    </div>
  );
}

/* ------------------------------- IDLE ---------------------------------- */

function IdleHero() {
  return (
    <div style={styles.center}>
      <div style={styles.heroTitle}>HUNTER MODE</div>
      <div style={styles.heroSub}>
        Real-time scan of ONE match across Betika · Odibets · Flashscore.
        <br />
        Vig removal → fair odds → edge detection. No cached data. No cron.
      </div>
      <div style={styles.heroHint}>Type a fixture above and hit [ START HUNT ] ⏎</div>
    </div>
  );
}

/* ------------------------------ LOADING --------------------------------- */

function LoadingView({ stage, pct }: { stage: string; pct: number }) {
  return (
    <div style={styles.center}>
      <div style={styles.loadBox}>
        <div style={styles.loadStatus}>{stage}</div>
        <div style={styles.track}>
          <div
            style={{
              ...styles.fill,
              width: `${Math.max(6, Math.round(pct * 100))}%`,
            }}
          />
        </div>
        <div style={styles.loadMeta}>SCANNING · TIMEOUT GUARD 10s/SOURCE · RATE LIMIT 1s/DOMAIN</div>
      </div>
    </div>
  );
}

/* ------------------------------ RESULTS --------------------------------- */

function ResultsView({ result, onNewHunt }: { result: HuntResult; onNewHunt: () => void }) {
  const edges = useMemo(
    () => [...(result.edges ?? [])].sort((a, b) => b.edge_percent - a.edge_percent),
    [result]
  );
  const maxEdge = Math.max(...edges.map((e) => Math.abs(e.edge_percent)), 1);

  return (
    <div style={styles.resultsWrap}>
      <div style={styles.metaStrip}>
        <div style={styles.metaLine}>
          HUNT “{result.query}” · {result.sport.toUpperCase()} ·{' '}
          {result.markets_scanned} MARKETS SCANNED · {edges.length} EDGES
        </div>
        <div style={styles.metaLineDim}>
          {Object.entries(result.sources ?? {})
            .map(([k, v]) => `${k}:${v}`)
            .join(' | ')}{' '}
          · {result.generated_at?.replace('T', ' ').replace('Z', '')} UTC
        </div>
        <button type="button" style={styles.newHuntLink} onClick={onNewHunt}>
          ✕ NEW HUNT
        </button>
      </div>

      {/* Top-5 picks table sorted by Edge % */}
      <div style={styles.panel}>
        <div style={styles.panelTitle}>TOP PICKS — SORTED BY EDGE %</div>
        <table style={styles.table}>
          <thead>
            <tr style={styles.thRow}>
              <th style={{ ...styles.th, textAlign: 'left' }}>#</th>
              <th style={{ ...styles.th, textAlign: 'left' }}>MARKET / SELECTION</th>
              <th style={styles.th}>BEST BOOK</th>
              <th style={styles.th}>ODDS</th>
              <th style={styles.th}>FAIR %</th>
              <th style={styles.th}>EDGE %</th>
              <th style={styles.th}>KELLY</th>
            </tr>
          </thead>
          <tbody>
            {edges.map((e, i) => (
              <tr key={`${e.market_type}-${e.selection}-${i}`} style={styles.tr}>
                <td style={{ ...styles.td, textAlign: 'left', color: Colors.textMuted }}>{i + 1}</td>
                <td style={{ ...styles.td, textAlign: 'left' }}>
                  <span style={styles.tdMarket}>{e.market_name}</span>{' '}
                  <span style={styles.tdSel}>{e.selection}</span>
                </td>
                <td style={styles.td}>{e.best_bookmaker}</td>
                <td style={styles.td}>{e.best_price.toFixed(2)}</td>
                <td style={styles.td}>{formatPercent(e.fair_probability, 1)}</td>
                <td style={{ ...styles.td, color: evColor(e.edge_percent), fontWeight: 700 }}>
                  {e.edge_percent >= 0 ? '+' : ''}
                  {formatPercent(e.edge_percent, 2)}
                </td>
                <td style={styles.td}>{formatPercent(e.kelly_stake * 100, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Edge bar chart (pure CSS, no chart lib needed for N<=5) */}
      <div style={styles.panel}>
        <div style={styles.panelTitle}>EDGE DISTRIBUTION</div>
        {edges.map((e, i) => (
          <div key={`bar-${i}`} style={styles.barRow}>
            <div style={styles.barLabel}>
              {e.selection.slice(0, 14)} <span style={styles.barLabelDim}>· {e.market_type}</span>
            </div>
            <div style={styles.barTrack}>
              <div
                style={{
                  ...styles.barFill,
                  width: `${(Math.abs(e.edge_percent) / maxEdge) * 100}%`,
                  backgroundColor: evColor(e.edge_percent),
                }}
              />
            </div>
            <div style={{ ...styles.barVal, color: evColor(e.edge_percent) }}>
              {e.edge_percent >= 0 ? '+' : ''}
              {formatPercent(e.edge_percent, 2)}
            </div>
          </div>
        ))}
      </div>

      {/* Full cards (reuse existing component contract) */}
      <div style={styles.panel}>
        <div style={styles.panelTitle}>SIGNAL DETAIL</div>
        {edges.map((e, i) => (
          <OddsCard key={`card-${i}`} edge={toTopEdge(e)} />
        ))}
      </div>
    </div>
  );
}

function toTopEdge(e: HuntResult['edges'][number]): TopEdge {
  return {
    match_id: e.match_id,
    sport: e.sport,
    icon: e.icon,
    home_team: e.home_team,
    away_team: e.away_team,
    commence: e.commence,
    market_type: e.market_type,
    market_name: e.market_name,
    line: e.line,
    selection: e.selection,
    best_bookmaker: e.best_bookmaker,
    best_price: e.best_price,
    fair_probability: e.fair_probability,
    raw_probability: e.raw_probability,
    ev_percent: e.edge_percent,
    kelly_stake: e.kelly_stake,
    confidence: e.confidence,
    signals: e.signals ?? [],
  };
}

/* ------------------------------- ERROR ---------------------------------- */

function ErrorView({ message, onRetry, onEdit }: { message: string; onRetry: () => void; onEdit: () => void }) {
  return (
    <div style={styles.center}>
      <div style={styles.alertBox}>
        <div style={styles.alertTitle}>⛔ NO MARKETS DETECTED</div>
        <p style={styles.alertBody}>{message}</p>
        <div style={styles.alertActions}>
          <button type="button" style={styles.retryBtn} onClick={onRetry}>
            ↻ RETRY HUNT
          </button>
          <button type="button" style={styles.editBtn} onClick={onEdit}>
            EDIT QUERY
          </button>
        </div>
        <div style={styles.alertFootnote}>
          The engine never fabricates odds — an empty scan means the sources
          genuinely returned nothing for this query.
        </div>
      </div>
    </div>
  );
}

/* ------------------------------- STYLES --------------------------------- */

const styles: Record<string, React.CSSProperties> = {
  screen: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    backgroundColor: Colors.background,
    minHeight: '100vh',
  },
  commandBar: {
    display: 'flex',
    gap: Spacing.sm,
    alignItems: 'center',
    padding: Spacing.lg,
    borderBottom: `1px solid ${Colors.border}`,
    backgroundColor: Colors.surface,
    flexWrap: 'wrap',
  },
  prompt: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 14, fontWeight: 700 },
  input: {
    flex: '1 1 260px',
    minWidth: 220,
    backgroundColor: Colors.background,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.md,
    color: Colors.textPrimary,
    fontFamily: Fonts.mono,
    fontSize: 15,
    padding: '14px 16px',
    outline: 'none',
  },
  select: {
    backgroundColor: Colors.background,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.md,
    color: Colors.textPrimary,
    fontFamily: Fonts.mono,
    fontSize: 12,
    padding: '13px 10px',
    cursor: 'pointer',
  },
  huntBtn: {
    backgroundColor: Colors.primary,
    color: '#04141A',
    border: 'none',
    borderRadius: Radius.md,
    fontFamily: Fonts.mono,
    fontSize: 13,
    fontWeight: 800,
    letterSpacing: 1,
    padding: '14px 22px',
    cursor: 'pointer',
    boxShadow: `0 0 18px ${Colors.primary}55`,
  },
  huntBtnDisabled: {
    backgroundColor: Colors.border,
    color: Colors.textMuted,
    border: 'none',
    borderRadius: Radius.md,
    fontFamily: Fonts.mono,
    fontSize: 13,
    fontWeight: 800,
    letterSpacing: 1,
    padding: '14px 22px',
    cursor: 'not-allowed',
  },
  center: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    padding: Spacing.xl,
  },
  heroTitle: {
    color: Colors.primary,
    fontFamily: Fonts.mono,
    fontSize: 34,
    fontWeight: 900,
    letterSpacing: 6,
    textShadow: `0 0 24px ${Colors.primary}66`,
  },
  heroSub: {
    marginTop: Spacing.md,
    color: Colors.textSecondary,
    fontFamily: Fonts.ui,
    fontSize: 13,
    lineHeight: 1.7,
    textAlign: 'center',
  },
  heroHint: {
    marginTop: Spacing.xl,
    color: Colors.textMuted,
    fontFamily: Fonts.mono,
    fontSize: 11,
    letterSpacing: 1,
  },
  loadBox: { width: '100%', maxWidth: 460 },
  loadStatus: {
    color: Colors.primary,
    fontFamily: Fonts.mono,
    fontSize: 14,
    letterSpacing: 1,
    marginBottom: Spacing.md,
  },
  track: {
    height: 8,
    backgroundColor: Colors.surfaceAlt,
    border: `1px solid ${Colors.border}`,
    borderRadius: 999,
    overflow: 'hidden',
  },
  fill: {
    height: '100%',
    background: `linear-gradient(90deg, ${Colors.primary}, ${Colors.success})`,
    transition: 'width 1.4s ease',
    borderRadius: 999,
  },
  loadMeta: {
    marginTop: Spacing.md,
    color: Colors.textMuted,
    fontFamily: Fonts.mono,
    fontSize: 10,
    letterSpacing: 1,
  },
  resultsWrap: {
    flex: 1,
    width: '100%',
    maxWidth: 760,
    margin: '0 auto',
    padding: `0 ${Spacing.lg}px ${Spacing.xl}px`,
  },
  metaStrip: { position: 'relative', padding: `${Spacing.md}px 0` },
  metaLine: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 11, letterSpacing: 0.5 },
  metaLineDim: { color: Colors.textMuted, fontFamily: Fonts.mono, fontSize: 10, marginTop: 4 },
  newHuntLink: {
    position: 'absolute',
    right: 0,
    top: Spacing.md,
    background: 'none',
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    color: Colors.danger,
    fontFamily: Fonts.mono,
    fontSize: 10,
    fontWeight: 700,
    padding: '4px 8px',
    cursor: 'pointer',
  },
  panel: {
    backgroundColor: Colors.surface,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.md,
    padding: Spacing.lg,
    marginTop: Spacing.md,
  },
  panelTitle: {
    color: Colors.primary,
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: 2,
    marginBottom: Spacing.md,
  },
  table: { width: '100%', borderCollapse: 'collapse' },
  thRow: { borderBottom: `1px solid ${Colors.border}` },
  th: {
    color: Colors.textMuted,
    fontFamily: Fonts.mono,
    fontSize: 9,
    letterSpacing: 1,
    textAlign: 'right' as const,
    padding: '6px 8px',
    fontWeight: 600,
  },
  tr: { borderBottom: `1px solid ${Colors.gridLine}` },
  td: {
    color: Colors.textPrimary,
    fontFamily: Fonts.mono,
    fontSize: 11,
    textAlign: 'right' as const,
    padding: '8px',
  },
  tdMarket: { color: Colors.textSecondary },
  tdSel: { color: Colors.textPrimary, fontWeight: 700 },
  barRow: {
    display: 'grid',
    gridTemplateColumns: '130px 1fr 70px',
    gap: Spacing.sm,
    alignItems: 'center',
    marginBottom: Spacing.sm,
  },
  barLabel: { color: Colors.textPrimary, fontFamily: Fonts.mono, fontSize: 10 },
  barLabelDim: { color: Colors.textMuted },
  barTrack: {
    height: 10,
    backgroundColor: Colors.surfaceAlt,
    borderRadius: 999,
    overflow: 'hidden',
  },
  barFill: { height: '100%', borderRadius: 999, transition: 'width 0.6s ease' },
  barVal: { fontFamily: Fonts.mono, fontSize: 10, textAlign: 'right' as const, fontWeight: 700 },
  alertBox: {
    width: '100%',
    maxWidth: 520,
    backgroundColor: '#2A1418',
    border: `1px solid ${Colors.danger}`,
    borderRadius: Radius.md,
    padding: Spacing.xl,
    textAlign: 'center',
    boxShadow: `0 0 30px ${Colors.danger}33`,
  },
  alertTitle: {
    color: Colors.danger,
    fontFamily: Fonts.mono,
    fontSize: 16,
    fontWeight: 900,
    letterSpacing: 2,
  },
  alertBody: {
    color: Colors.textPrimary,
    fontFamily: Fonts.ui,
    fontSize: 13,
    lineHeight: 1.6,
    marginTop: Spacing.md,
  },
  alertActions: {
    display: 'flex',
    gap: Spacing.sm,
    justifyContent: 'center',
    marginTop: Spacing.lg,
  },
  retryBtn: {
    backgroundColor: Colors.danger,
    color: '#1A0407',
    border: 'none',
    borderRadius: Radius.sm,
    fontFamily: Fonts.mono,
    fontSize: 12,
    fontWeight: 800,
    padding: '10px 18px',
    cursor: 'pointer',
  },
  editBtn: {
    backgroundColor: 'transparent',
    color: Colors.textSecondary,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    fontFamily: Fonts.mono,
    fontSize: 12,
    fontWeight: 700,
    padding: '10px 18px',
    cursor: 'pointer',
  },
  alertFootnote: {
    marginTop: Spacing.lg,
    color: Colors.textMuted,
    fontFamily: Fonts.ui,
    fontSize: 10,
    lineHeight: 1.5,
  },
};
