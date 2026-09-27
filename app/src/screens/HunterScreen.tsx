import React, { useCallback, useEffect, useRef, useState } from 'react';
import HunterConsole from '../components/HunterConsole';
import ResultsDashboard from '../components/ResultsDashboard';
import {
  HuntLine,
  HuntResult,
  NetworkError,
  fetchStatus,
  startHunt,
} from '../lib/swarmClient';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';

/**
 * STRATUM V3.0 — HUNTER terminal screen (agent-swarm UX flow).
 *
 *   IDLE    -> "ENTER MATCH TO HUNT" input + sport select + [DEPLOY HUNTERS]
 *   CONSOLE -> live Hacker-movie log stream (HunterConsole) fed by polling
 *              data/hunt_status.json every ~1s; progress bar + stage label
 *   RESULTS -> console fades out, ResultsDashboard slides up on terminal
 *              status ('complete' | 'no_results' | 'error'); polling stops
 *
 * Cleanup contract: the poll interval + AbortController are torn down on
 * unmount and whenever we leave CONSOLE (cancel button / terminal state).
 */

type Phase = 'IDLE' | 'CONSOLE' | 'RESULTS';

const SPORT_OPTIONS = [
  { value: 'soccer', label: 'Soccer ⚽' },
  { value: 'nba', label: 'NBA 🏀' },
  { value: 'nfl', label: 'NFL 🏈' },
  { value: 'mlb', label: 'MLB ⚾' },
  { value: 'nhl', label: 'NHL 🏒' },
] as const;

type SportValue = (typeof SPORT_OPTIONS)[number]['value'];

const POLL_MS = 1000; // hunt_status.json is rewritten ~1/s by the runner
const STALL_TIMEOUT_MS = 90_000; // same watchdog budget as swarmClient.pollStatus

export default function HunterScreen() {
  const [phase, setPhase] = useState<Phase>('IDLE');
  const [huntQuery, setHuntQuery] = useState('');
  const [selectedSport, setSelectedSport] = useState<SportValue>('soccer');
  const [currentLines, setCurrentLines] = useState<HuntLine[]>([]);
  const [currentProgress, setCurrentProgress] = useState(0);
  const [currentStage, setCurrentStage] = useState('');
  const [finalResult, setFinalResult] = useState<HuntResult | null>(null);
  const [deployError, setDeployError] = useState('');
  const [slideIn, setSlideIn] = useState(false);

  const pollTimerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const phaseRef = useRef<Phase>(phase);
  phaseRef.current = phase;

  /* --------------------------- teardown helpers -------------------------- */

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  // Unmount safety net: clearInterval + abort().
  useEffect(() => stopPolling, [stopPolling]);

  /* ------------------------------ transitions ---------------------------- */

  const resetToIdle = useCallback(() => {
    stopPolling();
    setPhase('IDLE');
    setCurrentLines([]);
    setCurrentProgress(0);
    setCurrentStage('');
    setFinalResult(null);
    setDeployError('');
    setSlideIn(false);
  }, [stopPolling]);

  const enterResults = useCallback((result: HuntResult | null) => {
    stopPolling(); // terminal state -> stop polling immediately
    setFinalResult(result);
    setPhase('RESULTS');
    // trigger CSS slide-up on next frame
    requestAnimationFrame(() => requestAnimationFrame(() => setSlideIn(true)));
  }, [stopPolling]);

  /* -------------------------------- polling ------------------------------ */

  const beginPolling = useCallback(() => {
    stopPolling();
    const controller = new AbortController();
    abortRef.current = controller;
    const startedAt = Date.now();
    let sawLiveHunt = false;

    const tick = async () => {
      if (controller.signal.aborted || phaseRef.current !== 'CONSOLE') return;
      try {
        const snap = await fetchStatus(controller.signal);
        if (controller.signal.aborted) return;
        if (snap.status === 'running' || snap.status === 'complete' ||
            snap.status === 'no_results' || snap.status === 'error') {
          sawLiveHunt = true;
        }
        setCurrentLines(snap.lines);
        setCurrentProgress(snap.progress);
        setCurrentStage(snap.stage);

        if (snap.status === 'complete' || snap.status === 'no_results' || snap.status === 'error') {
          enterResults(snap.result ?? null);
          return;
        }
      } catch (e) {
        if ((e as Error)?.name === 'AbortError') return;
        // transient network blip while the file is being rewritten -> keep alive
      }
      if (Date.now() - startedAt > STALL_TIMEOUT_MS && !(sawLiveHunt)) {
        setDeployError('Hunt stalled or network unavailable.');
        enterResults({
          query: '', timestamp_utc: '', agents_executed: [], markets_scanned_count: 0,
          top_edges: [], visual_reports_png: [], status: 'error',
          error_message: 'Hunt stalled or network unavailable.',
        } as HuntResult);
      } else if (Date.now() - startedAt > STALL_TIMEOUT_MS) {
        setDeployError('Swarm exceeded the 90s watchdog.');
        enterResults(null);
      }
    };

    void tick();
    pollTimerRef.current = window.setInterval(() => void tick(), POLL_MS);
  }, [enterResults, stopPolling]);

  /* ------------------------------- deployment ---------------------------- */

  const deploy = useCallback(async () => {
    const trimmed = huntQuery.trim();
    if (!trimmed || phase === 'CONSOLE') return;
    setDeployError('');
    setCurrentLines([]);
    setCurrentProgress(0);
    setCurrentStage('booting');
    setFinalResult(null);
    setSlideIn(false);
    setPhase('CONSOLE');

    try {
      await startHunt(trimmed, selectedSport);
      beginPolling();
    } catch (e) {
      if (e instanceof NetworkError) {
        // Transport missing (no dev server / no CF worker configured): fall
        // back to pure status polling — a workflow_dispatch run in flight
        // still publishes data/hunt_status.json which we can stream.
        beginPolling();
      } else {
        setDeployError(e instanceof Error ? e.message : String(e));
        beginPolling();
      }
    }
  }, [beginPolling, huntQuery, phase, selectedSport]);

  const cancel = useCallback(() => {
    stopPolling();
    resetToIdle();
  }, [resetToIdle, stopPolling]);

  /* --------------------------------- views ------------------------------- */

  if (phase === 'IDLE') {
    return (
      <div style={styles.idleWrap}>
        <div style={styles.heroTitle}>ENTER MATCH TO HUNT</div>
        <div style={styles.heroSub}>
          The swarm deploys SCOUT → ACTUARY → CONTEXT → STRATEGIST against live market sources.
        </div>
        <input
          style={styles.huntInput}
          placeholder="Enter match e.g. Al Hilal vs Al Nassr"
          value={huntQuery}
          onChange={(e) => setHuntQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void deploy(); }}
          autoFocus
        />
        <select
          style={styles.sportSelect}
          value={selectedSport}
          onChange={(e) => setSelectedSport(e.target.value as SportValue)}
          aria-label="Sport selector"
        >
          {SPORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {deployError ? <div style={styles.errorText}>{deployError}</div> : null}
        <button type="button" style={styles.deployBtn} onClick={() => void deploy()} disabled={!huntQuery.trim()}>
          DEPLOY HUNTERS
        </button>
        <div style={{ height: 56 }} />
        <HunterConsole lines={[]} progress={0} stage="" isActive={false} />
      </div>
    );
  }

  if (phase === 'CONSOLE') {
    return (
      <div style={styles.consoleWrap}>
        <div style={styles.consoleHeader}>
          <span style={styles.consoleTarget}>
            TARGET :: <b>{huntQuery}</b> · {selectedSport.toUpperCase()}
          </span>
          <button type="button" style={styles.cancelBtn} onClick={cancel}>
            ✕ CANCEL
          </button>
        </div>
        <div style={styles.consoleBackdrop}>
          <HunterConsole lines={currentLines} progress={currentProgress} stage={currentStage} isActive />
        </div>
        <style>{`@keyframes stratumSpin { to { transform: rotate(360deg); } }`}</style>
        <div style={styles.spinnerRow}>
          <span style={styles.spinner} />
          <span style={styles.spinnerText}>swarm airborne — holding for terminal report…</span>
        </div>
      </div>
    );
  }

  /* RESULTS */
  return (
    <div style={styles.resultsWrap}>
      <div
        style={{
          ...styles.slidePanel,
          transform: slideIn ? 'translateY(0)' : 'translateY(48px)',
          opacity: slideIn ? 1 : 0,
        }}
      >
        <ResultsDashboard result={finalResult} query={huntQuery} onStartHunt={resetToIdle} />
      </div>
      <button type="button" style={styles.backBtn} onClick={resetToIdle}>
        ← BACK TO SEARCH
      </button>
    </div>
  );
}

/* --------------------------------- styles -------------------------------- */

const styles: Record<string, React.CSSProperties> = {
  idleWrap: {
    display: 'flex', flexDirection: 'column', alignItems: 'center',
    gap: Spacing.lg, padding: `${Spacing.xl}px ${Spacing.lg}px`, maxWidth: 860, margin: '0 auto', width: '100%',
  },
  heroTitle: {
    color: Colors.textPrimary, fontSize: 30, fontWeight: 900, letterSpacing: 3, textAlign: 'center',
  },
  heroSub: { color: Colors.textSecondary, fontSize: 13, textAlign: 'center', marginTop: -Spacing.sm },
  huntInput: {
    width: '100%', boxSizing: 'border-box', backgroundColor: '#0A0D14', color: Colors.textPrimary,
    border: `1px solid ${Colors.border}`, borderRadius: Radius.md, padding: '18px 20px',
    fontSize: 18, fontFamily: Fonts.ui, outline: 'none',
  },
  sportSelect: {
    backgroundColor: Colors.surfaceAlt, color: Colors.textPrimary, border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm, padding: '10px 14px', fontSize: 14, fontFamily: Fonts.mono, minWidth: 200,
  },
  errorText: { color: Colors.danger, fontSize: 12.5, fontFamily: Fonts.mono, textAlign: 'center' },
  deployBtn: {
    backgroundColor: '#00E5FF', color: '#000', border: 'none', borderRadius: 9999,
    padding: '12px 32px', fontWeight: 800, letterSpacing: 2, textTransform: 'uppercase',
    fontSize: 14, cursor: 'pointer', boxShadow: '0 8px 28px rgba(0,229,255,0.35)',
    transition: 'all 200ms ease',
  },
  consoleWrap: {
    position: 'relative', padding: Spacing.lg, maxWidth: 980, margin: '0 auto', width: '100%',
    boxSizing: 'border-box',
  },
  consoleHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: Spacing.md },
  consoleTarget: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 12.5, letterSpacing: 0.5 },
  cancelBtn: {
    backgroundColor: 'transparent', color: Colors.danger, border: `1px solid ${Colors.danger}`,
    borderRadius: Radius.sm, padding: '6px 14px', fontSize: 12, fontWeight: 700,
    letterSpacing: 1, cursor: 'pointer', fontFamily: Fonts.mono,
  },
  consoleBackdrop: {
    backdropFilter: 'blur(6px)', WebkitBackdropFilter: 'blur(6px)',
    backgroundColor: 'rgba(15,17,26,0.55)', borderRadius: 14, padding: 2,
  },
  spinnerRow: { display: 'flex', alignItems: 'center', gap: Spacing.sm, marginTop: Spacing.md },
  spinner: {
    width: 14, height: 14, borderRadius: '50%', border: `2px solid ${Colors.border}`,
    borderTopColor: Colors.primary, animation: 'stratumSpin 0.9s linear infinite', display: 'inline-block',
  },
  spinnerText: { color: Colors.textMuted, fontSize: 12, fontFamily: Fonts.mono, fontStyle: 'italic' },
  resultsWrap: {
    padding: `${Spacing.lg}px ${Spacing.lg}px 96px`, maxWidth: 980, margin: '0 auto', width: '100%',
    boxSizing: 'border-box',
  },
  slidePanel: {
    transition: 'transform 450ms cubic-bezier(0.22, 1, 0.36, 1), opacity 450ms ease',
  },
  backBtn: {
    position: 'fixed', bottom: 18, left: '50%', transform: 'translateX(-50%)',
    backgroundColor: Colors.surface, color: Colors.primary, border: `1px solid ${Colors.primary}`,
    borderRadius: 9999, padding: '10px 26px', fontWeight: 800, letterSpacing: 1.5,
    cursor: 'pointer', fontFamily: Fonts.mono, fontSize: 12.5, zIndex: 20,
  },
};
