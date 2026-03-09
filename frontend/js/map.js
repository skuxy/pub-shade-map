/**
 * map.js — Leaflet map initialisation, pub markers, and panel logic.
 *
 * Flow:
 *  1. Initialise Leaflet map on Zagreb.
 *  2. Fetch /api/pubs → render pub markers.
 *  3. On marker click → open side panel, fetch shade timeline + weather.
 *  4. Date picker change → re-fetch shade timeline.
 */

const ZAGREB = [45.815, 15.982];
// API_BASE is defined in config.js (loaded before this script)

// ── Map setup ────────────────────────────────────────────────────────────
const map = L.map('map', {
  center: ZAGREB,
  zoom: 14,
  zoomControl: true,
});

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

// ── State ─────────────────────────────────────────────────────────────────
let allPubs = [];
let markerMap = {};          // pub_id → Leaflet marker
let selectedPubId = null;

// ── Helpers ───────────────────────────────────────────────────────────────

function todayZagreb() {
  return luxon.DateTime.now().setZone('Europe/Zagreb').toFormat('yyyy-MM-dd');
}

function makeMarkerIcon(status) {
  // status: 'sun' | 'shade' | 'night'
  return L.divIcon({
    className: '',
    html: `<div class="pub-marker ${status}"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

function setMarkerStatus(pubId, status) {
  const marker = markerMap[pubId];
  if (!marker) return;
  marker._shadeStatus = status;
  marker.setIcon(makeMarkerIcon(status + (pubId === selectedPubId ? ' selected' : '')));
}

// ── Current-time shade status (refreshed on load + every 5 min) ──────────

async function refreshCurrentStatus() {
  const now = luxon.DateTime.now().setZone('Europe/Zagreb');
  const timeLabel = now.toFormat('HH:mm');
  const dateStr = now.toFormat('yyyy-MM-dd');

  for (const pub of allPubs) {
    const pubId = pub.properties.id;
    try {
      const resp = await fetch(`${API_BASE}/api/shade/${encodeURIComponent(pubId)}?date=${dateStr}`);
      if (!resp.ok) continue;
      const data = await resp.json();

      // Find the entry closest to now
      const entry = data.timeline.find(t => {
        const local = luxon.DateTime.fromISO(t.time, { zone: 'utc' })
          .setZone('Europe/Zagreb').toFormat('HH:mm');
        return local >= timeLabel;
      });

      if (!entry) {
        setMarkerStatus(pubId, 'night');
      } else {
        setMarkerStatus(pubId, entry.in_shade ? 'shade' : 'sun');
      }
    } catch (_) {
      setMarkerStatus(pubId, 'night');
    }
  }
}

// ── Panel ─────────────────────────────────────────────────────────────────

function openPanel(pub) {
  const panel = document.getElementById('panel');
  panel.classList.remove('panel-hidden');

  document.getElementById('pub-name').textContent = pub.properties.name || 'Unnamed pub';
  document.getElementById('pub-amenity').textContent = pub.properties.amenity || 'venue';

  // Reset loading states
  document.getElementById('timeline-loading').textContent = 'Loading…';
  document.getElementById('timeline-loading').style.display = 'block';
  document.getElementById('timeline-chart').style.display = 'none';
  document.getElementById('weather-loading').textContent = 'Loading…';
  document.getElementById('weather-loading').style.display = 'block';
  document.getElementById('weather-container').innerHTML = '';
  document.getElementById('status-badge').className = 'badge badge-hidden';

  const [lon, lat] = pub.geometry.coordinates;
  const pubId = pub.properties.id;

  // Set date picker to today
  const datePicker = document.getElementById('date-picker');
  datePicker.value = todayZagreb();
  datePicker.max = luxon.DateTime.now().setZone('Europe/Zagreb')
    .plus({ days: 3 }).toFormat('yyyy-MM-dd');
  datePicker.min = todayZagreb();

  loadShade(pubId, datePicker.value);
  loadWeather(lat, lon);

  // Wire date picker
  datePicker.onchange = () => loadShade(pubId, datePicker.value);
}

function closePanel() {
  document.getElementById('panel').classList.add('panel-hidden');
  if (selectedPubId) {
    const prev = markerMap[selectedPubId];
    if (prev) prev.setIcon(makeMarkerIcon(prev._shadeStatus || 'night'));
    selectedPubId = null;
  }
}

// ── Load shade data ───────────────────────────────────────────────────────

async function loadShade(pubId, dateStr) {
  document.getElementById('timeline-loading').textContent = 'Loading timeline…';
  document.getElementById('timeline-loading').style.display = 'block';
  document.getElementById('timeline-chart').style.display = 'none';
  document.getElementById('status-badge').className = 'badge badge-hidden';

  try {
    const resp = await fetch(`${API_BASE}/api/shade/${encodeURIComponent(pubId)}?date=${dateStr}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    renderTimeline('timeline-chart', data.timeline, dateStr);
    updateStatusBadge(data.timeline, dateStr);
  } catch (err) {
    document.getElementById('timeline-loading').textContent = `Error loading timeline: ${err.message}`;
  }
}

function updateStatusBadge(timeline, dateStr) {
  const badge = document.getElementById('status-badge');
  const icon  = document.getElementById('status-icon');
  const text  = document.getElementById('status-text');

  const now = luxon.DateTime.now().setZone('Europe/Zagreb');
  const todayStr = now.toFormat('yyyy-MM-dd');

  if (dateStr !== todayStr || timeline.length === 0) {
    badge.className = 'badge badge-hidden';
    return;
  }

  const timeLabel = now.toFormat('HH:mm');
  const entry = timeline.find(t => {
    const local = luxon.DateTime.fromISO(t.time, { zone: 'utc' })
      .setZone('Europe/Zagreb').toFormat('HH:mm');
    return local >= timeLabel;
  });

  if (!entry) {
    badge.className = 'badge nighttime';
    icon.textContent = '🌙';
    text.textContent = 'Sun has set';
  } else if (entry.in_shade) {
    badge.className = 'badge in-shade';
    icon.textContent = '🌑';
    text.textContent = 'Currently in shade';
  } else {
    badge.className = 'badge in-sun';
    icon.textContent = '☀️';
    text.textContent = 'Currently in sun';
  }
}

// ── Load weather ──────────────────────────────────────────────────────────

async function loadWeather(lat, lon) {
  const data = await fetchWeather(lat, lon);
  renderWeather('weather-container', data);
}

// ── Initialise pubs ───────────────────────────────────────────────────────

async function initPubs() {
  const overlay = document.getElementById('loading-overlay');

  try {
    const resp = await fetch(`${API_BASE}/api/pubs`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const geojson = await resp.json();
    allPubs = geojson.features || [];

    for (const pub of allPubs) {
      const [lon, lat] = pub.geometry.coordinates;
      const pubId = pub.properties.id;

      const marker = L.marker([lat, lon], {
        icon: makeMarkerIcon('night'),
        title: pub.properties.name || '',
      }).addTo(map);

      marker._shadeStatus = 'night';
      markerMap[pubId] = marker;

      marker.on('click', () => {
        // Deselect previous
        if (selectedPubId && selectedPubId !== pubId) {
          const prev = markerMap[selectedPubId];
          if (prev) prev.setIcon(makeMarkerIcon(prev._shadeStatus || 'night'));
        }
        selectedPubId = pubId;
        marker.setIcon(makeMarkerIcon((marker._shadeStatus || 'night') + ' selected'));
        openPanel(pub);
      });
    }

    overlay.classList.add('hidden');

    // Kick off current-status refresh (best-effort, non-blocking)
    // Refresh every 5 minutes
    refreshCurrentStatus();
    setInterval(refreshCurrentStatus, 5 * 60 * 1000);

  } catch (err) {
    overlay.innerHTML = `<p style="color:#e05252">Failed to load pubs: ${err.message}</p>`;
  }
}

// ── Event listeners ───────────────────────────────────────────────────────

document.getElementById('panel-close').addEventListener('click', closePanel);

// Close panel on map click (not on marker)
map.on('click', closePanel);

// ── Boot ──────────────────────────────────────────────────────────────────

initPubs();
