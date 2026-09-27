import React, { useState } from 'react';
import { HuntResult, TopEdge } from '../lib/swarmClient';
import { Colors, Fonts, Radius, Spacing, confidenceColor, evColor } from '../theme/colors';

/**
 * STRATUM V3.0 — ResultsDashboard: post-hunt view shown once the console
 * fades out (slide-up handled by HunterScreen).
 *
 *  - null result OR status==='no_results' → honest red alert + retry button.
 *    (Backend integrity rule: empty top_edges is a VALID outcome — we never
 *    render invented numbers.)
 *  - success → header stats, ranked edge table (sorted desc by EV × Conf),
 *    Matplotlib report images from data/hunts/{timestamp}/, disclaimer.
 */

export interface ResultsDashboardProps {
  result: HuntResult | null;
  query: string;
  /** Optional retry hook wired to DEPLOY HUNTERS again (from HunterScreen). */
  onStartHunt?: () => void;
}

/** Rank score = EV% × confidence (the same key the strategist sorts on). */
function rankScore(e: TopEdge): number {
  return (e.ev_percent || 0) * (e.confidence_score || 0);
}

function fmt(n: number | null | undefined, digits = 2, suffix = ''): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toFixed(digits) + suffix;
}

/**
 * Site payload base for report PNGs committed under data/hunts/{timestamp}/
 * by deploy-swarm.yml. The runner archives with the exact UTC timestamp of
 * completion (e.g. "2026-09-27T04:28:28Z" -> "20260927T042828Z").
 */
function huntAssetBase(result: HuntResult): string {
  const iso = result.timestamp_utc || '';
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/.exec(iso);
  if (!m) return '/data/hunts/latest';
  const [y, mo, d, h, mi, sec] = m.slice(1);
  return `/data/hunts/${y}${mo}${d}T${h}${mi}${sec}Z`;
}

export default function ResultsDashboard({ result, query, onStartHunt }: ResultsDashboardProps) {
  const [hoverIdx, setHoverIdx] = useState<number>(-1);

  /* ---------------------------- failure / empty case ------------------- */
  const isEmpty = !result || result.status === 'no_results' || result.top_edges.length === 0;
  if (isEmpty) {
    const detail = result?.error_message ?? '';
    return (
      <div style={styles.alertCard} data-testid="results-empty">
        <div style={styles.alertIcon}>⛔</div>
        <div style={styles.alertTitle}>
          {query || result?.query || 'Match'}: No markets detected across all sources.
        </div>
        <div style={styles.alertBody}>Verify team names or try again.</div>
        {detail ? <div style={styles.alertDetail}>{detail}</div> : null}
        {onStartHunt && (
          <button type="button" style={styles.retryBtn} onClick={onStartHunt}>
            ↻ RETRY HUNT
          </button>
        )}
        <div style={styles.disclaimer}>
          Stratum reports honest empty results — it never fabricates prices to fill this screen.
        </div>
      </div>
    );
  }

  /* ------------------------------ success case ------------------------- */
  const edges = [...result.top_edges].sort((a, b) => rankScore(b) - rankScore(a));
  const avgConf = edges.reduce((s, e) => s + (e.confidence_score || 0), 0) / Math.max(edges.length, 1);
  const degraded = (result.agents_failed?.length ?? 0) > 0 || result.context_available === false;

  return (
    <div style={styles.wrap} data-testid="results-dashboard">
      {/* header row */}
      <div style={styles.headerRow}>
        <span style={styles.matchName}>{result.query || query}</span>
        <span style={styles.chip}>{(result.sport || 'auto').toUpperCase()}</span>
        <span style={styles.chip}>{result.markets_scanned_count} markets scanned</span>
        <span style={{ ...styles.chip, color: confidenceColor(avgConf), borderColor: confidenceColor(avgConf) }}>
          AVG CONF {Math.round(avgConf)}%
        </span>
        {degraded && <span style={{ ...styles.chip, color: Colors.warning }}>MATH-ONLY · CONF PENALIZED</span>}
      </div>

      {/* ranked edge table */}
      <div style={styles.tableScroll}>
        <table style={styles.table}>
          <thead>
            <tr>
              {['#', 'Market', 'Selection', 'Book Odds', 'Fair Odds', 'EV%', 'Conf', 'Kelly %'].map((h) => (
                <th key={h} style={{ ...styles.th, textAlign: h === 'Market' || h === 'Selection' ? 'left' : 'right' }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {edges.map((e, i) => {
              const best = i === 0;
              const hovered = hoverIdx === i;
              return (
                <tr
                  key={`${e.market}-${e.selection}-${i}`}
                  onMouseEnter={() => setHoverIdx(i)}
                  onMouseLeave={() => setHoverIdx(-1)}
                  style={{
                    ...styles.td_row,
                    backgroundColor: hovered ? 'rgba(0,229,255,0.06)' : best ? 'rgba(46,230,166,0.05)' : 'transparent',
                    boxShadow: best ? `inset 3px 0 0 ${Colors.success}` : 'none',
                  }}
                >
                  <td style={{ ...styles.td, color: Colors.textMuted }}>{best ? '★' : i + 1}</td>
                  <td style={{ ...styles.td, maxWidth: 260 }}>
                    {e.market}
                    <div style={styles.reasoning}>{e.reasoning_summary}</div>
                  </td>
                  <td style={{ ...styles.td, color: Colors.textPrimary, fontWeight: 600 }}>{e.selection}</td>
                  <td style={{ ...styles.td, ...styles.num }}>{fmt(e.book_odds)}</td>
                  <td style={{ ...styles.td, ...styles.num, color: Colors.textSecondary }}>{fmt(e.fair_odds)}</td>
                  <td style={{ ...styles.td, ...styles.num, color: evColor(e.ev_percent), fontWeight: 700 }}>
                    {fmt(e.ev_percent, 1, '%')}
                  </td>
                  <td style={{ ...styles.td, ...styles.num, color: confidenceColor(e.confidence_score) }}>
                    {Math.round(e.confidence_score)}
                  </td>
                  <td style={{ ...styles.td, ...styles.num }}>{fmt(e.kelly_stake_pct, 1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* visual reports (Matplotlib PNGs committed under data/hunts/{ts}/) */}
      {result.visual_reports_png.length > 0 && (
        <div style={styles.vizGrid}>
          {result.visual_reports_png.map((png) => {
            const name = png.split('/').pop() ?? png;
            return (
              <figure key={png} style={styles.figure}>
                <img src={`${huntAssetBase(result)}/${name}`} alt={name} style={styles.img} loading="lazy" />
                <figcaption style={styles.caption}>{name.replace(/\.png$/i, '').toUpperCase()}</figcaption>
              </figure>
            );
          })}
        </div>
      )}

      <div style={styles.disclaimer}>
        All figures derived from scraped market data + deterministic math. Stratum never fabricates prices.
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  wrap: { display: 'flex', flexDirection: 'column', gap: Spacing.lg },
  headerRow: { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: Spacing.sm },
  matchName: { color: Colors.textPrimary, fontSize: 20, fontWeight: 800, letterSpacing: 0.3 },
  chip: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    color: Colors.textSecondary,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    padding: '3px 8px',
    letterSpacing: 0.5,
  },
  tableScroll: { overflowX: 'auto', border: `1px solid ${Colors.border}`, borderRadius: Radius.md },
  table: { width: '100%', borderCollapse: 'collapse', minWidth: 720 },
  th: {
    fontFamily: Fonts.mono,
    fontSize: 10.5,
    letterSpacing: 1,
    color: Colors.textMuted,
    textTransform: 'uppercase',
    padding: '10px 12px',
    borderBottom: `1px solid ${Colors.border}`,
    backgroundColor: Colors.surfaceAlt,
    whiteSpace: 'nowrap',
  },
  td_row: { transition: 'background-color 150ms ease' },
  td: {
    padding: '10px 12px',
    borderBottom: `1px solid ${Colors.gridLine}`,
    color: Colors.textPrimary,
    fontSize: 13,
    verticalAlign: 'top',
  },
  num: { fontFamily: Fonts.mono, fontVariantNumeric: 'tabular-nums', textAlign: 'right', whiteSpace: 'nowrap' },
  reasoning: { color: Colors.textSecondary, fontSize: 11, marginTop: 3, lineHeight: 1.45 },
  vizGrid: { display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: Spacing.lg },
  figure: { position: 'relative', margin: 0, border: `1px solid ${Colors.border}`, borderRadius: Radius.md, overflow: 'hidden' },
  img: { width: '100%', display: 'block', backgroundColor: '#fff' },
  caption: {
    position: 'absolute',
    left: 8,
    bottom: 8,
    fontFamily: Fonts.mono,
    fontSize: 10,
    letterSpacing: 1,
    color: Colors.textPrimary,
    backgroundColor: 'rgba(15,17,26,0.75)',
    padding: '2px 8px',
    borderRadius: Radius.sm,
  },
  disclaimer: { color: Colors.textMuted, fontSize: 11.5, fontStyle: 'italic', lineHeight: 1.5 },

  /* empty-state alert */
  alertCard: {
    border: `1px solid ${Colors.danger}`,
    borderRadius: Radius.lg,
    backgroundColor: 'rgba(255,77,77,0.06)',
    padding: Spacing.xl,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: Spacing.md,
    textAlign: 'center',
  },
  alertIcon: { fontSize: 30 },
  alertTitle: { color: Colors.danger, fontWeight: 800, fontSize: 16 },
  alertBody: { color: Colors.textSecondary, fontSize: 13 },
  alertDetail: {
    color: Colors.textMuted,
    fontSize: 12,
    fontFamily: Fonts.mono,
    maxWidth: 560,
    lineHeight: 1.5,
  },
  retryBtn: {
    marginTop: Spacing.sm,
    backgroundColor: Colors.primary,
    color: '#000',
    border: 'none',
    borderRadius: 9999,
    padding: '10px 28px',
    fontWeight: 800,
    letterSpacing: 1.5,
    cursor: 'pointer',
    textTransform: 'uppercase',
  },
};
