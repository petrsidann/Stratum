import React from 'react';
import { useNavigate } from 'react-router-dom';
import { MarketFeed, TopEdge, fetchMarketFeed } from '../utils/apiClient';
import {
  Colors,
  Fonts,
  Radius,
  Spacing,
  confidenceColor,
  evColor,
  formatAmericanOdds,
  formatPercent,
} from '../theme/colors';

const SPORT_GLYPHS: Record<string, string> = {
  basketball: '[NBA]',
  football: '[NFL]',
  baseball: '[MLB]',
  soccer: '[FC]',
  hockey: '[NHL]',
};

interface Props {
  edge: TopEdge;
}

/**
 * Single market opportunity card. Color-coded EV, fair vs best odds,
 * Kelly stake, confidence pill and signal chips. Dense terminal-style layout.
 */
export default function OddsCard({ edge }: Props) {
  const navigate = useNavigate();

  // Hunter Mode reuses this card for hunt-scoped rows whose match_id does
  // not exist in the cron feed; navigating there would render an empty
  // detail screen. Only navigate when the match is actually present.
  const openMatch = async () => {
    const feed: MarketFeed = await fetchMarketFeed(false);
    if ((feed.matches ?? []).some((m) => m.id === edge.match_id)) {
      navigate(`/match/${encodeURIComponent(edge.match_id)}`);
    }
  };
  const ev = edge.ev_percent;
  const color = evColor(ev);
  const confColor = confidenceColor(edge.confidence);
  const stakeUnits = (edge.kelly_stake * 100).toFixed(1);
  const glyph =
    SPORT_GLYPHS[edge.icon] ?? `[${(edge.sport || '??').slice(0, 3).toUpperCase()}]`;

  return (
    <button
      type="button"
      onClick={openMatch}
      style={styles.card}
    >
      <div style={styles.headerRow}>
        <span style={{ ...styles.sportGlyph, color: Colors.primary }}>{glyph}</span>
        <span style={styles.teams}>
          {edge.away_team} @ {edge.home_team}
        </span>
        <span style={{ ...styles.confidencePill, borderColor: confColor, color: confColor }}>
          {formatPercent(edge.confidence, 0)}
        </span>
      </div>

      <div style={styles.marketRow}>
        <span style={styles.marketName}>
          {edge.market_name}
          {edge.line !== null && edge.line !== undefined ? ` ${edge.line}` : ''}
        </span>
        <span style={styles.selection}>{edge.selection}</span>
      </div>

      <div style={styles.numbersRow}>
        <NumberBlock
          label={`BEST (${(edge.best_bookmaker || '?').slice(0, 10)})`}
          value={formatAmericanOdds(edge.best_price)}
          valueColor={Colors.textPrimary}
        />
        <NumberBlock
          label="FAIR PROB"
          value={formatPercent(edge.fair_probability)}
          valueColor={Colors.primary}
        />
        <NumberBlock
          label="EV"
          value={`${ev > 0 ? '+' : ''}${formatPercent(ev, 2)}`}
          valueColor={color}
        />
        <NumberBlock label="KELLY" value={`${stakeUnits}u`} valueColor={Colors.warning} />
      </div>

      {edge.signals && edge.signals.length > 0 && (
        <div style={styles.signalRow}>
          {edge.signals.map((s) => (
            <span key={s} style={styles.signalChip}>
              {s}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}

function NumberBlock({
  label,
  value,
  valueColor,
}: {
  label: string;
  value: string;
  valueColor: string;
}) {
  return (
    <div style={styles.numberBlock}>
      <div style={styles.label}>{label}</div>
      <div style={{ ...styles.value, color: valueColor }}>{value}</div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  card: {
    display: 'block',
    width: '100%',
    textAlign: 'left',
    backgroundColor: Colors.surface,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.md,
    padding: Spacing.md,
    marginBottom: Spacing.md,
    cursor: 'pointer',
    color: Colors.textPrimary,
    fontFamily: Fonts.ui,
  },
  headerRow: {
    display: 'flex',
    alignItems: 'center',
    gap: Spacing.sm,
  },
  sportGlyph: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: 700,
  },
  teams: {
    flex: 1,
    color: Colors.textPrimary,
    fontFamily: Fonts.ui,
    fontWeight: 600,
    fontSize: 14,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  confidencePill: {
    border: '1px solid',
    borderRadius: Radius.sm,
    padding: '2px 6px',
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: 700,
    backgroundColor: 'transparent',
  },
  marketRow: {
    display: 'flex',
    justifyContent: 'space-between',
    marginTop: Spacing.xs,
  },
  marketName: { color: Colors.textSecondary, fontSize: 12 },
  selection: { color: Colors.textPrimary, fontWeight: 600, fontSize: 12 },
  numbersRow: {
    display: 'flex',
    marginTop: Spacing.sm,
    justifyContent: 'space-between',
    gap: Spacing.sm,
  },
  numberBlock: { flex: 1, minWidth: 0 },
  label: {
    color: Colors.textMuted,
    fontSize: 9,
    letterSpacing: 0.5,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  value: {
    fontFamily: Fonts.mono,
    fontSize: 14,
    fontWeight: 700,
    marginTop: 2,
  },
  signalRow: {
    display: 'flex',
    gap: 6,
    marginTop: Spacing.sm,
    flexWrap: 'wrap',
  },
  signalChip: {
    backgroundColor: Colors.surfaceAlt,
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    padding: '2px 6px',
    color: Colors.accentPink,
    fontFamily: Fonts.mono,
    fontSize: 9,
    fontWeight: 700,
    letterSpacing: 0.5,
  },
};
