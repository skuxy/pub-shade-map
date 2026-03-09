# Frontend

Vanilla JS + Leaflet.js interactive map. No build step required — served
directly by the FastAPI backend at `http://localhost:8000`.

## Structure

```
frontend/
├── index.html       # Single-page app shell
├── css/
│   └── style.css    # Dark theme styles
└── js/
    ├── map.js       # Leaflet map, pub markers, popup logic
    ├── timeline.js  # Sun/shade timeline chart (Chart.js)
    └── weather.js   # 3-day weather forecast widget
```

## Pub Marker Colours

| Colour | Meaning |
|--------|---------|
| Yellow / orange | Pub is currently in sunlight |
| Dark grey | Pub is currently in shade |
| Blue | Night / sun below horizon |

## External Dependencies (CDN)

- [Leaflet.js](https://leafletjs.com/) — map rendering
- [Chart.js](https://www.chartjs.org/) — shade timeline bar chart
- [Luxon](https://moment.github.io/luxon/) — timezone-aware datetime formatting
