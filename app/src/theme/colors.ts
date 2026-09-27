// Stratum V2.0 design system tokens (web port of the native theme).
// Strict palette: dark base, neon cyan primary, green success, red danger.

export const Colors = {
  background: '#0F111A',
  surface: '#1E2330',
  surfaceAlt: '#171B26',
  primary: '#00E5FF',
  success: '#00FF9D',
  danger: '#FF4D4D',
  warning: '#FFC857',
  accentPink: '#FF4DA6',
  textPrimary: '#E8ECF4',
  textSecondary: '#8A93A6',
  textMuted: '#4C566A',
  border: '#2A3142',
  gridLine: '#232936',
};

export const Fonts = {
  ui: "'Inter', system-ui, -apple-system, sans-serif",
  mono: "'JetBrains Mono', 'SFMono-Regular', Menlo, monospace",
};

export const Radius = { sm: 6, md: 10, lg: 16 };
export const Spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 };

export function evColor(evPercent: number): string {
  if (evPercent >= 3) return Colors.success;
  if (evPercent > 0) return Colors.primary;
  return Colors.danger;
}

export function confidenceColor(confidence: number): string {
  if (confidence >= 80) return Colors.success;
  if (confidence >= 60) return Colors.primary;
  if (confidence >= 40) return Colors.warning;
  return Colors.textMuted;
}

export function formatAmericanOdds(price: number | null | undefined): string {
  if (price === null || price === undefined || Number.isNaN(price)) return 'No Signal';
  return price > 0 ? `+${price}` : `${price}`;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${value.toFixed(digits)}%`;
}
