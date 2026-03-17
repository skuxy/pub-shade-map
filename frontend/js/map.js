/**
 * map.js — Leaflet map initialisation, pub markers, and panel logic.
 *
 * Flow:
 *  1. Initialise Leaflet map on Zagreb.
 *  2. Fetch /api/pubs → render pub markers (includes sunny_score_today when
 *     the precompute cache is warm).
 *  3. Call /api/current-status once on load (and every 5 min) to colour all
 *     markers from a single round-trip instead of one /api/shade call per pub.
 *  4. On marker click → open side panel, fetch shade timeline + weather.
 *  5. Date picker change → re-fetch shade timeline.
 *  6. Filter buttons (All / In Sun / In Shade) show/hide markers in real time.
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
let allPubs     = [];
let markerMap   = {};   // pub_id → Leaflet marker
let selectedPubId = null;
let activeFilter  = 'all';   // 'all' | 'sun' | 'shade'
let weatherDays   = [];      // day summaries from last weather fetch

// ── Helpers ───────────────────────────────────────────────────────────────

function todayZagreb() {
  return luxon.DateTime.now().setZone('Europe/Zagreb').toFormat('yyyy-MM-dd');
}

function makeMarkerIcon(status) {
  // status: 'sun' | 'shade' | 'night' | 'unknown'  (+ optional ' selected')
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
  const selected = pubId === selectedPubId ? ' selected' : '';
  marker.setIcon(makeMarkerIcon(status + selected));
  _applyFilter(marker, status);
}

/**
 * Show or hide a marker based on the current filter.
 */
function _applyFilter(marker, status) {
  if (activeFilter === 'all') {
    if (!map.hasLayer(marker)) marker.addTo(map);
  } else if (activeFilter === 'sun') {
    if (status === 'sun') {
      if (!map.hasLayer(marker)) marker.addTo(map);
    } else {
      if (map.hasLayer(marker)) map.removeLayer(marker);
    }
  } else if (activeFilter === 'shade') {
    if (status === 'shade') {
      if (!map.hasLayer(marker)) marker.addTo(map);
    } else {
      if (map.hasLayer(marker)) map.removeLayer(marker);
    }
  }
}

function applyFilterToAll() {
  for (const pub of allPubs) {
    const pubId  = pub.properties.id;
    const marker = markerMap[pubId];
    if (!marker) continue;
    _applyFilter(marker, marker._shadeStatus || 'night');
  }
}

// ── Current-time shade status (single call for all pubs) ─────────────────

/**
 * Fetch /api/current-status (one round-trip) and update every marker.
 * Replaces the previous approach of one /api/shade request per pub.
 */
async function refreshCurrentStatus() {
  try {
    const resp = await fetch(`${API_BASE}/api/current-status`);
    if (!resp.ok) return;
    const { pubs } = await resp.json();

    for (const [pubId, info] of Object.entries(pubs)) {
      // Skip pubs whose timeline hasn't been computed yet — don't override
      // the initial marker colour with a misleading 'night' dot.
      if (info.status !== 'unknown') {
        setMarkerStatus(pubId, info.status);
      }
    }
  } catch (_) {
    // Non-fatal — markers stay at last known status.
  }
}

// ── Panel ─────────────────────────────────────────────────────────────────

function openPanel(pub) {
  const panel = document.getElementById('panel');
  panel.classList.remove('panel-hidden');

  document.getElementById('pub-name').textContent    = pub.properties.name    || 'Unnamed pub';
  document.getElementById('pub-amenity').textContent = pub.properties.amenity || 'venue';

  // Show sunny score if already computed.
  const scoreEl = document.getElementById('pub-sunny-score');
  const score   = pub.properties.sunny_score_today;
  if (score != null) {
    scoreEl.textContent = `${score}% sun today`;
    scoreEl.className   = 'sunny-score ' + (score >= 60 ? 'sunny-high' : score >= 30 ? 'sunny-mid' : 'sunny-low');
    scoreEl.style.display = 'inline-block';
  } else {
    scoreEl.style.display = 'none';
  }

  // Reset loading states.
  document.getElementById('timeline-loading').textContent = 'Loading…';
  document.getElementById('timeline-loading').style.display = 'block';
  document.getElementById('timeline-chart').style.display   = 'none';
  document.getElementById('weather-loading').textContent    = 'Loading…';
  document.getElementById('weather-loading').style.display  = 'block';
  document.getElementById('weather-container').innerHTML    = '';
  document.getElementById('status-badge').className = 'badge badge-hidden';
  weatherDays = [];
  applyCloudOverlay();  // clear any previous overlay while new weather loads

  const [lon, lat] = pub.geometry.coordinates;
  const pubId = pub.properties.id;

  const datePicker = document.getElementById('date-picker');
  datePicker.value = todayZagreb();
  datePicker.max   = luxon.DateTime.now().setZone('Europe/Zagreb').plus({ days: 3 }).toFormat('yyyy-MM-dd');
  datePicker.min   = todayZagreb();

  loadShade(pubId, datePicker.value, pub.properties.opening_hours);
  loadWeather(lat, lon);

  datePicker.onchange = () => {
    loadShade(pubId, datePicker.value, pub.properties.opening_hours);
    applyCloudOverlay();
  };
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

async function loadShade(pubId, dateStr, openingHours) {
  document.getElementById('timeline-loading').textContent = 'Loading timeline…';
  document.getElementById('timeline-loading').style.display = 'block';
  document.getElementById('timeline-chart').style.display   = 'none';
  document.getElementById('status-badge').className = 'badge badge-hidden';

  try {
    const resp = await fetch(`${API_BASE}/api/shade/${encodeURIComponent(pubId)}?date=${dateStr}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    renderTimeline('timeline-chart', data.timeline, dateStr, data.sunny_pct, openingHours);
    updateStatusBadge(data.timeline, dateStr);
    applyCloudOverlay();

    // Update the sunny score badge in the panel if we now have fresher data.
    if (data.sunny_pct != null && dateStr === todayZagreb()) {
      const scoreEl = document.getElementById('pub-sunny-score');
      const s = data.sunny_pct;
      scoreEl.textContent = `${s}% sun today`;
      scoreEl.className   = 'sunny-score ' + (s >= 60 ? 'sunny-high' : s >= 30 ? 'sunny-mid' : 'sunny-low');
      scoreEl.style.display = 'inline-block';
    }
  } catch (err) {
    document.getElementById('timeline-loading').textContent = `Error loading timeline: ${err.message}`;
  }
}

function updateStatusBadge(timeline, dateStr) {
  const badge = document.getElementById('status-badge');
  const icon  = document.getElementById('status-icon');
  const text  = document.getElementById('status-text');

  const now      = luxon.DateTime.now().setZone('Europe/Zagreb');
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

// ── Cloudy weather overlay ────────────────────────────────────────────────

/**
 * Add or remove the cloudy tint on the timeline and status badge based on
 * the weather forecast for the currently selected date.
 *
 * "Cloudy" = WMO code ≥ 3 (overcast / fog / precipitation) or cloud cover ≥ 70 %.
 */
function applyCloudOverlay() {
  const dateStr = document.getElementById('date-picker').value;
  const dayData = weatherDays.find(d => d.date === dateStr);
  const cloudy  = dayData && (dayData.code >= 3 || dayData.cloudAvg >= 70);

  const timelineEl = document.getElementById('timeline-section');
  const badgeEl    = document.getElementById('status-badge');
  const noteEl     = document.getElementById('cloud-note');

  if (timelineEl) timelineEl.classList.toggle('cloudy-weather', !!cloudy);
  if (badgeEl)    badgeEl.classList.toggle('cloudy-weather',    !!cloudy);
  if (noteEl)     noteEl.style.display = cloudy ? 'block' : 'none';
}

// ── Load weather ──────────────────────────────────────────────────────────

async function loadWeather(lat, lon) {
  const data = await fetchWeather(lat, lon);
  weatherDays = renderWeather('weather-container', data) || [];
  applyCloudOverlay();
}

// ── Filter controls ───────────────────────────────────────────────────────

function initFilterControls() {
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeFilter = btn.dataset.filter;
      applyFilterToAll();
    });
  });
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

      // Initial colour: sun/shade from sunny_score if available, else night.
      let initialStatus = 'night';
      if (pub.properties.sunny_score_today != null) {
        initialStatus = pub.properties.sunny_score_today > 0 ? 'sun' : 'shade';
      }

      const marker = L.marker([lat, lon], {
        icon: makeMarkerIcon(initialStatus),
        title: pub.properties.name || '',
      }).addTo(map);

      marker._shadeStatus = initialStatus;
      markerMap[pubId] = marker;

      marker.on('click', () => {
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

    // Fetch accurate current statuses (single API call).
    refreshCurrentStatus();
    setInterval(refreshCurrentStatus, 5 * 60 * 1000);

  } catch (err) {
    overlay.innerHTML = `<p style="color:#e05252">Failed to load pubs: ${err.message}</p>`;
  }
}

// ── Event listeners ───────────────────────────────────────────────────────

document.getElementById('panel-close').addEventListener('click', closePanel);
map.on('click', closePanel);

// ── Boot ──────────────────────────────────────────────────────────────────

initFilterControls();
initPubs();
