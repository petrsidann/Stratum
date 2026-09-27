import axios from 'axios';

export const FEED_URL =
  'https://raw.githubusercontent.com/petersidann/Stratum/main/data/live_market_feed.json';

export interface BookmakerPrice {
  bookmaker: string;
  price: number;
  decimal: number;
  implied_probability: number;
}

export interface MarketMovementPoint {
  0: string;
  1: number;
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

/**
 * Downloads live_market_feed.json from the repository raw URL.
 * On any failure returns an empty feed with error metadata so the UI
 * renders "No Signal" states instead of stale or fabricated data.
 */
export async function fetchMarketFeed(force = false): Promise<MarketFeed> {
  const now = Date.now();
  if (!force && cache && now - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.feed;
  }
  try {
    const response = await axios.get<MarketFeed>(FEED_URL, {
      timeout: 15000,
      headers: { 'Cache-Control': 'no-cache' },
    });
    const feed = response.data;
    if (!feed || !Array.isArray(feed.matches)) {
      throw new Error('malformed feed payload');
    }
    cache = { feed, fetchedAt: now };
    return feed;
  } catch (err) {
    // Serve last known good structure but flag freshness honestly.
    if (cache) {
      return {
        ...cache.feed,
        meta: { ...cache.feed.meta, error: 'stale_cache' },
      };
    }
    return EMPTY_FEED;
  }
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
