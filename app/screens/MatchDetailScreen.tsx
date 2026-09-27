import { useFocusEffect, useNavigation, RouteProp } from '@react-navigation/native';
import React, { useCallback, useMemo, useState } from 'react';
import { ActivityIndicator, ScrollView, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { LineChart } from 'react-native-chart-kit';
import { Colors, Fonts, Radius, Spacing, evColor, formatAmericanOdds, formatPercent } from '../theme/colors';
import { fetchMarketFeed, findMatch, groupMarketsByTab, Market, MarketFeed } from '../utils/apiClient';

type Props = { route: RouteProp<{ params: { matchId: string } }, 'params'> };

const TABS = ['MAIN', 'PROPS', 'DERIVATIVES', 'VISUALS'] as const;
type Tab = (typeof TABS)[number];

/**
 * Match detail: every scanned market for the fixture grouped into tabs.
 * VISUALS renders vig-removal bars, a 2h line-movement sparkline and a
 * Kelly risk gauge computed from live feed values only.
 */
export default function MatchDetailScreen({ route }: Props) {
  const navigation = useNavigation();
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>('MAIN');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  useFocusEffect(
    useCallback(() => {
      fetchMarketFeed().then((f) => {
        setFeed(f);
        setLoading(false);
      });
    }, [])
  );

  const match = useMemo(
    () => (feed ? findMatch(feed, (route as any).params?.matchId ?? '') : undefined),
    [feed, route]
  );

  const groups = useMemo(() => (match ? groupMarketsByTab(match) : null), [match]);

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator color={Colors.primary} />
      </View>
    );
  }

  if (!match || !groups) {
    return (
      <View style={styles.center}>
        <Text style={styles.noSignal}>NO SIGNAL</Text>
        <Text style={styles.subtle}>Match data is not present in the live feed.</Text>
      </View>
    );
  }

  const marketsForTab: Market[] =
    tab === 'MAIN' ? groups.main : tab === 'PROPS' ? groups.props : groups.derivatives;

  const selected =
    match.markets.find((m) => m.key === selectedKey) ?? marketsForTab[0] ?? match.markets[0];

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.sport}>{match.sport.toUpperCase()}</Text>
        <Text style={styles.teams}>
          {match.away_team} @ {match.home_team}
        </Text>
        <Text style={styles.subtle}>
          {match.commence?.replace('T', ' ').replace('Z', ' UTC')} | {match.market_count ?? match.markets.length} MARKETS SCANNED
        </Text>
      </View>

      <View style={styles.tabRow}>
        {TABS.map((t) => (
          <TouchableOpacity key={t} onPress={() => setTab(t)} style={[styles.tab, tab === t && styles.tabActive]}>
            <Text style={[styles.tabText, tab === t && styles.tabTextActive]}>{t}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {tab === 'VISUALS' ? (
        <VisualsTab market={selected} screenWidth={360} />
      ) : (
        <ScrollView contentContainerStyle={{ paddingBottom: 40 }}>
          {marketsForTab.length === 0 && (
            <Text style={[styles.noSignal, { textAlign: 'center', marginTop: 40 }]}>NO SIGNAL</Text>
          )}
          {marketsForTab.map((mk) => (
            <TouchableOpacity key={mk.key} onPress={() => setSelectedKey(mk.key)} activeOpacity={0.7}>
              <View style={styles.marketCard}>
                <View style={styles.marketHeader}>
                  <Text style={styles.marketTitle}>
                    {mk.name}
                    {mk.line !== null && mk.line !== undefined ? ` ${mk.line}` : ''}
                  </Text>
                  <Text style={styles.selection}>{mk.selection}</Text>
                </View>
                {mk.bookmakers.length === 0 ? (
                  <Text style={styles.noSignalSmall}>NO SIGNAL - no live bookmaker prices</Text>
                ) : (
                  <>
                    <View style={styles.rowLabels}>
                      <Text style={styles.colLabel}>BOOK</Text>
                      <Text style={styles.colLabel}>ODDS</Text>
                      <Text style={styles.colLabel}>IMP %</Text>
                      <Text style={styles.colLabel}>FAIR %</Text>
                      <Text style={styles.colLabel}>EDGE %</Text>
                      <Text style={styles.colLabel}>STAKE</Text>
                    </View>
                    {mk.bookmakers.slice(0, 6).map((b, i) => {
                      const edge = mk.fair_probability - b.implied_probability;
                      return (
                        <View key={`${b.bookmaker}-${i}`} style={styles.dataRow}>
                          <Text style={[styles.cell, styles.bookCell]} numberOfLines={1}>{b.bookmaker}</Text>
                          <Text style={[styles.cell, styles.mono]}>{formatAmericanOdds(b.price)}</Text>
                          <Text style={[styles.cell, styles.mono]}>{b.implied_probability.toFixed(1)}</Text>
                          <Text style={[styles.cell, styles.mono, { color: Colors.primary }]}>
                            {mk.fair_probability.toFixed(1)}
                          </Text>
                          <Text style={[styles.cell, styles.mono, { color: evColor(edge) }]}>
                            {edge > 0 ? '+' : ''}{edge.toFixed(1)}
                          </Text>
                          <Text style={[styles.cell, styles.mono, { color: Colors.warning }]}>
                            {edge > 0 ? `${(mk.kelly_stake * 100).toFixed(1)}u` : '--'}
                          </Text>
                        </View>
                      );
                    })}
                  </>
                )}
              </View>
            </TouchableOpacity>
          ))}
        </ScrollView>
      )}
    </View>
  );
}

/** Charts tab: all series derived from real feed numbers only. */
function VisualsTab({ market, screenWidth }: { market?: Market; screenWidth: number }) {
  if (!market || market.bookmakers.length === 0) {
    return (
      <View style={styles.center}>
        <Text style={styles.noSignal}>NO SIGNAL</Text>
        <Text style={styles.subtle}>No live prices to visualize for this market.</Text>
      </View>
    );
  }

  const labels = market.bookmakers.slice(0, 8).map((b) => b.bookmaker.slice(0, 6));
  const rawProbs = market.bookmakers.slice(0, 8).map((b) => Math.round(b.implied_probability));
  const fairProbs = market.bookmakers.slice(0, 8).map(() => Math.round(market.fair_probability));

  const movement = market.movement ?? [];
  const movementValues = movement.map((p) => p[1]);
  const chartWidth = screenWidth - Spacing.xl * 2;

  const kellyPct = Math.min(Math.max(market.kelly_stake * 100, 0), 10); // cap display at 10u
  const riskLevel = kellyPct >= 5 ? 'HIGH' : kellyPct >= 2 ? 'MODERATE' : kellyPct > 0 ? 'LOW' : 'NONE';
  const riskColor = riskLevel === 'HIGH' ? Colors.danger : riskLevel === 'MODERATE' ? Colors.warning : Colors.success;

  const baseChartConfig = {
    backgroundGradientFrom: Colors.surface,
    backgroundGradientTo: Colors.surfaceAlt,
    decimalPlaces: 0,
    color: (opacity = 1) => `rgba(0, 229, 255, ${opacity})`,
    labelColor: () => Colors.textSecondary,
    propsForBackgroundLines: { stroke: Colors.gridLine },
  };

  return (
    <ScrollView contentContainerStyle={{ padding: Spacing.lg, gap: Spacing.lg }}>
      <View style={styles.chartCard}>
        <Text style={styles.chartTitle}>VIG REMOVAL - RAW vs FAIR PROBABILITY</Text>
        <Text style={styles.subtle}>
          {market.name} {market.line ?? ''} | {market.selection}
        </Text>
        <LineChart
          data={{
            labels,
            datasets: [
              { data: rawProbs, colors: () => Colors.accentPink },
              { data: fairProbs, colors: () => Colors.primary },
            ],
          }}
          width={chartWidth}
          height={200}
          yAxisSuffix="%"
          bezier
          withInnerLines
          chartConfig={baseChartConfig}
          style={styles.chart}
        />
        <View style={styles.legendRow}>
          <View style={[styles.legendDot, { backgroundColor: Colors.accentPink }]} />
          <Text style={styles.legendText}>RAW IMPLIED (with vig)</Text>
          <View style={[styles.legendDot, { backgroundColor: Colors.primary }]} />
          <Text style={styles.legendText}>FAIR (vig removed)</Text>
        </View>
      </View>

      <View style={styles.chartCard}>
        <Text style={styles.chartTitle}>LINE MOVEMENT - LAST 2 HOURS</Text>
        {movementValues.length >= 2 ? (
          <LineChart
            data={{ labels: movement.map((_, i) => `${i}`), datasets: [{ data: movementValues }] }}
            width={chartWidth}
            height={140}
            yAxisSuffix=""
            bezier
            chartConfig={{ ...baseChartConfig, color: (o = 1) => `rgba(0, 255, 157, ${o})` }}
            style={styles.chart}
          />
        ) : (
          <Text style={styles.subtle}>Insufficient snapshots yet. Movement history builds across engine cycles.</Text>
        )}
      </View>

      <View style={styles.chartCard}>
        <Text style={styles.chartTitle}>KELLY SIZING GAUGE</Text>
        <View style={styles.gaugeTrack}>
          <View style={[styles.gaugeFill, { width: `${(kellyPct / 10) * 100}%`, backgroundColor: riskColor }]} />
        </View>
        <View style={styles.gaugeLabels}>
          <Text style={styles.subtle}>0u</Text>
          <Text style={[styles.riskText, { color: riskColor }]}>
            {(market.kelly_stake * 100).toFixed(1)}u QUARTER-KELLY | RISK: {riskLevel}
          </Text>
          <Text style={styles.subtle}>10u</Text>
        </View>
        <Text style={[styles.evBig, { color: evColor(market.ev_percent) }]}>
          EV {market.ev_percent > 0 ? '+' : ''}{market.ev_percent.toFixed(2)}%
        </Text>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: Colors.background },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: Colors.background, padding: Spacing.xl },
  header: { padding: Spacing.lg, backgroundColor: Colors.surface, borderBottomWidth: 1, borderBottomColor: Colors.border },
  sport: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 10, letterSpacing: 2 },
  teams: { color: Colors.textPrimary, fontFamily: Fonts.ui, fontWeight: '700', fontSize: 18, marginTop: 2 },
  subtle: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: 4 },
  tabRow: { flexDirection: 'row', paddingHorizontal: Spacing.md, paddingVertical: Spacing.sm, gap: 6 },
  tab: { flex: 1, paddingVertical: 8, borderRadius: Radius.sm, backgroundColor: Colors.surface, borderWidth: 1, borderColor: Colors.border, alignItems: 'center' },
  tabActive: { borderColor: Colors.primary, backgroundColor: '#0B2A33' },
  tabText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 10 },
  tabTextActive: { color: Colors.primary, fontWeight: '700' },
  marketCard: { marginHorizontal: Spacing.lg, marginTop: Spacing.md, backgroundColor: Colors.surface, borderRadius: Radius.md, borderWidth: 1, borderColor: Colors.border, padding: Spacing.md },
  marketHeader: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: Spacing.sm },
  marketTitle: { color: Colors.textPrimary, fontFamily: Fonts.ui, fontWeight: '700', fontSize: 13 },
  selection: { color: Colors.primary, fontFamily: Fonts.ui, fontWeight: '600', fontSize: 13 },
  rowLabels: { flexDirection: 'row', borderBottomWidth: 1, borderBottomColor: Colors.border, paddingBottom: 4, marginBottom: 4 },
  colLabel: { flex: 1, color: Colors.textMuted, fontFamily: Fonts.ui, fontSize: 8, letterSpacing: 0.5 },
  dataRow: { flexDirection: 'row', paddingVertical: 3 },
  cell: { flex: 1, fontSize: 11, color: Colors.textPrimary },
  bookCell: { fontFamily: Fonts.ui, flex: 1.4 },
  mono: { fontFamily: Fonts.mono },
  noSignal: { color: Colors.danger, fontFamily: Fonts.mono, fontSize: 16, fontWeight: '700', letterSpacing: 2 },
  noSignalSmall: { color: Colors.textMuted, fontFamily: Fonts.mono, fontSize: 10, paddingVertical: 6 },
  chartCard: { backgroundColor: Colors.surface, borderRadius: Radius.md, borderWidth: 1, borderColor: Colors.border, padding: Spacing.md },
  chartTitle: { color: Colors.textPrimary, fontFamily: Fonts.mono, fontSize: 11, fontWeight: '700', letterSpacing: 1 },
  chart: { borderRadius: Radius.sm, marginTop: Spacing.sm },
  legendRow: { flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: Spacing.sm, flexWrap: 'wrap' },
  legendDot: { width: 8, height: 8, borderRadius: 4 },
  legendText: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 10, marginRight: Spacing.sm },
  gaugeTrack: { height: 14, backgroundColor: Colors.surfaceAlt, borderRadius: 7, borderWidth: 1, borderColor: Colors.border, marginTop: Spacing.md, overflow: 'hidden' },
  gaugeFill: { height: '100%', borderRadius: 7 },
  gaugeLabels: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 6 },
  riskText: { fontFamily: Fonts.mono, fontSize: 11, fontWeight: '700' },
  evBig: { fontFamily: Fonts.mono, fontSize: 26, fontWeight: '700', marginTop: Spacing.md, textAlign: 'center' },
});
