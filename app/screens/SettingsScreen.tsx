import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Switch, Text, TextInput, TouchableOpacity, View } from 'react-native';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';

const SPORTS = ['NBA', 'NFL', 'MLB', 'NHL', 'Premier League', 'Champions League', 'La Liga', 'Serie A', 'Bundesliga', 'Ligue 1'];

interface Settings {
  bankroll: number;
  notifications: boolean;
  enabledSports: string[];
}

const DEFAULTS: Settings = {
  bankroll: 1000,
  notifications: false,
  enabledSports: [...SPORTS],
};

/**
 * Bankroll input (drives unit sizing shown in OddsCard), notification toggle,
 * and per-sport filters. Persisted locally with AsyncStorage.
 */
export default function SettingsScreen() {
  const [settings, setSettings] = useState<Settings>(DEFAULTS);
  const [loaded, setLoaded] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);

  useEffect(() => {
    AsyncStorage.getItem('stratum.settings')
      .then((raw) => {
        if (raw) setSettings({ ...DEFAULTS, ...JSON.parse(raw) });
      })
      .finally(() => setLoaded(true));
  }, []);

  const persist = async (next: Settings) => {
    setSettings(next);
    await AsyncStorage.setItem('stratum.settings', JSON.stringify(next));
    setSavedFlash(true);
    setTimeout(() => setSavedFlash(false), 1200);
  };

  const toggleSport = (sport: string) => {
    const has = settings.enabledSports.includes(sport);
    const enabledSports = has
      ? settings.enabledSports.filter((s) => s !== sport)
      : [...settings.enabledSports, sport];
    persist({ ...settings, enabledSports });
  };

  if (!loaded) return <View style={styles.container} />;

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: Spacing.lg, gap: Spacing.lg }}>
      <View style={styles.card}>
        <Text style={styles.sectionTitle}>BANKROLL</Text>
        <Text style={styles.hint}>Kelly stakes are reported as % of bankroll. Set your roll here.</Text>
        <TextInput
          style={styles.input}
          keyboardType="numeric"
          value={String(settings.bankroll)}
          onChangeText={(t) => {
            const n = Number(t.replace(/[^0-9.]/g, ''));
            persist({ ...settings, bankroll: Number.isFinite(n) ? n : 0 });
          }}
          placeholder="1000"
          placeholderTextColor={Colors.textMuted}
        />
        <Text style={styles.unitPreview}>
          1% = ${' '}
          <Text style={{ fontFamily: Fonts.mono, color: Colors.primary }}>
            {(settings.bankroll * 0.01).toFixed(2)}
          </Text>
        </Text>
      </View>

      <View style={styles.card}>
        <View style={styles.switchRow}>
          <View style={{ flex: 1 }}>
            <Text style={styles.sectionTitle}>NOTIFICATIONS</Text>
            <Text style={styles.hint}>Alert when a new signal above 80% confidence appears.</Text>
          </View>
          <Switch
            value={settings.notifications}
            onValueChange={(v) => persist({ ...settings, notifications: v })}
            trackColor={{ true: Colors.primary, false: Colors.border }}
            thumbColor={settings.notifications ? Colors.success : Colors.textSecondary}
          />
        </View>
      </View>

      <View style={styles.card}>
        <Text style={styles.sectionTitle}>SPORT FILTERS</Text>
        <View style={styles.sportGrid}>
          {SPORTS.map((sport) => {
            const active = settings.enabledSports.includes(sport);
            return (
              <TouchableOpacity key={sport} onPress={() => toggleSport(sport)} style={[styles.sportChip, active && styles.sportChipActive]}>
                <Text style={[styles.sportText, active && styles.sportTextActive]}>{sport.toUpperCase()}</Text>
              </TouchableOpacity>
            );
          })}
        </View>
      </View>

      <View style={styles.card}>
        <Text style={styles.sectionTitle}>DATA SOURCE</Text>
        <Text style={styles.sourceUrl}>
          https://raw.githubusercontent.com/petersidann/Stratum/main/data/live_market_feed.json
        </Text>
        <Text style={styles.hint}>Refreshed every 15 minutes by the GitHub Actions engine. No synthetic data is ever rendered.</Text>
      </View>

      {savedFlash && (
        <Text style={styles.saved}>SAVED</Text>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: Colors.background },
  card: { backgroundColor: Colors.surface, borderRadius: Radius.md, borderWidth: 1, borderColor: Colors.border, padding: Spacing.lg },
  sectionTitle: { color: Colors.textPrimary, fontFamily: Fonts.mono, fontSize: 12, fontWeight: '700', letterSpacing: 1 },
  hint: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: 4, lineHeight: 16 },
  input: {
    fontFamily: Fonts.mono,
    fontSize: 20,
    color: Colors.primary,
    borderBottomWidth: 1,
    borderBottomColor: Colors.border,
    marginTop: Spacing.md,
    paddingBottom: 6,
  },
  unitPreview: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: Spacing.sm },
  switchRow: { flexDirection: 'row', alignItems: 'center' },
  sportGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: Spacing.md },
  sportChip: { borderWidth: 1, borderColor: Colors.border, borderRadius: Radius.sm, paddingHorizontal: 10, paddingVertical: 5, backgroundColor: Colors.surfaceAlt },
  sportChipActive: { borderColor: Colors.success, backgroundColor: '#0C2A1F' },
  sportText: { color: Colors.textSecondary, fontFamily: Fonts.mono, fontSize: 9 },
  sportTextActive: { color: Colors.success, fontWeight: '700' },
  sourceUrl: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 9, marginTop: Spacing.sm },
  saved: { color: Colors.success, fontFamily: Fonts.mono, textAlign: 'center', letterSpacing: 2 },
});
