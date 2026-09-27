import React from 'react';
import { HashRouter, Route, Routes } from 'react-router-dom';
import HunterScreen from './screens/HunterScreen';
import MatchDetailScreen from './screens/MatchDetailScreen';
import { Colors, Fonts, Spacing } from './theme/colors';

/**
 * HUNTER MODE shell.
 *
 * The app no longer boots into a static/cron-fed dashboard. It starts in an
 * EMPTY state (search bar only) and every piece of data on screen comes from
 * an explicit, user-triggered hunt (see screens/HunterScreen.tsx).
 *
 * Routes kept for deep links only:
 *   /                -> Hunter terminal (IDLE | LOADING | RESULTS | ERROR)
 *   /match/:id       -> legacy detail view (reads last committed feed)
 */
export default function App() {
  return (
    <HashRouter>
      <div style={styles.shell}>
        <header style={styles.header}>
          <span style={styles.brand}>
            STRATUM<span style={styles.brandDim}> // HUNTER MODE</span>
          </span>
          <span style={styles.badge}>REAL-TIME · NO CRON · NO CACHED DATA</span>
        </header>

        <main style={styles.main}>
          <Routes>
            <Route path="/" element={<HunterScreen />} />
            <Route path="/match/:matchId" element={<MatchDetailScreen />} />
            <Route path="*" element={<HunterScreen />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
  );
}

const styles: Record<string, React.CSSProperties> = {
  shell: {
    display: 'flex',
    flexDirection: 'column',
    minHeight: '100vh',
    backgroundColor: Colors.background,
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: `${Spacing.md}px ${Spacing.lg}px`,
    backgroundColor: Colors.surface,
    borderBottom: `1px solid ${Colors.border}`,
    position: 'sticky',
    top: 0,
    zIndex: 10,
  },
  brand: {
    color: Colors.primary,
    fontFamily: Fonts.mono,
    fontSize: 14,
    fontWeight: 700,
    letterSpacing: 2,
  },
  brandDim: { color: Colors.textMuted, fontWeight: 400, fontSize: 10 },
  badge: {
    color: Colors.success,
    fontFamily: Fonts.mono,
    fontSize: 9,
    letterSpacing: 1,
    border: `1px solid ${Colors.border}`,
    borderRadius: 4,
    padding: '3px 6px',
  },
  main: { flex: 1, display: 'flex', flexDirection: 'column' },
};
