/**
 * timeline.js — render a sun/shade bar chart for a pub's daily timeline.
 *
 * Uses Chart.js horizontal bar chart where each segment represents a
 * 5-minute block, coloured by in_shade (dark) or in sun (yellow).
 */

let timelineChart = null;

/**
 * Convert a UTC ISO string to Zagreb local time label "HH:MM".
 */
function toZagrebTime(isoUtc) {
  return luxon.DateTime
    .fromISO(isoUtc, { zone: 'utc' })
    .setZone('Europe/Zagreb')
    .toFormat('HH:mm');
}

/**
 * Build Chart.js dataset from timeline entries.
 *
 * We model the timeline as a single horizontal stacked bar chart where
 * each data point is a 5-minute segment with a background colour of
 * sun-yellow or shade-grey.
 *
 * @param {Array} timeline — array of {time, in_shade, sun_azimuth, sun_elevation}
 * @returns {Object} Chart.js data config
 */
function buildChartData(timeline) {
  // One segment per step; each has duration 5 min (normalised to 1 unit)
  const labels = timeline.map(t => toZagrebTime(t.time));
  const data = timeline.map(() => 1);   // uniform width
  const colors = timeline.map(t => t.in_shade ? '#3b4a6b' : '#f6c90e');
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

/**
 * Draw (or re-draw) the shade timeline chart.
 *
 * @param {string} canvasId — ID of the <canvas> element
 * @param {Array}  timeline — timeline data from /api/shade
 * @param {string} dateStr  — YYYY-MM-DD (for current-time marker)
 */
function renderTimeline(canvasId, timeline, dateStr) {
  const canvas = document.getElementById(canvasId);
  const loadingEl = document.getElementById('timeline-loading');

  if (!timeline || timeline.length === 0) {
    loadingEl.textContent = 'No daylight data available for this date.';
    canvas.style.display = 'none';
    return;
  }

  loadingEl.style.display = 'none';
  canvas.style.display = 'block';

  // Destroy previous instance
  if (timelineChart) {
    timelineChart.destroy();
    timelineChart = null;
  }

  const chartData = buildChartData(timeline);

  // Determine current time marker index (Zagreb time)
  const now = luxon.DateTime.now().setZone('Europe/Zagreb');
  const todayStr = now.toFormat('yyyy-MM-dd');
  let nowIndex = -1;
  if (dateStr === todayStr) {
    const nowLabel = now.toFormat('HH:mm');
    nowIndex = chartData.labels.findLastIndex(l => l <= nowLabel);
  }

  // Current-time marker plugin
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
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  // Show only every ~hour label to avoid crowding (one per 12 steps at 5 min)
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
              const t = timeline[idx];
              const localTime = toZagrebTime(t.time);
              return `${localTime} (Zagreb)`;
            },
            label: (item) => {
              const t = timeline[item.dataIndex];
              const status = t.in_shade ? '🌑 In shade' : '☀️ In sun';
              return [
                status,
                `Azimuth: ${t.sun_azimuth}°`,
                `Elevation: ${t.sun_elevation}°`,
              ];
            },
          },
          backgroundColor: '#1a1d27',
          borderColor: '#2e3350',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#8891a5',
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
          grid: { color: '#2e3350', drawBorder: false },
          border: { color: '#2e3350' },
        },
        y: {
          display: false,
          stacked: true,
        },
      },
    },
    plugins: [nowLinePlugin],
  });

  // Fixed height so chart is readable
  canvas.parentElement.style.height = '72px';
}
