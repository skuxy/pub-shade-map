/**
 * config.js — resolve the backend API base URL at runtime.
 *
 * When running locally the frontend is served by the FastAPI backend on the
 * same origin, so relative paths work fine (API_BASE = '').
 *
 * When running on GitHub Pages the frontend is served from GitHub's CDN, so
 * requests must go to the separately deployed backend.
 *
 * Set BACKEND_URL below once you have a deployed backend URL (e.g. Render).
 * Leave it empty while developing locally — it won't be used.
 */

const BACKEND_URL = 'https://pub-shade-map.duckdns.org';

function getApiBase() {
  const { hostname } = window.location;
  const isLocal = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '';
  if (isLocal) return '';                       // same-origin, relative paths
  if (BACKEND_URL) return BACKEND_URL;          // explicit override always wins
  return '';                                    // fallback (will fail until set)
}

const API_BASE = getApiBase();

if (!API_BASE && window.location.hostname.includes('github.io')) {
  console.warn(
    '[config] Running on GitHub Pages but BACKEND_URL is not set in frontend/js/config.js. ' +
    'API calls will fail. Deploy the backend and set BACKEND_URL.'
  );
}
