import React from 'react';
import { HashRouter, NavLink, Route, Routes } from 'react-router-dom';
import HomeScreen from './screens/HomeScreen';
import MatchDetailScreen from './screens/MatchDetailScreen';
import SettingsScreen from './screens/SettingsScreen';
import { Colors, Fonts, Spacing } from './theme/colors';

/**
 * Stratum V2.0 PWA shell: top nav (terminal header) + routed screens.
 * HashRouter keeps deep links working on GitHub Pages without server rewrites.
 */
export default function App() {
  return (
    <HashRouter>
      <div style={styles.shell}>
        <header style={styles.header}>
          <span style={styles.brand}>
            STRATUM<span style={styles.brandDim}> // QUANT ENGINE</span>
          </span>
          <nav style={styles.nav}>
            <NavLink to="/" end style={navLinkStyle}>
              EDGES
            </NavLink>
            <NavLink to="/settings" style={navLinkStyle}>
              SETTINGS
            </NavLink>
          </nav>
        </header>

        <main style={styles.main}>
          <Routes>
            <Route path="/" element={<HomeScreen />} />
            <Route path="/match/:matchId" element={<MatchDetailScreen />} />
            <Route path="/settings" element={<SettingsScreen />} />
            <Route path="*" element={<HomeScreen />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
  );
}

function navLinkStyle({ isActive }: { isActive: boolean }): React.CSSProperties {
  return {
    color: isActive ? Colors.primary : Colors.textSecondary,
    fontFamily: Fonts.mono,
    fontSize: 11,
    fontWeight: isActive ? 700 : 400,
    textDecoration: 'none',
    letterSpacing: 1,
    padding: '4px 8px',
    borderBottom: isActive ? `2px solid ${Colors.primary}` : '2px solid transparent',
  };
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
  nav: { display: 'flex', gap: Spacing.sm },
  main: { flex: 1, display: 'flex', flexDirection: 'column' },
};
