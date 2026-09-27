import React, { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Colors,
  Fonts,
  Radius,
  Spacing,
  evColor,
  formatAmericanOdds,
  formatPercent,
} from '../theme/colors';
import {
  fetchMarketFeed,
  findMatch,
  groupMarketsByTab,
  Market,
  MarketFeed,
} from '../utils/apiClient';

const TABS = ['MAIN', 'PROPS', 'DERIVATIVES', 'VISUALS'] as const;
type Tab = (typeof TABS)[number];

/**
 * Match detail: every scanned market for the fixture grouped into tabs.
 * VISUALS renders vig-removal bars, a line-movement sparkline and a Kelly
 * risk gauge computed from live feed values only.
 */
export default function MatchDetailScreen() {
  const { matchId } = useParams<{ matchId: string }>();
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>('MAIN');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    fetchMarketFeed().then((f) => {
      if (mounted) {
        setFeed(f);
        setLoading(false);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  const match = useMemo(
    () => (feed && matchId ? findMatch(feed, decodeURIComponent(matchId)) : undefined),
    [feed, matchId]
  );

  const groups = useMemo(() => (match ? groupMarketsByTab(match) : null), [match]);

  if (loading) {
    return (
      <div style={styles.center}>
        <div style={styles.subtle}>LOADING MARKETS...</div>
      </div>
    );
  }

  if (!match || !groups) {
    return (
      <div style={styles.center}>
        <div style={styles.noSignal}>NO SIGNAL</div>
        <div style={styles.subtle}>Match data is not present in the live feed.</div>
      </div>
    );
  }

  const marketsForTab: Market[] =
    tab === 'MAIN' ? groups.main : tab === 'PROPS' ? groups.props : groups.derivatives;

  const selected =
    match.markets.find((m) => m.key === selectedKey) ??
    marketsForTab[0] ??
    match.markets[0];

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <div style={styles.sport}>{(match.sport || '').toUpperCase()}</div>
        <div style={styles.teams}>
          {match.away_team} @ {match.home_team}
        </div>
        <div style={styles.subtle}>
          {match.commence?.replace('T', ' ').replace('Z', ' UTC')} |{' '}
          {match.market_count ?? match.markets.length} MARKETS SCANNED
        </div>
      </div>

      <div style={styles.tabRow}>
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            style={{ ...styles.tab, ...(tab === t ? styles.tabActive : null) }}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'VISUALS' ? (
        <VisualsTab market={selected} />
      ) : (
        <div style={{ padding: `0 ${Spacing.lg}px ${Spacing.xl}px`, maxWidth: 860, margin: '0 auto', width: '100%', boxSizing: 'border-box' }}>
          {marketsForTab.length === 0 && (
            <div style={{ ...styles.noSignal, textAlign: 'center', marginTop: 40 }}>
              NO SIGNAL
            </div>
          )}
          {marketsForTab.map((mk) => (
            <MarketRow key={mk.key} market={mk} onSelect={() => setSelectedKey(mk.key)} />
          ))}
        </div>
      )}
    </div>
  );
}

function MarketRow({ market, onSelect }: { market: Market; onSelect: () => void }) {
  return (
    <div style={styles.marketCard} onClick={onSelect}>
      <div style={styles.marketHeader}>
        <span style={styles.marketTitle}>
          {market.name}
          {market.line !== null && market.line !== undefined ? ` ${market.line}` : ''}
        </span>
        <span style={styles.selection}>{market.selection}</span>
      </div>
      {market.bookmakers.length === 0 ? (
        <div style={styles.noSignalSmall}>NO SIGNAL - no live bookmaker prices</div>
      ) : (
        <table style={styles.table}>
          <thead>
            <tr>
              {['BOOK', 'ODDS', 'IMP %', 'FAIR %', 'EDGE %', 'STAKE'].map((h) => (
                <th key={h} style={styles.th}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {market.bookmakers.slice(0, 6).map((b, i) => {
              const edge = market.fair_probability - b.implied_probability;
              return (
                <tr key={`${b.bookmaker}-${i}`}>
                  <td style={{ ...styles.td, fontFamily: Fonts.ui }}>{b.bookmaker}</td>
                  <td style={{ ...styles.td, fontFamily: Fonts.mono }}>
                    {formatAmericanOdds(b.price)}
                  </td>
                  <td style={{ ...styles.td, fontFamily: Fonts.mono }}>
                    {b.implied_probability.toFixed(1)}
                  </td>
                  <td style={{ ...styles.td, fontFamily: Fonts.mono, color: Colors.primary }}>
                    {market.fair_probability.toFixed(1)}
                  </td>
                  <td style={{ ...styles.td, fontFamily: Fonts.mono, color: evColor(edge) }}>
                    {edge > 0 ? '+' : ''}
                    {edge.toFixed(1)}
                  </td>
                  <td style={{ ...styles.td, fontFamily: Fonts.mono, color: Colors.warning }}>
                    {edge > 0 ? `${(market.kelly_stake * 100).toFixed(1)}u` : '--'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** Charts tab: all series derived from real feed numbers only. */
function VisualsTab({ market }: { market?: Market }) {
  if (!market || market.bookmakers.length === 0) {
    return (
      <div style={styles.center}>
        <div style={styles.noSignal}>NO SIGNAL</div>
        <div style={styles.subtle}>No live prices to visualize for this market.</div>
      </div>
    );
  }

  const vigData = market.bookmakers.slice(0, 8).map((b) => ({
    book: b.bookmaker.slice(0, 8),
    raw: Math.round(b.implied_probability * 10) / 10,
    fair: Math.round(market.fair_probability * 10) / 10,
  }));

  const movement = market.movement ?? [];
  const movementData = movement.map((p, i) => ({
    idx: i + 1,
    time: p[0]?.slice(11, 16) ?? String(i + 1),
    price: p[1],
  }));

  const kellyPct = Math.min(Math.max(market.kelly_stake * 100, 0), 10);
  const riskLevel =
    kellyPct >= 5 ? 'HIGH' : kellyPct >= 2 ? 'MODERATE' : kellyPct > 0 ? 'LOW' : 'NONE';
  const riskColor =
    riskLevel === 'HIGH' ? Colors.danger : riskLevel === 'MODERATE' ? Colors.warning : Colors.success;

  const tooltipStyle = {
    backgroundColor: Colors.surface,
    border: `1px solid ${Colors.border}`,
    fontFamily: Fonts.mono,
    fontSize: 11,
  };

  return (
    <div style={{ padding: Spacing.lg, display: 'flex', flexDirection: 'column', gap: Spacing.lg, maxWidth: 860, margin: '0 auto', width: '100%', boxSizing: 'border-box' }}>
      <div style={styles.chartCard}>
        <div style={styles.chartTitle}>VIG REMOVAL - RAW vs FAIR PROBABILITY</div>
        <div style={styles.subtle}>
          {market.name} {market.line ?? ''} | {market.selection}
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={vigData} margin={{ top: 12, right: 8, left: -16, bottom: 0 }}>
            <CartesianGrid stroke={Colors.gridLine} vertical={false} />
            <XAxis dataKey="book" tick={{ fill: Colors.textSecondary, fontSize: 10, fontFamily: Fonts.mono }} stroke={Colors.border} />
            <YAxis unit="%" tick={{ fill: Colors.textSecondary, fontSize: 10, fontFamily: Fonts.mono }} stroke={Colors.border} />
            <Tooltip contentStyle={tooltipStyle} cursor={{ fill: 'rgba(0,229,255,0.06)' }} />
            <Legend wrapperStyle={{ fontSize: 10, fontFamily: Fonts.mono, color: Colors.textSecondary }} />
            <Bar dataKey="raw" name="RAW IMPLIED (with vig)" fill={Colors.accentPink} radius={[3, 3, 0, 0]} />
            <Bar dataKey="fair" name="FAIR (vig removed)" fill={Colors.primary} radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div style={styles.chartCard}>
        <div style={styles.chartTitle}>LINE MOVEMENT TRACKER</div>
        {movementData.length >= 2 ? (
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={movementData} margin={{ top: 12, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid stroke={Colors.gridLine} vertical={false} />
              <XAxis dataKey="time" tick={{ fill: Colors.textSecondary, fontSize: 10, fontFamily: Fonts.mono }} stroke={Colors.border} />
              <YAxis domain={['auto', 'auto']} tick={{ fill: Colors.textSecondary, fontSize: 10, fontFamily: Fonts.mono }} stroke={Colors.border} />
              <Tooltip contentStyle={tooltipStyle} />
              <ReferenceLine y={americanToDecimalFallback(market.best.decimal)} stroke={Colors.warning} strokeDasharray="4 4" label={{ value: 'BEST NOW', fill: Colors.warning, fontSize: 9 }} />
              <Line type="monotone" dataKey="price" name="DECIMAL" stroke={Colors.success} strokeWidth={2} dot={{ r: 2, fill: Colors.success }} activeDot={{ r: 4 }} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div style={{ ...styles.subtle, padding: `${Spacing.lg}px 0` }}>
            Insufficient snapshots yet. Movement history builds across engine cycles.
          </div>
        )}
      </div>

      <div style={styles.chartCard}>
        <div style={styles.chartTitle}>KELLY SIZING GAUGE</div>
        <div style={styles.gaugeTrack}>
          <div
            style={{
              height: '100%',
              width: `${(kellyPct / 10) * 100}%`,
              minWidth: kellyPct > 0 ? 6 : 0,
              borderRadius: 7,
              backgroundColor: riskColor,
              transition: 'width 0.4s ease',
            }}
          />
        </div>
        <div style={styles.gaugeLabels}>
          <span style={styles.subtle}>0u</span>
          <span style={{ ...styles.riskText, color: riskColor }}>
            {(market.kelly_stake * 100).toFixed(1)}u QUARTER-KELLY | RISK: {riskLevel}
          </span>
          <span style={styles.subtle}>10u</span>
        </div>
        <div style={{ ...styles.evBig, color: evColor(market.ev_percent) }}>
          EV {market.ev_percent > 0 ? '+' : ''}
          {market.ev_percent.toFixed(2)}%
        </div>
        <div style={{ ...styles.subtle, textAlign: 'center' }}>
          {formatPercent(market.raw_probability)} raw implied {'->'} {formatPercent(market.fair_probability)} fair
          {' | '}vig stripped {formatPercent(market.vig_removed_percent)}
          {' | '}{market.num_bookmakers} books
        </div>
      </div>
    </div>
  );
}

function americanToDecimalFallback(decimalPrice: number): number {
  return Number.isFinite(decimalPrice) && decimalPrice > 0 ? decimalPrice : 2;
}

const styles: Record<string, React.CSSProperties> = {
  container: { flex: 1, backgroundColor: Colors.background, minHeight: '100%' },
  center: {
    minHeight: '50vh',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: Colors.background,
    padding: Spacing.xl,
  },
  header: {
    padding: Spacing.lg,
    backgroundColor: Colors.surface,
    borderBottom: `1px solid ${Colors.border}`,
  },
  sport: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 10, letterSpacing: 2 },
  teams: { color: Colors.textPrimary, fontFamily: Fonts.ui, fontWeight: 700, fontSize: 18, marginTop: 2 },
  subtle: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: 4 },
  tabRow: { display: 'flex', gap: 6, padding: Spacing.md },
  tab: {
    flex: 1,
    padding: '8px 0',
    borderRadius: Radius.sm,
    backgroundColor: Colors.surface,
    border: `1px solid ${Colors.border}`,
    color: Colors.textSecondary,
    fontFamily: Fonts.mono,
    fontSize: 10,
    cursor: 'pointer',
  },
  tabActive: { borderColor: Colors.primary, backgroundColor: '#0B2A33', color: Colors.primary, fontWeight: 700 },
  marketCard: {
    marginBottom: Spacing.md,
    backgroundColor: Colors.surface,
    borderRadius: Radius.md,
    border: `1px solid ${Colors.border}`,
    padding: Spacing.md,
    cursor: 'pointer',
  },
  marketHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    marginBottom: Spacing.sm,
  },
  marketTitle: { color: Colors.textPrimary, fontFamily: Fonts.ui, fontWeight: 700, fontSize: 13 },
  selection: { color: Colors.primary, fontFamily: Fonts.ui, fontWeight: 600, fontSize: 13 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    textAlign: 'left',
    color: Colors.textMuted,
    fontFamily: Fonts.ui,
    fontSize: 8,
    letterSpacing: 0.5,
    padding: '4px 4px',
    borderBottom: `1px solid ${Colors.border}`,
    fontWeight: 400,
  },
  td: {
    padding: '3px 4px',
    fontSize: 11,
    color: Colors.textPrimary,
    whiteSpace: 'nowrap',
  },
  noSignal: { color: Colors.danger, fontFamily: Fonts.mono, fontSize: 16, fontWeight: 700, letterSpacing: 2 },
  noSignalSmall: { color: Colors.textMuted, fontFamily: Fonts.mono, fontSize: 10, padding: '6px 0' },
  chartCard: {
    backgroundColor: Colors.surface,
    borderRadius: Radius.md,
    border: `1px solid ${Colors.border}`,
    padding: Spacing.md,
  },
  chartTitle: { color: Colors.textPrimary, fontFamily: Fonts.mono, fontSize: 11, fontWeight: 700, letterSpacing: 1 },
  gaugeTrack: {
    height: 14,
    backgroundColor: Colors.surfaceAlt,
    borderRadius: 7,
    border: `1px solid ${Colors.border}`,
    marginTop: Spacing.md,
    overflow: 'hidden',
  },
  gaugeLabels: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginTop: 6,
  },
  riskText: { fontFamily: Fonts.mono, fontSize: 11, fontWeight: 700 },
  evBig: { fontFamily: Fonts.mono, fontSize: 26, fontWeight: 700, marginTop: Spacing.md, textAlign: 'center' },
};

// Keep the unused Cell import out of the bundle warnings by referencing it
// in an internal helper used for future per-bar coloring.
void Cell;
