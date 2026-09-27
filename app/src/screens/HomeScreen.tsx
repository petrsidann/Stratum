import React, { useCallback, useMemo, useState } from 'react';
import OddsCard from '../components/OddsCard';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';
import { fetchMarketFeed, MarketFeed, TopEdge } from '../utils/apiClient';
import { loadSettings } from '../utils/settings';

const SPORT_FILTERS = ['ALL', 'NBA', 'NFL', 'MLB', 'NHL', 'SOCCER'] as const;
type SportFilter = (typeof SPORT_FILTERS)[number];

/**
 * Home dashboard: top edges sorted by confidence (>= 80% preferred).
 * Falls back transparently to the best available signals and renders
 * "No Signal" when live data is absent. Never shows fabricated rows.
 */
export default function HomeScreen() {
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState<SportFilter>('ALL');

  const load = useCallback(async (force = false) => {
    if (!force) {
      fetchMarketFeed(false).then((data) => {
        setFeed(data);
        setLoading(false);
      });
      return;
    }
    setRefreshing(true);
    const data = await fetchMarketFeed(true);
    setFeed(data);
    setRefreshing(false);
    setLoading(false);
  }, []);

  React.useEffect(() => {
    load(false);
  }, [load]);

  const edges = useMemo<TopEdge[]>(() => {
    if (!feed) return [];
    const settings = loadSettings();
    let list = feed.top_edges ?? [];
    const highConf = list.filter((e) => e.confidence >= 80);
    if (highConf.length === 0) {
      // Transparent fallback: show what exists, still ranked by confidence.
      list = list.slice(0, 50);
    } else {
      list = highConf;
    }
    if (filter !== 'ALL') {
      list = list.filter((e) => {
        const sport = (e.sport || '').toUpperCase();
        if (filter === 'SOCCER') {
          return (
            /soccer|league|serie|liga|bundesliga|ligue|ucl|champions/i.test(sport) &&
            !/NBA|NFL|MLB|NHL/.test(sport)
          );
        }
        return sport.includes(filter);
      });
    }
    // Respect per-sport filters configured in Settings.
    const enabled = settings.enabledSports.map((s) => s.toUpperCase());
    if (enabled.length > 0 && enabled.length < 10) {
      list = list.filter((e) => {
        const sport = (e.sport || '').toUpperCase();
        return enabled.some((s) => sport.includes(s.split(' ')[0]));
      });
    }
    return list.sort((a, b) => b.confidence - a.confidence || b.ev_percent - a.ev_percent);
  }, [feed, filter]);

  if (loading) {
    return (
      <div style={styles.center}>
        <div style={styles.spinner} />
        <div style={styles.loadingText}>SYNCING LIVE MARKET FEED...</div>
      </div>
    );
  }

  const meta = feed?.meta;
  const degraded = !!meta?.error;

  return (
    <div style={styles.container}>
      <div style={styles.statusBar}>
        <div style={{ flex: 1 }}>
          <div style={styles.statusText}>
            ENGINE v{meta?.version ?? '--'} | {meta?.matches_scanned ?? 0} MATCHES |{' '}
            {meta?.markets_scanned ?? 0} MARKETS | {meta?.signals_found ?? 0} SIGNALS
          </div>
          <div style={{ ...styles.statusText, color: degraded ? Colors.warning : Colors.textSecondary }}>
            {degraded
              ? `FEED DEGRADED (${meta?.error})`
              : `UPDATED ${meta?.generated_at?.replace('T', ' ').replace('Z', '') ?? '--'} UTC`}
          </div>
        </div>
        <button type="button" style={styles.linkButton} onClick={() => load(true)}>
          {refreshing ? 'REFRESHING...' : 'REFRESH'}
        </button>
      </div>

      <div style={styles.filterRow}>
        {SPORT_FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            style={{
              ...styles.filterChip,
              ...(filter === f ? styles.filterChipActive : null),
            }}
          >
            {f}
          </button>
        ))}
      </div>

      <div style={styles.listArea}>
        {edges.length === 0 ? (
          <div style={styles.emptyBox}>
            <div style={styles.emptyTitle}>NO SIGNAL</div>
            <p style={styles.emptyBody}>
              No live market data available right now. The engine only displays real
              fetched odds; nothing is simulated. Tap REFRESH or wait for the next
              15-minute cron cycle.
            </p>
          </div>
        ) : (
          edges.map((edge, idx) => (
            <OddsCard
              key={`${edge.match_id}-${edge.market_type}-${edge.selection}-${idx}`}
              edge={edge}
            />
          ))
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: { flex: 1, display: 'flex', flexDirection: 'column', backgroundColor: Colors.background, minHeight: '100%' },
  center: {
    flex: 1,
    minHeight: '60vh',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: Colors.background,
  },
  spinner: {
    width: 32,
    height: 32,
    border: `3px solid ${Colors.border}`,
    borderTopColor: Colors.primary,
    borderRadius: '50%',
    animation: 'stratum-spin 0.9s linear infinite',
  },
  loadingText: {
    color: Colors.textSecondary,
    fontFamily: Fonts.mono,
    fontSize: 11,
    marginTop: Spacing.md,
    letterSpacing: 1,
  },
  statusBar: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: `${Spacing.sm}px ${Spacing.lg}px`,
    backgroundColor: Colors.surfaceAlt,
    borderBottom: `1px solid ${Colors.border}`,
  },
  statusText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 10 },
  linkButton: {
    background: 'none',
    border: 'none',
    color: Colors.primary,
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: 700,
    cursor: 'pointer',
    padding: 4,
  },
  filterRow: { display: 'flex', gap: 6, padding: Spacing.lg, flexWrap: 'wrap' },
  filterChip: {
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    padding: '4px 10px',
    backgroundColor: Colors.surface,
    color: Colors.textSecondary,
    fontFamily: Fonts.mono,
    fontSize: 10,
    cursor: 'pointer',
  },
  filterChipActive: { borderColor: Colors.primary, backgroundColor: '#0B2A33', color: Colors.primary, fontWeight: 700 },
  listArea: { padding: `0 ${Spacing.lg}px`, maxWidth: 720, width: '100%', margin: '0 auto', boxSizing: 'border-box' },
  emptyBox: { padding: Spacing.xl, textAlign: 'center' },
  emptyTitle: { color: Colors.danger, fontFamily: Fonts.mono, fontSize: 18, fontWeight: 700, letterSpacing: 2 },
  emptyBody: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 12, lineHeight: 1.6 },
};
