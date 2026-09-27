import React, { useState } from 'react';
import { Colors, Fonts, Radius, Spacing } from '../theme/colors';
import { FEED_URL } from '../utils/apiClient';
import { SPORTS, Settings, loadSettings, saveSettings } from '../utils/settings';

/**
 * Bankroll input (drives unit sizing shown in OddsCard), notification toggle,
 * and per-sport filters. Persisted locally with localStorage.
 */
export default function SettingsScreen() {
  const [settings, setSettings] = useState<Settings>(() => loadSettings());
  const [savedFlash, setSavedFlash] = useState(false);

  const persist = (next: Settings) => {
    setSettings(next);
    saveSettings(next);
    setSavedFlash(true);
    window.setTimeout(() => setSavedFlash(false), 1200);
  };

  const toggleSport = (sport: string) => {
    const has = settings.enabledSports.includes(sport);
    const enabledSports = has
      ? settings.enabledSports.filter((s) => s !== sport)
      : [...settings.enabledSports, sport];
    persist({ ...settings, enabledSports });
  };

  return (
    <div style={styles.page}>
      <div style={styles.card}>
        <div style={styles.sectionTitle}>BANKROLL</div>
        <div style={styles.hint}>Kelly stakes are reported as % of bankroll. Set your roll here.</div>
        <input
          style={styles.input}
          inputMode="decimal"
          value={String(settings.bankroll)}
          onChange={(e) => {
            const n = Number(e.target.value.replace(/[^0-9.]/g, ''));
            persist({ ...settings, bankroll: Number.isFinite(n) ? n : 0 });
          }}
          placeholder="1000"
        />
        <div style={styles.unitPreview}>
          1% = ${' '}
          <span style={{ fontFamily: Fonts.mono, color: Colors.primary }}>
            {(settings.bankroll * 0.01).toFixed(2)}
          </span>
        </div>
      </div>

      <div style={styles.card}>
        <label style={styles.switchRow}>
          <span style={{ flex: 1 }}>
            <span style={styles.sectionTitle}>NOTIFICATIONS</span>
            <span style={{ ...styles.hint, display: 'block' }}>
              Alert when a new signal above 80% confidence appears.
            </span>
          </span>
          <input
            type="checkbox"
            checked={settings.notifications}
            onChange={(e) => persist({ ...settings, notifications: e.target.checked })}
            style={styles.checkbox}
          />
        </label>
      </div>

      <div style={styles.card}>
        <div style={styles.sectionTitle}>SPORT FILTERS</div>
        <div style={styles.sportGrid}>
          {SPORTS.map((sport) => {
            const active = settings.enabledSports.includes(sport);
            return (
              <button
                key={sport}
                type="button"
                onClick={() => toggleSport(sport)}
                style={{ ...styles.sportChip, ...(active ? styles.sportChipActive : null) }}
              >
                {sport.toUpperCase()}
              </button>
            );
          })}
        </div>
      </div>

      <div style={styles.card}>
        <div style={styles.sectionTitle}>DATA SOURCE</div>
        <div style={styles.sourceUrl}>{FEED_URL}</div>
        <div style={styles.hint}>
          Refreshed every 15 minutes by the GitHub Actions engine. No synthetic data is
          ever rendered; missing markets display &quot;No Signal&quot;.
        </div>
      </div>

      {savedFlash && <div style={styles.saved}>SAVED</div>}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: Spacing.lg,
    display: 'flex',
    flexDirection: 'column',
    gap: Spacing.lg,
    maxWidth: 720,
    width: '100%',
    margin: '0 auto',
    boxSizing: 'border-box',
  },
  card: {
    backgroundColor: Colors.surface,
    borderRadius: Radius.md,
    border: `1px solid ${Colors.border}`,
    padding: Spacing.lg,
  },
  sectionTitle: { color: Colors.textPrimary, fontFamily: Fonts.mono, fontSize: 12, fontWeight: 700, letterSpacing: 1 },
  hint: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: 4, lineHeight: 1.5 },
  input: {
    fontFamily: Fonts.mono,
    fontSize: 20,
    color: Colors.primary,
    background: 'transparent',
    border: 'none',
    borderBottom: `1px solid ${Colors.border}`,
    marginTop: Spacing.md,
    paddingBottom: 6,
    width: '100%',
    outline: 'none',
  },
  unitPreview: { color: Colors.textSecondary, fontFamily: Fonts.ui, fontSize: 11, marginTop: Spacing.sm },
  switchRow: { display: 'flex', alignItems: 'center', cursor: 'pointer' },
  checkbox: { width: 20, height: 20, accentColor: Colors.primary, cursor: 'pointer' },
  sportGrid: { display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: Spacing.md },
  sportChip: {
    border: `1px solid ${Colors.border}`,
    borderRadius: Radius.sm,
    padding: '5px 10px',
    backgroundColor: Colors.surfaceAlt,
    color: Colors.textSecondary,
    fontFamily: Fonts.mono,
    fontSize: 9,
    cursor: 'pointer',
  },
  sportChipActive: { borderColor: Colors.success, backgroundColor: '#0C2A1F', color: Colors.success, fontWeight: 700 },
  sourceUrl: { color: Colors.primary, fontFamily: Fonts.mono, fontSize: 9, marginTop: Spacing.sm, wordBreak: 'break-all' },
  saved: { color: Colors.success, fontFamily: Fonts.mono, textAlign: 'center', letterSpacing: 2 },
};
