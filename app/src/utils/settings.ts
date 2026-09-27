// Local settings store backed by localStorage (web equivalent of AsyncStorage).

export interface Settings {
  bankroll: number;
  notifications: boolean;
  enabledSports: string[];
}

export const SPORTS = [
  'NBA',
  'NFL',
  'MLB',
  'NHL',
  'Premier League',
  'Champions League',
  'La Liga',
  'Serie A',
  'Bundesliga',
  'Ligue 1',
];

const STORAGE_KEY = 'stratum.settings';

export const DEFAULT_SETTINGS: Settings = {
  bankroll: 1000,
  notifications: false,
  enabledSports: [...SPORTS],
};

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch {
    // Corrupt or unavailable storage: fall through to defaults.
  }
  return { ...DEFAULT_SETTINGS };
}

export function saveSettings(next: Settings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage full or blocked: settings remain in memory for this session.
  }
}
