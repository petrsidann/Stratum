import React, { useEffect, useRef } from 'react';
import { HuntLine } from '../lib/swarmClient';
import { Colors, Fonts } from '../theme/colors';

/**
 * STRATUM V3.0 — HunterConsole: the live swarm terminal.
 *
 * Renders the streaming agent log fed by the polling layer (swarmClient):
 *   - fixed-height scrollable console, auto-scrolls to newest line
 *   - color-coded per agent: SCOUT=#38BDF8 · ACTUARY=#2EE6A6 ·
 *     CONTEXT=#FACC15 · STRATEGIST=#00E5FF (SWARM orchestrator = dim violet)
 *   - header strip: pulsing status dot + [STAGE] label + smooth progress bar
 *   - muted "Awaiting deployment order…" placeholder when idle & empty
 *
 * Zero external deps beyond React; Tailwind-style class names are carried as
 * data attributes for future styling, but layout uses inline styles because
 * this repo's design system is style-object based (see theme/colors.ts).
 */

export interface HunterConsoleProps {
  lines: HuntLine[];
  progress: number; // 0..1
  stage: string;
  isActive: boolean;
}

const AGENT_COLORS: Record<HuntLine['agent'], string> = {
  SCOUT: '#38BDF8', // blue
  ACTUARY: '#2EE6A6', // green
  CONTEXT: '#FACC15', // yellow
  STRATEGIST: '#00E5FF', // cyan
  SWARM: '#A78BFA', // orchestrator chatter (violet, secondary)
};

const TS_COLOR = '#8B9BB4'; // dim grey timestamps

function hhmmss(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function HunterConsole({ lines, progress, stage, isActive }: HunterConsoleProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom whenever a new line streams in.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  const pct = Math.min(Math.max(progress || 0, 0), 1) * 100;
  const showPlaceholder = !isActive && lines.length === 0;

  return (
    <div
      data-testid="hunter-console"
      style={{
        display: 'flex',
        flexDirection: 'column',
        backgroundColor: '#0A0D14',
        border: `1px solid ${Colors.border}`,
        borderRadius: 12,
        overflow: 'hidden',
        boxShadow: '0 0 40px rgba(0, 229, 255, 0.06)',
      }}
    >
      {/* ---- top bar: pulse dot + stage + progress ---------------------- */}
      <div style={styles.topBar}>
        <span
          aria-hidden
          className={isActive ? 'stratum-pulse' : undefined}
          style={{
            ...styles.dot,
            backgroundColor: isActive ? Colors.primary : Colors.textMuted,
            animation: isActive ? 'stratumPulse 1.2s ease-in-out infinite' : 'none',
          }}
        />
        <span style={styles.stageLabel}>[{(stage || (isActive ? 'BOOTING' : 'IDLE')).toUpperCase()}]</span>
        <span style={styles.progressWrap}>
          <span
            style={{
              ...styles.progressFill,
              width: `${pct}%`,
            }}
          />
        </span>
        <span style={styles.progressPct}>{Math.round(pct)}%</span>
      </div>

      {/* ---- log stream -------------------------------------------------- */}
      <div ref={scrollRef} data-testid="hunter-console-stream" style={styles.stream}>
        {showPlaceholder ? (
          <div style={styles.awaiting}>Awaiting deployment order…</div>
        ) : (
          lines.map((line, i) => {
            const color = AGENT_COLORS[line.agent] ?? Colors.textPrimary;
            return (
              <div key={i} style={styles.logRow} data-agent={line.agent}>
                <span style={styles.timestamp}>{hhmmss(line.ts)}</span>
                <span style={{ ...styles.agentTag, color }}>[{line.agent}]</span>
                <span style={styles.logText}>{line.text}</span>
              </div>
            );
          })
        )}
        {isActive && lines.length > 0 && <div style={styles.cursor}>▍</div>}
      </div>

      {/* keyframes for the pulse dot (no tailwind dependency available) */}
      <style>{`
        @keyframes stratumPulse {
          0%   { box-shadow: 0 0 0 0 rgba(0,229,255,0.55); }
          70%  { box-shadow: 0 0 0 8px rgba(0,229,255,0); }
          100% { box-shadow: 0 0 0 0 rgba(0,229,255,0); }
        }
        @keyframes stratumBlink { 0%,49% {opacity:1;} 50%,100% {opacity:0;} }
      `}</style>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  topBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '10px 14px',
    borderBottom: `1px solid ${Colors.border}`,
    backgroundColor: Colors.surfaceAlt,
  },
  dot: {
    width: 9,
    height: 9,
    borderRadius: '50%',
    flexShrink: 0,
  },
  stageLabel: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    letterSpacing: 1.5,
    color: Colors.primary,
    fontWeight: 700,
    whiteSpace: 'nowrap',
  },
  progressWrap: {
    position: 'relative',
    flex: 1,
    height: 6,
    borderRadius: 3,
    backgroundColor: '#141824',
    border: `1px solid ${Colors.border}`,
    overflow: 'hidden',
  },
  progressFill: {
    position: 'absolute',
    inset: 0,
    right: 'auto',
    borderRadius: 3,
    background: 'linear-gradient(90deg, #38BDF8 0%, #00E5FF 60%, #2EE6A6 100%)',
    transition: 'width 700ms cubic-bezier(0.22, 1, 0.36, 1)',
  },
  progressPct: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    color: Colors.textSecondary,
    minWidth: 36,
    textAlign: 'right',
  },
  stream: {
    height: '60vh',
    maxHeight: '60vh',
    overflowY: 'auto',
    padding: '12px 14px',
    fontFamily: Fonts.mono,
    fontSize: 12.5,
    lineHeight: 1.7,
    scrollbarWidth: 'thin' as const,
  },
  awaiting: {
    height: '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: Colors.textMuted,
    fontStyle: 'italic',
    letterSpacing: 0.5,
  },
  logRow: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 8,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },
  timestamp: {
    color: TS_COLOR,
    fontFamily: Fonts.mono,
    fontSize: 10.5,
    flexShrink: 0,
    fontVariantNumeric: 'tabular-nums',
  },
  agentTag: {
    fontFamily: Fonts.mono,
    fontWeight: 700,
    fontSize: 11,
    letterSpacing: 0.5,
    flexShrink: 0,
    minWidth: 92,
  },
  logText: {
    color: '#FFFFFF',
    fontFamily: Fonts.ui,
    fontSize: 12.5,
  },
  cursor: {
    color: Colors.primary,
    animation: 'stratumBlink 1s step-end infinite',
    marginTop: 2,
  },
};
