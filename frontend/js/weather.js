/**
 * weather.js — fetch and render a 3-day hourly weather forecast from
 * the backend /api/weather proxy (Open-Meteo).
 */

// WMO Weather Interpretation Codes → human label + emoji
const WMO_CODES = {
  0:  { label: 'Clear sky',          icon: '☀️' },
  1:  { label: 'Mainly clear',       icon: '🌤️' },
  2:  { label: 'Partly cloudy',      icon: '⛅' },
  3:  { label: 'Overcast',           icon: '☁️' },
  45: { label: 'Foggy',              icon: '🌫️' },
  48: { label: 'Icy fog',            icon: '🌫️' },
  51: { label: 'Light drizzle',      icon: '🌦️' },
  53: { label: 'Drizzle',            icon: '🌦️' },
  55: { label: 'Heavy drizzle',      icon: '🌧️' },
  61: { label: 'Light rain',         icon: '🌧️' },
  63: { label: 'Rain',               icon: '🌧️' },
  65: { label: 'Heavy rain',         icon: '🌧️' },
  71: { label: 'Light snow',         icon: '🌨️' },
  73: { label: 'Snow',               icon: '❄️' },
  75: { label: 'Heavy snow',         icon: '❄️' },
  80: { label: 'Rain showers',       icon: '🌦️' },
  81: { label: 'Rain showers',       icon: '🌧️' },
  82: { label: 'Violent showers',    icon: '⛈️' },
  95: { label: 'Thunderstorm',       icon: '⛈️' },
  96: { label: 'Thunderstorm + hail',icon: '⛈️' },
  99: { label: 'Thunderstorm + hail',icon: '⛈️' },
};

function wmoLabel(code) {
  return WMO_CODES[code] || { label: 'Unknown', icon: '❓' };
}

/**
 * Fetch 3-day weather forecast for a lat/lon from the backend proxy.
 * @returns {Promise<Object|null>} Open-Meteo response or null on error.
 */
async function fetchWeather(lat, lon) {
  try {
    const resp = await fetch(`/api/weather?lat=${lat}&lon=${lon}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (err) {
    console.error('Weather fetch error:', err);
    return null;
  }
}

/**
 * Group hourly weather data by calendar date and compute daily summaries.
 * Returns an array of up to 3 day objects.
 */
function groupByDay(data) {
  const hourly = data.hourly;
  const times = hourly.time;         // "2024-06-15T10:00"
  const temps = hourly.temperature_2m;
  const precip = hourly.precipitation_probability;
  const codes = hourly.weathercode;
  const clouds = hourly.cloudcover;
  const wind = hourly.windspeed_10m;

  const days = {};

  for (let i = 0; i < times.length; i++) {
    const dateStr = times[i].split('T')[0];
    if (!days[dateStr]) {
      days[dateStr] = { temps: [], precip: [], codes: [], clouds: [], wind: [] };
    }
    days[dateStr].temps.push(temps[i]);
    days[dateStr].precip.push(precip[i]);
    days[dateStr].codes.push(codes[i]);
    days[dateStr].clouds.push(clouds[i]);
    days[dateStr].wind.push(wind[i]);
  }

  return Object.entries(days).slice(0, 3).map(([dateStr, d]) => {
    const avg = arr => arr.reduce((a, b) => a + b, 0) / arr.length;
    const max = arr => Math.max(...arr);
    const min = arr => Math.min(...arr);

    // Pick the most common weather code in the daytime hours (6-18)
    const midCodes = d.codes.slice(6, 18);
    const dominantCode = midCodes.sort(
      (a, b) => midCodes.filter(v => v === a).length - midCodes.filter(v => v === b).length
    ).pop();

    return {
      date: dateStr,
      tempMin: Math.round(min(d.temps)),
      tempMax: Math.round(max(d.temps)),
      precipMax: Math.round(max(d.precip)),
      cloudAvg: Math.round(avg(d.clouds)),
      windMax: Math.round(max(d.wind)),
      code: dominantCode,
    };
  });
}

/**
 * Render weather forecast cards into containerId.
 */
function renderWeather(containerId, weatherData) {
  const container = document.getElementById(containerId);
  const loading = document.getElementById('weather-loading');

  if (!weatherData) {
    loading.textContent = 'Weather unavailable.';
    return;
  }

  loading.style.display = 'none';
  container.innerHTML = '';

  const days = groupByDay(weatherData);
  const DateTime = luxon.DateTime;

  days.forEach((day, idx) => {
    const dt = DateTime.fromISO(day.date, { zone: 'Europe/Zagreb' });
    const { label, icon } = wmoLabel(day.code);
    const dayName = idx === 0 ? 'Today'
                  : idx === 1 ? 'Tomorrow'
                  : dt.toFormat('cccc');   // e.g. "Wednesday"

    const card = document.createElement('div');
    card.className = 'weather-day';
    card.innerHTML = `
      <div class="weather-day-header">
        <span class="weather-day-name">${dayName} <small style="color:var(--text-muted);font-weight:400">${dt.toFormat('d MMM')}</small></span>
        <span class="weather-icon" title="${label}">${icon}</span>
      </div>
      <div class="weather-stats">
        <span>🌡️ <strong>${day.tempMin}° – ${day.tempMax}°C</strong></span>
        <span>💧 <strong>${day.precipMax}%</strong> precip</span>
        <span>☁️ <strong>${day.cloudAvg}%</strong> cloud</span>
        <span>💨 <strong>${day.windMax} km/h</strong></span>
      </div>
    `;
    container.appendChild(card);
  });
}
