
// Primary source: raw GitHub feed committed by the engine every 15 minutes.
export const FEED_URL =
  'https://raw.githubusercontent.com/petrsidann/Stratum/main/data/live_market_feed.json';

// Bundled fallback: copy of the latest scan baked into the static site at
// build time (CI copies data/live_market_feed.json to public/data/latest_scan.json).
export const BUNDLED_FEED_URL = './data/latest_scan.json';

export interface BookmakerPrice {
  bookmaker: string;
  price: number;
  decimal: number;
  implied_probability: number;
}

export interface Market {
  key: string;
  type: string;
  name: string;
  line: number | null;
  selection: string;
  raw_probability: number;
  fair_probability: number;
  vig_removed_percent: number;
  best: { bookmaker: string; price: number; decimal: number };
  bookmakers: BookmakerPrice[];
  num_bookmakers: number;
  ev_percent: number;
  kelly_stake: number;
  confidence: number;
  movement: [string, number][];
  direction: string;
  has_data: boolean;
  signals: string[];
}

export interface Match {
  id: string;
  source: string;
  sources?: string[];
  sport: string;
  sport_key: string;
  icon: string;
  home_team: string;
  away_team: string;
  commence: string;
  status: string;
  markets: Market[];
  market_count?: number;
}

export interface TopEdge {
  match_id: string;
  sport: string;
  icon: string;
  home_team: string;
  away_team: string;
  commence: string;
  market_type: string;
  market_name: string;
  line: number | null;
  selection: string;
  best_bookmaker: string;
  best_price: number;
  fair_probability: number;
  raw_probability: number;
  ev_percent: number;
  kelly_stake: number;
  confidence: number;
  signals: string[];
}

export interface FeedMeta {
  version: string;
  generated_at: string;
  next_refresh_minutes?: number;
  data_policy?: string;
  sources_attempted?: string[];
  sources_active?: string[];
  matches_scanned: number;
  markets_scanned: number;
  signals_found: number;
  error?: string;
}

export interface MarketFeed {
  meta: FeedMeta;
  catalog: Record<string, string[]>;
  top_edges: TopEdge[];
  matches: Match[];
}

const EMPTY_FEED: MarketFeed = {
  meta: {
    version: 'n/a',
    generated_at: '',
    matches_scanned: 0,
    markets_scanned: 0,
    signals_found: 0,
    error: 'feed_unavailable',
  },
  catalog: {},
  top_edges: [],
  matches: [],
};

let cache: { feed: MarketFeed; fetchedAt: number } | null = null;
const CACHE_TTL_MS = 5 * 60 * 1000;

function isWellFormed(feed: unknown): feed is MarketFeed {
  const f = feed as MarketFeed;
  return !!f && typeof f === 'object' && Array.isArray(f.matches);
}

async function tryFetch(url: string): Promise<MarketFeed | null> {
  try {
    // Use the Fetch API (not XHR) so no CORS preflight is triggered; raw
    // GitHub serves simple GETs with Access-Control-Allow-Origin: *.
    const response = await fetch(url, {
      method: 'GET',
      cache: 'no-store',
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) return null;
    const data = (await response.json()) as MarketFeed;
    if (isWellFormed(data)) return data;
    return null;
  } catch {
    return null;
  }
}

/**
 * Downloads live_market_feed.json from the repository raw URL.
 * Falls back to the bundled latest_scan.json snapshot baked into the PWA at
 * build time (flagged so the UI labels freshness honestly).
 * On total failure returns an empty feed with error metadata so every screen
 * renders "No Signal" states instead of fabricated rows.
 */
export async function fetchMarketFeed(force = false): Promise<MarketFeed> {
  const now = Date.now();
  if (!force && cache && now - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.feed;
  }

  const live = await tryFetch(FEED_URL);
  if (live) {
    cache = { feed: live, fetchedAt: now };
    return live;
  }

  if (cache) {
    return { ...cache.feed, meta: { ...cache.feed.meta, error: 'stale_cache' } };
  }

  const bundled = await tryFetch(BUNDLED_FEED_URL);
  if (bundled) {
    cache = { feed: bundled, fetchedAt: now };
    return {
      ...bundled,
      meta: { ...bundled.meta, error: 'bundled_snapshot' },
    };
  }

  return EMPTY_FEED;
}

export function findMatch(feed: MarketFeed, matchId: string): Match | undefined {
  return feed.matches.find((m) => m.id === matchId);
}

export function groupMarketsByTab(match: Match): {
  main: Market[];
  props: Market[];
  derivatives: Market[];
} {
  const main: Market[] = [];
  const props: Market[] = [];
  const derivatives: Market[] = [];
  for (const mk of match.markets ?? []) {
    if (mk.type === 'player_prop') props.push(mk);
    else if (['moneyline', 'spread', 'total'].includes(mk.type)) main.push(mk);
    else derivatives.push(mk);
  }
  return { main, props, derivatives };
}
