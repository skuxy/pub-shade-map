/**
 * app.js — the whole map, client side.
 *
 * data/pubs.json holds, per venue, a horizon profile: the elevation angle up
 * to which surrounding roofs hide the sky, for every degree of compass
 * bearing, as seen from the venue's assumed terrace.  A venue is in shade
 * when the sun is lower than its horizon in the sun's direction:
 *
 *     shade  ⇔  0 < sunElevation < horizon[sunAzimuth]
 *
 * (same rule as backend/shadow/horizon.py:is_shaded).  The sun's position
 * comes from SunCalc, so any date and time is a table lookup — no server.
 *
 * Trees are separate, because the sun can shine under a crown: per bearing
 * the file lists elevation intervals covered by foliage (backend/shadow/
 * trees.py).  A venue the roofs leave in the sun is in tree shade when the
 * sun's elevation falls in one of them.  Deciduous trees count only while
 * they are in leaf.
 */

const { DateTime } = luxon;
const ZONE = 'Europe/Zagreb';
const CENTRE = [45.8131, 15.9772];     // Trg bana Jelačića
const STEP_MIN = 5;

// ── State ─────────────────────────────────────────────────────────────────
let pubs = [];
let nAz = 360;
let followNow = true;
let filter = 'all';
let selected = null;        // pub object
let terraceLayer = null;
let weather = null;         // from weather.js

const $ = (id) => document.getElementById(id);

// ── Sun ───────────────────────────────────────────────────────────────────

/** Sun azimuth (° clockwise from north) and elevation (°) over Zagreb. */
function sunAt(dt) {
  const p = SunCalc.getPosition(dt.toJSDate(), CENTRE[0], CENTRE[1]);
  return {
    az: ((p.azimuth * 180 / Math.PI) + 180 + 360) % 360,   // SunCalc: 0 = south, + west
    el: p.altitude * 180 / Math.PI,
  };
}

/** Deciduous trees in Zagreb carry leaves from about mid-April to the end of October. */
function inLeaf(dt) {
  const md = dt.month * 100 + dt.day;
  return md >= 415 && md <= 1031;
}

/** Flat [bin, low, high, …] triples → per-bin lists of [low, high] (tenths of a degree). */
function unpackFoliage(flat) {
  const bins = {};
  for (let k = 0; k + 2 < (flat || []).length; k += 3) {
    (bins[flat[k]] = bins[flat[k]] || []).push([flat[k + 1], flat[k + 2]]);
  }
  return bins;
}

const behind = (bins, i, el10) => (bins[i] || []).some(([lo, hi]) => lo <= el10 && el10 <= hi);

/**
 * 'sun' | 'tree' | 'shade' | 'night' for one venue given a sun position.
 * 'shade' is a roof in the way; 'tree' is foliage in the way and no roof.
 */
function statusAt(pub, sun, leaf) {
  if (sun.el <= 0) return 'night';
  const i = Math.floor(sun.az * nAz / 360 + 0.5) % nAz;
  const el10 = sun.el * 10;
  if (el10 < pub.horizon[i]) return 'shade';
  if (behind(pub.evergreenBins, i, el10) || (leaf && behind(pub.treeBins, i, el10))) return 'tree';
  return 'sun';
}

// ── Time controls ─────────────────────────────────────────────────────────

/** Zagreb wall-clock time *minutes* after 00:00 on *day* (correct on DST days). */
function wallTime(day, minutes) {
  return day.set({ hour: Math.floor(minutes / 60), minute: minutes % 60 });
}

function selectedTime() {
  return wallTime(DateTime.fromISO($('date-picker').value, { zone: ZONE }),
    Number($('time-slider').value));
}

function setTime(dt) {
  const local = dt.setZone(ZONE);
  $('date-picker').value = local.toFormat('yyyy-MM-dd');
  $('time-slider').value = Math.floor((local.hour * 60 + local.minute) / STEP_MIN) * STEP_MIN;
}

function setFollowNow(on) {
  followNow = on;
  $('now-btn').classList.toggle('active', on);
  if (on) setTime(DateTime.now());
  render();
}

// ── Map ───────────────────────────────────────────────────────────────────

const map = L.map('map', { center: CENTRE, zoom: 15, preferCanvas: true });
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    + ' · heights: <a href="https://zg3d-zagreb.hub.arcgis.com/">ZG3D, Grad Zagreb</a>',
  maxZoom: 19,
}).addTo(map);

// Keep the map filling its box however the box changes (panel, rotation, late layout).
new ResizeObserver(() => map.invalidateSize()).observe($('map'));

const STYLE = {
  sun:   { radius: 8, fillColor: '#ffc400', color: '#7a4b00', weight: 2,   fillOpacity: 1 },
  tree:  { radius: 6, fillColor: '#4f9a63', color: '#ffffff', weight: 1.5, fillOpacity: 1 },
  shade: { radius: 6, fillColor: '#4b5d82', color: '#ffffff', weight: 1.5, fillOpacity: 1 },
  // would be in direct sun, but the forecast for that hour is overcast
  cloud: { radius: 7, fillColor: '#efe2ae', color: '#9a8748', weight: 1.5, fillOpacity: 0.75 },
  night: { radius: 5, fillColor: '#1c2333', color: '#8c96ad', weight: 1,   fillOpacity: 0.9 },
};

function render() {
  const dt = selectedTime();
  const sun = sunAt(dt);
  const counts = { sun: 0, tree: 0, shade: 0, night: 0 };
  const cloudy = isCloudy(weather, dt);
  const leaf = inLeaf(dt);

  for (const pub of pubs) {
    const status = statusAt(pub, sun, leaf);
    counts[status]++;
    pub.status = status;
    const isSelected = pub === selected;
    const style = STYLE[cloudy && status === 'sun' ? 'cloud' : status];
    pub.marker.setStyle({
      ...style,
      ...(isSelected ? { weight: 4, color: '#e5322d', radius: style.radius + 2 } : {}),
    });
    const visible = filter === 'all' || filter === (status === 'tree' ? 'shade' : status) || isSelected;
    if (visible && !map.hasLayer(pub.marker)) pub.marker.addTo(map);
    if (!visible && map.hasLayer(pub.marker)) map.removeLayer(pub.marker);
    if (status === 'sun' || isSelected) pub.marker.bringToFront();
  }

  const shadeText = `${counts.shade + counts.tree} in shade`
    + (counts.tree ? ` (${counts.tree} under trees)` : '');
  $('time-label').textContent = dt.toFormat('HH:mm');
  $('counts').textContent = sun.el <= 0
    ? `${dt.toFormat('ccc d LLL, HH:mm')} — the sun is down`
    : cloudy
      ? `${dt.toFormat('ccc d LLL, HH:mm')} — overcast · ${counts.sun} would be in sun · ${shadeText}`
      : `${dt.toFormat('ccc d LLL, HH:mm')} — ${counts.sun} in sun · ${shadeText}`;

  $('legend-cloud').hidden = !cloudy;
  document.body.classList.toggle('night', sun.el <= 0);
  if (selected) renderPanel();
}

// ── Side panel ────────────────────────────────────────────────────────────

/** Status for every STEP_MIN minutes of wall-clock time on the selected Zagreb day. */
function dayTimeline(pub, day) {
  const steps = [];
  const leaf = inLeaf(day);
  for (let m = 0; m < 24 * 60; m += STEP_MIN) {
    steps.push(statusAt(pub, sunAt(wallTime(day, m)), leaf));
  }
  return steps;
}

function drawTimeline(steps, nowMinutes) {
  const canvas = $('timeline');
  const width = canvas.clientWidth || 320;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = 64 * ratio;
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);

  // Show daylight plus half an hour either side.
  const first = Math.max(0, steps.findIndex((s) => s !== 'night') - 6);
  const last = Math.min(steps.length, steps.length - [...steps].reverse().findIndex((s) => s !== 'night') + 6);
  const span = Math.max(1, last - first);
  const x = (i) => (i - first) / span * width;
  canvas.dataset.first = first;
  canvas.dataset.span = span;

  const colours = { sun: '#ffc400', tree: '#4f9a63', shade: '#4b5d82', night: '#1c2333' };
  for (let i = first; i < last; i++) {
    ctx.fillStyle = colours[steps[i]];
    ctx.fillRect(x(i), 0, Math.ceil(width / span) + 0.5, 44);
  }
  ctx.fillStyle = '#8891a5';
  ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center';
  const perHour = 60 / STEP_MIN;
  for (let h = Math.ceil(first / perHour); h * perHour < last; h++) {
    if (span / perHour > 10 && h % 2) continue;
    ctx.fillRect(x(h * perHour), 44, 1, 4);
    ctx.fillText(String(h).padStart(2, '0'), x(h * perHour), 60);
  }
  const nowX = x(nowMinutes / STEP_MIN);
  if (nowX >= 0 && nowX <= width) {
    ctx.fillStyle = '#e5322d';
    ctx.fillRect(nowX - 1, 0, 2, 48);
  }
}

function renderPanel() {
  const pub = selected;
  const dt = selectedTime();
  const day = dt.startOf('day');
  const steps = dayTimeline(pub, day);
  const nowIdx = Math.floor((dt.hour * 60 + dt.minute) / STEP_MIN);
  const status = steps[nowIdx];
  const hhmm = (i) => `${String(Math.floor(i * STEP_MIN / 60)).padStart(2, '0')}:${String(i * STEP_MIN % 60).padStart(2, '0')}`;

  // Badge: status now, and when it next changes.
  const TEXT = { sun: ['☀️', 'In the sun'], tree: ['🌳', 'In tree shade'], shade: ['⛱️', 'In the shade'], night: ['🌙', 'Sun is down'] };
  const THEN = { sun: 'sun', tree: 'tree shade', shade: 'shade' };
  $('status-badge').className = `badge ${status}`;
  const overcast = status === 'sun' && isCloudy(weather, dt);
  $('status-icon').textContent = overcast ? '☁️' : TEXT[status][0];
  $('status-text').textContent = `${overcast ? 'Sunny spot, but overcast' : TEXT[status][1]} at ${dt.toFormat('HH:mm')}`;
  let change = steps.findIndex((s, i) => i > nowIdx && s !== status);
  let next = '';
  if (status === 'night') {
    const firstSun = steps.findIndex((s, i) => i > nowIdx && s === 'sun');
    if (firstSun >= 0) next = `First sun at ${hhmm(firstSun)}`;
  } else if (change >= 0) {
    next = steps[change] === 'night' ? `until sunset at ${hhmm(change)}`
      : `until ${hhmm(change)}, then ${THEN[steps[change]]}`;
  }
  $('status-next').textContent = next;

  // Timeline + summary.
  $('timeline-date').textContent = dt.toFormat('cccc d LLLL');
  drawTimeline(steps, dt.hour * 60 + dt.minute);
  const sunSteps = steps.filter((s) => s === 'sun').length;
  const treeSteps = steps.filter((s) => s === 'tree').length;
  const dayStepsCount = steps.filter((s) => s !== 'night').length;
  const firstSun = steps.indexOf('sun');
  const lastSun = steps.lastIndexOf('sun');
  $('timeline-summary').textContent = sunSteps === 0
    ? 'No direct sun on this day.'
    : `${(sunSteps * STEP_MIN / 60).toFixed(1)} h of direct sun (of ${(dayStepsCount * STEP_MIN / 60).toFixed(1)} h daylight), `
      + `first ${hhmm(firstSun)}, last ${hhmm(lastSun + 1)}`;
  if (treeSteps) {
    $('timeline-summary').textContent += ` · ${(treeSteps * STEP_MIN / 60).toFixed(1)} h behind trees`;
  }
  const hasTrees = pub.trees.length || pub.evergreens.length;
  $('tree-note').hidden = !hasTrees;
  if (hasTrees) {
    $('tree-note').textContent = !pub.trees.length ? '🌳 Evergreen trees nearby are counted all year.'
      : inLeaf(dt) ? '🌳 Trees nearby are counted as in leaf (mid-April to October). Tree shade is dappled, not solid.'
        : '🌳 Deciduous trees nearby are bare on this date and not counted.';
  }

  $('cloud-note').hidden = !isCloudy(weather, dt);
}

function openPanel(pub) {
  selected = pub;
  $('panel').classList.remove('panel-hidden');
  $('pub-name').textContent = pub.name;

  const meta = [pub.amenity];
  if (pub.address) meta.push(pub.address);
  if (pub.seating && pub.seating !== 'no') meta.push('outdoor seating');
  if (pub.hours) meta.push(`open ${pub.hours}`);
  $('pub-meta').textContent = meta.join(' · ');

  $('terrace-note').textContent = {
    facade: '📍 Measured at the red dot: 3 m outside the building, on what looks like the street side. '
      + 'If the terrace is on another side, the result can differ a lot.',
    open: '📍 Measured exactly where the venue is mapped (already outdoors).',
    indoor: '📍 No open spot found next to this building; measured at the mapped point, ignoring the building itself.',
  }[pub.kind];

  if (terraceLayer) map.removeLayer(terraceLayer);
  terraceLayer = L.layerGroup([
    L.polyline([[pub.lat, pub.lon], pub.terrace], { color: '#e5322d', weight: 2, dashArray: '3 4' }),
    L.circleMarker(pub.terrace, { radius: 3, color: '#e5322d', fillColor: '#e5322d', fillOpacity: 1 }),
  ]).addTo(map);

  render();
  setTimeout(() => { map.invalidateSize(); if (selected) renderPanel(); }, 260);   // after the panel slide
}

function closePanel() {
  selected = null;
  $('panel').classList.add('panel-hidden');
  if (terraceLayer) { map.removeLayer(terraceLayer); terraceLayer = null; }
  render();
  setTimeout(() => map.invalidateSize(), 260);
}

// ── Wiring ────────────────────────────────────────────────────────────────

function showBanner(text) {
  $('banner').textContent = text;
  $('banner').hidden = false;
}

async function init() {
  setTime(DateTime.now());

  $('time-slider').addEventListener('input', () => setFollowNow(false));
  $('date-picker').addEventListener('change', () => {
    if (!$('date-picker').value) setTime(DateTime.now());
    setFollowNow(false);
  });
  $('now-btn').addEventListener('click', () => setFollowNow(true));
  document.querySelectorAll('.filter-btn').forEach((btn) => btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach((b) => b.classList.toggle('active', b === btn));
    filter = btn.dataset.filter;
    render();
  }));
  $('panel-close').addEventListener('click', closePanel);
  map.on('click', closePanel);
  $('timeline').addEventListener('click', (ev) => {
    const c = $('timeline');
    const idx = Number(c.dataset.first) + ev.offsetX / c.clientWidth * Number(c.dataset.span);
    $('time-slider').value = Math.max(0, Math.min(1435, Math.round(idx) * STEP_MIN));
    setFollowNow(false);
  });
  window.addEventListener('resize', () => selected && renderPanel());
  setInterval(() => { if (followNow) { setTime(DateTime.now()); render(); } }, 30 * 1000);

  try {
    const resp = await fetch('data/pubs.json');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    nAz = data.azimuths;
    pubs = data.pubs;
  } catch (err) {
    $('counts').textContent = '';
    showBanner(`Could not load venue data (${err.message}).`);
    return;
  }

  for (const pub of pubs) {
    pub.trees = pub.trees || [];
    pub.evergreens = pub.evergreens || [];
    pub.treeBins = unpackFoliage(pub.trees);
    pub.evergreenBins = unpackFoliage(pub.evergreens);
    pub.marker = L.circleMarker([pub.lat, pub.lon], STYLE.night)
      .bindTooltip(() => { const el = document.createElement('span'); el.textContent = pub.name; return el; }, { direction: 'top', offset: [0, -6] })
      .on('click', (ev) => { L.DomEvent.stopPropagation(ev); openPanel(pub); })
      .addTo(map);
  }
  render();

  weather = await loadWeather(CENTRE[0], CENTRE[1]);
  renderWeather('weather-container', weather);
  render();
}

init();
