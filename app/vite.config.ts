import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// Stratum V2.0 PWA build configuration.
// Base path is "./" so the static bundle works on GitHub Pages project sites
// (e.g. https://petrsidann.github.io/Stratum/) without extra configuration.
export default defineConfig({
  base: './',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icons/icon-192.png', 'icons/icon-512.png'],
      manifest: false, // app/public/manifest.json is authored manually
      workbox: {
        globPatterns: ['**/*.{js,css,html,png,json}'],
        navigateFallbackDenylist: [/^\/data\//],
        runtimeCaching: [
          {
            // Live feed and bundled scan data: network first, fall back to
            // cache when offline so the app still opens with last-known data
            // flagged as stale by the client.
            urlPattern: /\/(data\/latest_scan\.json|live_market_feed\.json)/,
            handler: 'NetworkFirst' as const,
            options: {
              cacheName: 'stratum-feed',
              networkTimeoutSeconds: 10,
              expiration: { maxEntries: 4, maxAgeSeconds: 60 * 60 * 24 },
            },
          },
        ],
      },
    }),
  ],
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
