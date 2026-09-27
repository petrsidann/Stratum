import React from 'react';
import { StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { Colors, Fonts, Radius, Spacing, confidenceColor, evColor, formatAmericanOdds, formatPercent } from '../theme/colors';
import { TopEdge } from '../utils/apiClient';

const SPORT_GLYPHS: Record<string, string> = {
  basketball: '[NBA]',
  football: '[NFL]',
  baseball: '[MLB]',
  soccer: '[FC]',
  hockey: '[NHL]',
};

interface Props {
  edge: TopEdge;
  onPress?: () => void;
}

/**
 * Single market opportunity row. Color-coded EV, fair vs best odds,
 * Kelly stake, confidence bar, and signal chips. Dense Bloomberg-style layout.
 */
export default function OddsCard({ edge, onPress }: Props) {
  const ev = edge.ev_percent;
  const color = evColor(ev);
  const confColor = confidenceColor(edge.confidence);
  const stakeUnits = (edge.kelly_stake * 100).toFixed(1);

  return (
    <TouchableOpacity
      activeOpacity={0.75}
      onPress={onPress}
      style={styles.card}
    >
      <View style={styles.headerRow}>
        <Text style={[styles.sportGlyph, { color: Colors.primary }]}>
          {SPORT_GLYPHS[edge.icon] ?? `[${edge.sport.slice(0, 3).toUpperCase()}]`}
        </Text>
        <Text style={styles.teams} numberOfLines={1}>
          {edge.away_team} @ {edge.home_team}
        </Text>
        <View style={[styles.confidencePill, { borderColor: confColor }]}>
          <Text style={[styles.confidenceText, { color: confColor }]}>
            {formatPercent(edge.confidence, 0)}
          </Text>
        </View>
      </View>

      <View style={styles.marketRow}>
        <Text style={styles.marketName}>
          {edge.market_name}
          {edge.line !== null && edge.line !== undefined ? ` ${edge.line}` : ''}
        </Text>
        <Text style={styles.selection}>{edge.selection}</Text>
      </View>

      <View style={styles.numbersRow}>
        <View style={styles.numberBlock}>
          <Text style={styles.label}>BEST ({(edge.best_bookmaker || '?').slice(0, 10)})</Text>
          <Text style={[styles.value, { color: Colors.textPrimary }]}>
            {formatAmericanOdds(edge.best_price)}
          </Text>
        </View>
        <View style={styles.numberBlock}>
          <Text style={styles.label}>FAIR PROB</Text>
          <Text style={[styles.value, { color: Colors.primary }]}>
            {formatPercent(edge.fair_probability)}
          </Text>
        </View>
        <View style={styles.numberBlock}>
          <Text style={styles.label}>EV</Text>
          <Text style={[styles.value, { color }]}>
            {ev > 0 ? '+' : ''}{formatPercent(ev, 2)}
          </Text>
        </View>
        <View style={styles.numberBlock}>
          <Text style={styles.label}>KELLY</Text>
          <Text style={[styles.value, { color: Colors.warning }]}>{stakeUnits}u</Text>
        </View>
      </View>

      {edge.signals?.length > 0 && (
        <View style={styles.signalRow}>
          {edge.signals.map((s) => (
            <View key={s} style={styles.signalChip}>
              <Text style={styles.signalText}>{s}</Text>
            </View>
          ))}
        </View>
      )}
    </TouchableOpacity>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: Colors.surface,
    borderRadius: Radius.md,
    borderWidth: 1,
    borderColor: Colors.border,
    padding: Spacing.md,
    marginHorizontal: Spacing.lg,
    marginTop: Spacing.md,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.sm,
  },
  sportGlyph: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: '700',
  },
  teams: {
    flex: 1,
    color: Colors.textPrimary,
    fontFamily: Fonts.ui,
    fontWeight: '600',
    fontSize: 14,
  },
  confidencePill: {
    borderWidth: 1,
    borderRadius: Radius.sm,
    paddingHorizontal: 6,
    paddingVertical: 2,
  },
  confidenceText: {
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: '700',
  },
  marketRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginTop: Spacing.xs,
  },
  marketName: {
    color: Colors.textSecondary,
    fontFamily: Fonts.ui,
    fontSize: 12,
  },
  selection: {
    color: Colors.textPrimary,
    fontFamily: Fonts.ui,
    fontWeight: '600',
    fontSize: 12,
  },
  numbersRow: {
    flexDirection: 'row',
    marginTop: Spacing.sm,
    justifyContent: 'space-between',
  },
  numberBlock: {
    flex: 1,
  },
  label: {
    color: Colors.textMuted,
    fontFamily: Fonts.ui,
    fontSize: 9,
    letterSpacing: 0.5,
  },
  value: {
    fontFamily: Fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    marginTop: 2,
  },
  signalRow: {
    flexDirection: 'row',
    gap: 6,
    marginTop: Spacing.sm,
    flexWrap: 'wrap',
  },
  signalChip: {
    backgroundColor: Colors.surfaceAlt,
    borderRadius: Radius.sm,
    borderWidth: 1,
    borderColor: Colors.border,
    paddingHorizontal: 6,
    paddingVertical: 2,
  },
  signalText: {
    color: Colors.accentPink,
    fontFamily: Fonts.mono,
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
});
