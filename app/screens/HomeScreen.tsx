import { useFocusEffect, useNavigation } from '@react-navigation/native';
import { NativeStackNavigationProp } from '@react-navigation/native-stack';
import React, { useCallback, useMemo, useState } from 'react';
import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import OddsCard from '../components/OddsCard';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';
import { fetchMarketFeed, MarketFeed, TopEdge } from '../utils/apiClient';
import { RootStackParamList } from '../App';

type Nav = NativeStackNavigationProp<RootStackParamList>;

const SPORT_FILTERS = ['ALL', 'NBA', 'NFL', 'MLB', 'NHL', 'SOCCER'] as const;
type SportFilter = (typeof SPORT_FILTERS)[number];

/**
 * Home dashboard: top edges with confidence >= 80%, sorted by confidence.
 * Falls back to showing best available signals when none clear the bar,
 * and renders "No Signal" when live data is absent. Never shows fake rows.
 */
export default function HomeScreen() {
  const navigation = useNavigation<Nav>();
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [filter, setFilter] = useState<SportFilter>('ALL');

  const load = useCallback(async (force = false) => {
    try {
      const data = await fetchMarketFeed(force);
      setFeed(data);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      load(false);
    }, [load])
  );

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    load(true);
  }, [load]);

  const edges = useMemo<TopEdge[]>(() => {
    if (!feed) return [];
    let list = feed.top_edges ?? [];
    const highConf = list.filter((e) => e.confidence >= 80);
    if (highConf.length === 0) {
      // Transparent fallback: show what exists, still ranked by confidence.
      highConf.push(...list.slice(0, 50));
    }
    if (filter !== 'ALL') {
      highConf.forEach(() => {});
      list = highConf.filter((e) =>
        filter === 'SOCCER'
          ? /league|soccer|serie|liga|bundesliga|ligue/i.test(e.sport)
          : e.sport.toUpperCase().includes(filter)
      );
    } else {
      list = highConf;
    }
    return list.sort((a, b) => b.confidence - a.confidence || b.ev_percent - a.ev_percent);
  }, [feed, filter]);

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator color={Colors.primary} size="large" />
        <Text style={styles.loadingText}>SYNCING LIVE MARKET FEED...</Text>
      </View>
    );
  }

  const meta = feed?.meta;
  const stale = meta?.error && meta.error !== undefined;

  return (
    <View style={styles.container}>
      <View style={styles.statusBar}>
        <View style={styles.statusLeft}>
          <Text style={styles.statusText}>
            ENGINE v{meta?.version ?? '--'} | {meta?.matches_scanned ?? 0} MATCHES |{' '}
            {meta?.markets_scanned ?? 0} MARKETS
          </Text>
          <Text style={[styles.statusText, stale && styles.staleText]}>
            {stale
              ? `FEED DEGRADED (${meta?.error})`
              : `UPDATED ${meta?.generated_at?.replace('T', ' ').replace('Z', '') ?? '--'} UTC`}
          </Text>
        </View>
        <TouchableOpacity onPress={() => navigation.navigate('Settings')}>
          <Text style={styles.settingsLink}>SETTINGS</Text>
        </TouchableOpacity>
      </View>

      <View style={styles.filterRow}>
        {SPORT_FILTERS.map((f) => (
          <TouchableOpacity
            key={f}
            onPress={() => setFilter(f)}
            style={[styles.filterChip, filter === f && styles.filterChipActive]}
          >
            <Text style={[styles.filterText, filter === f && styles.filterTextActive]}>{f}</Text>
          </TouchableOpacity>
        ))}
      </View>

      <FlatList
        data={edges}
        keyExtractor={(item, idx) => `${item.match_id}-${item.market_type}-${item.selection}-${idx}`}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={Colors.primary} />
        }
        ListEmptyComponent={
          <View style={styles.emptyBox}>
            <Text style={styles.emptyTitle}>NO SIGNAL</Text>
            <Text style={styles.emptyBody}>
              No live market data available right now. The engine only displays real
              fetched odds; nothing is simulated. Pull to refresh or wait for the next
              15-minute cron cycle.
            </Text>
          </View>
        }
        renderItem={({ item }) => (
          <OddsCard
            edge={item}
            onPress={() => navigation.navigate('MatchDetail', { matchId: item.match_id })}
          />
        )}
        contentContainerStyle={{ paddingBottom: 32 }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: Colors.background },
  center: { flex: 1, backgroundColor: Colors.background, alignItems: 'center', justifyContent: 'center' },
  loadingText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 11, marginTop: Spacing.md, letterSpacing: 1 },
  statusBar: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: Spacing.lg,
    paddingVertical: Spacing.sm,
    backgroundColor: Colors.surfaceAlt,
    borderBottomWidth: 1,
    borderBottomColor: Colors.border,
  },
  statusLeft: { flex: 1 },
  statusText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 10 },
  staleText: { color: Colors.warning },
  settingsLink: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 11, fontWeight: '700' },
  filterRow: {
    flexDirection: 'row',
    gap: 6,
    paddingHorizontal: Spacing.lg,
    paddingVertical: Spacing.md,
  },
  filterChip: {
    borderWidth: 1,
    borderColor: Colors.border,
    borderRadius: Radius.sm,
    paddingHorizontal: 10,
    paddingVertical: 4,
    backgroundColor: Colors.surface,
  },
  filterChipActive: { borderColor: Colors.primary, backgroundColor: '#0B2A33' },
  filterText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 10 },
  filterTextActive: { color: Colors.primary, fontWeight: '700' },
  emptyBox: { padding: Spacing.xl, alignItems: 'center' },
  emptyTitle: { color: Colors.danger, fontFamily: Fonts.mono, fontSize: 18, fontWeight: '700', letterSpacing: 2 },
  emptyBody: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 12, textAlign: 'center', marginTop: Spacing.md, lineHeight: 18 },
});
