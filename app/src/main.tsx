import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { Colors, Fonts } from './theme/colors';

// Global reset and base theme for the Stratum PWA.
const globalCss = document.createElement('style');
globalCss.textContent = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body, #root { height: 100%; }
  body {
    background-color: ${Colors.background};
    color: ${Colors.textPrimary};
    font-family: ${Fonts.ui};
    -webkit-font-smoothing: antialiased;
    overscroll-behavior-y: none;
  }
  button { font: inherit; }
  @keyframes stratum-spin { to { transform: rotate(360deg); } }
`;
document.head.appendChild(globalCss);

const container = document.getElementById('root');
if (container) {
  createRoot(container).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}

// Service worker registration for offline support / install prompt.
// vite-plugin-pwa emits sw.js at the site root during production builds;
// in dev the module simply does not exist and registration is skipped.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    const swUrl = new URL('sw.js', document.baseURI).href;
    fetch(swUrl, { method: 'HEAD' })
      .then((res) => {
        if (res.ok) return navigator.serviceWorker.register(swUrl);
      })
      .catch(() => {
        // No SW available (e.g. manifest-only deploy): installation via
        // "Add to Home Screen" still works through the web app manifest.
      });
  });
}
