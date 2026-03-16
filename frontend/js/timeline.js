/**
 * timeline.js — render a sun/shade bar chart for a pub's daily timeline.
 *
 * Uses Chart.js horizontal bar chart where each segment represents a
 * 5-minute block, coloured by in_shade (dark) or in sun (yellow).
 *
 * Below the chart a one-line summary shows:
 *   - Total hours in sun / shade
 *   - "Opens HH:MM – closes HH:MM" parsed from the OSM opening_hours string
 *     (common patterns only; falls back to raw string for complex expressions)
 */

let timelineChart = null;

// ── Time helpers ──────────────────────────────────────────────────────────

/**
 * Convert a UTC ISO string to Zagreb local time label "HH:MM".
 */
function toZagrebTime(isoUtc) {
  return luxon.DateTime
    .fromISO(isoUtc, { zone: 'utc' })
    .setZone('Europe/Zagreb')
    .toFormat('HH:mm');
}

// ── Opening hours parser ──────────────────────────────────────────────────

/**
 * Parse a subset of the OSM opening_hours DSL and return a human-readable
 * string for today's hours.
 *
 * Handles the most common patterns:
 *   "09:00-23:00"                 → "09:00 – 23:00"
 *   "24/7"                        → "Open 24 / 7"
 *   "Mo-Fr 10:00-22:00"           → "10:00 – 22:00"  (if today matches)
 *   "Mo-Sa 10:00-02:00; Su 12:00-00:00"  → per-day lookup
 *
 * Returns null for strings that can't be parsed, so callers can fall back to
 * showing the raw value.
 *
 * @param {string} raw  — raw OSM opening_hours value
 * @returns {string|null}
 */
function parseOpeningHours(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const s = raw.trim();

  if (s === '24/7') return 'Open 24 / 7';

  const DAY_NAMES = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
  const todayIdx  = new Date().getDay();   // 0=Sun … 6=Sat

  // Split on semicolons into rule segments and find the first that covers today.
  const segments = s.split(';').map(p => p.trim()).filter(Boolean);

  for (const seg of segments) {
    // Match optional day spec + time range, e.g. "Mo-Fr 09:00-23:00"
    const m = seg.match(
      /^(?:([A-Za-z]{2})(?:-([A-Za-z]{2}))?\s+)?(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$/
    );
    if (!m) continue;

    const [, dayFrom, dayTo, timeOpen, timeClose] = m;

    if (!dayFrom) {
      // No day spec → applies every day.
      return `${timeOpen} – ${timeClose}`;
    }

    const fromIdx = DAY_NAMES.indexOf(dayFrom);
    const toIdx   = dayTo ? DAY_NAMES.indexOf(dayTo) : fromIdx;

    if (fromIdx < 0) continue;

    // Handle ranges that wrap around the week (e.g. Fr-Mo).
    const coversToday = fromIdx <= toIdx
      ? todayIdx >= fromIdx && todayIdx <= toIdx
      : todayIdx >= fromIdx || todayIdx <= toIdx;

    if (coversToday) return `${timeOpen} – ${timeClose}`;
  }

  // Nothing matched; return raw string truncated.
  return s.length <= 40 ? s : s.slice(0, 37) + '…';
}

// ── Chart helpers ─────────────────────────────────────────────────────────

/**
 * Build Chart.js dataset from timeline entries.
 *
 * One segment per 5-minute step; uniform width; background colour indicates
 * sun (yellow) or shade (blue-grey).
 *
 * @param {Array} timeline — [{time, in_shade, sun_azimuth, sun_elevation}, …]
 * @returns {Object} Chart.js data config
 */
function buildChartData(timeline) {
  const labels  = timeline.map(t => toZagrebTime(t.time));
  const data    = timeline.map(() => 1);
  const colors  = timeline.map(t => t.in_shade ? '#3b4a6b' : '#f6c90e');
  const borders = timeline.map(t => t.in_shade ? '#2e3a5a' : '#d4a900');

  return {
    labels,
    datasets: [{
      data,
      backgroundColor: colors,
      borderColor: borders,
      borderWidth: 0,
      borderSkipped: false,
    }],
  };
}

// ── Main render function ──────────────────────────────────────────────────

/**
 * Draw (or re-draw) the shade timeline chart.
 *
 * @param {string}      canvasId     — ID of the <canvas> element
 * @param {Array}       timeline     — timeline data from /api/shade
 * @param {string}      dateStr      — YYYY-MM-DD (for current-time marker)
 * @param {number|null} sunnyPct     — % of daylight in sun (0–100), or null
 * @param {string|null} openingHours — raw OSM opening_hours string, or null
 */
function renderTimeline(canvasId, timeline, dateStr, sunnyPct, openingHours) {
  const canvas    = document.getElementById(canvasId);
  const loadingEl = document.getElementById('timeline-loading');
  const summaryEl = document.getElementById('timeline-summary');

  if (!timeline || timeline.length === 0) {
    loadingEl.textContent = 'No daylight data available for this date.';
    canvas.style.display  = 'none';
    if (summaryEl) summaryEl.textContent = '';
    return;
  }

  loadingEl.style.display = 'none';
  canvas.style.display    = 'block';

  if (timelineChart) {
    timelineChart.destroy();
    timelineChart = null;
  }

  const chartData = buildChartData(timeline);

  // ── Current-time marker ───────────────────────────────────────────────
  const now      = luxon.DateTime.now().setZone('Europe/Zagreb');
  const todayStr = now.toFormat('yyyy-MM-dd');
  let nowIndex   = -1;
  if (dateStr === todayStr) {
    const nowLabel = now.toFormat('HH:mm');
    nowIndex = chartData.labels.findLastIndex(l => l <= nowLabel);
  }

  const nowLinePlugin = {
    id: 'nowLine',
    afterDraw(chart) {
      if (nowIndex < 0) return;
      const meta = chart.getDatasetMeta(0);
      if (!meta.data[nowIndex]) return;
      const bar = meta.data[nowIndex];
      const { ctx } = chart;
      const x = bar.x + bar.width / 2;
      const { top, bottom } = chart.chartArea;
      ctx.save();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth   = 2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  // Show only every ~hour label to avoid crowding (one per 12 steps at 5 min).
  const tickLabels = chartData.labels.map((l, i) => (i % 12 === 0 ? l : ''));

  timelineChart = new Chart(canvas, {
    type: 'bar',
    data: chartData,
    options: {
      indexAxis: 'x',
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => {
              const idx = items[0].dataIndex;
              const t   = timeline[idx];
              return `${toZagrebTime(t.time)} (Zagreb)`;
            },
            label: (item) => {
              const t      = timeline[item.dataIndex];
              const status = t.in_shade ? '🌑 In shade' : '☀️ In sun';
              return [
                status,
                `Azimuth: ${t.sun_azimuth}°`,
                `Elevation: ${t.sun_elevation}°`,
              ];
            },
          },
          backgroundColor: '#1a1d27',
          borderColor:     '#2e3350',
          borderWidth:     1,
          titleColor:      '#e2e8f0',
          bodyColor:       '#8891a5',
        },
      },
      scales: {
        x: {
          stacked: true,
          ticks: {
            color: '#8891a5',
            font: { size: 11 },
            maxRotation: 0,
            callback: (_, i) => tickLabels[i],
          },
          grid:   { color: '#2e3350', drawBorder: false },
          border: { color: '#2e3350' },
        },
        y: { display: false, stacked: true },
      },
    },
    plugins: [nowLinePlugin],
  });

  canvas.parentElement.style.height = '72px';

  // ── Summary line ──────────────────────────────────────────────────────
  if (summaryEl) {
    const parts = [];

    if (sunnyPct != null) {
      const stepMins   = 5;
      const totalSteps = timeline.length;
      const sunSteps   = Math.round(totalSteps * sunnyPct / 100);
      const sunH       = (sunSteps * stepMins / 60).toFixed(1);
      const shadeH     = ((totalSteps - sunSteps) * stepMins / 60).toFixed(1);
      parts.push(`☀️ ${sunH} h sun  🌑 ${shadeH} h shade`);
    }

    if (openingHours) {
      const parsed = parseOpeningHours(openingHours);
      if (parsed) parts.push(`🕐 ${parsed}`);
    }

    summaryEl.textContent = parts.join('   ·   ');
  }
}
