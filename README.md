# Zagreb Pub Shade Map

Interactive map showing when Zagreb pubs and bars are in sun or shade throughout the day, combined with a 3-day weather forecast.

## Features

- All pubs, bars, and cafes with outdoor seating in Zagreb from OpenStreetMap
- 2.5D shadow casting using building footprints and heights at **5-minute resolution**
- Visual sun/shade timeline per pub for today and the next 3 days
- 3-day weather forecast from Open-Meteo (free, no API key required)
- Interactive Leaflet.js map with dark theme
- Markers coloured by current shade status (yellow = sun, grey = shade)

## How It Works

1. Building footprints and heights are fetched from OpenStreetMap and cached locally
2. Sun position (azimuth + elevation angle) is computed for Zagreb at 5-minute intervals using `pysolar`
3. For each time step, a shadow polygon is projected from each building footprint opposite the sun:
   - `shadow_length = building_height / tan(sun_elevation)`
   - Shadow polygon = convex hull of footprint vertices + offset vertices
4. Each pub is checked against shadow polygons of nearby buildings (≤ 200 m radius)
5. Results are served via FastAPI and visualised on a Leaflet map

## Data Sources

| Data | Source |
|------|--------|
| Pubs / bars | OpenStreetMap via [Overpass API](https://overpass-api.de/) |
| Building footprints & heights | OpenStreetMap (height / building:levels tags) |
| Sun position | [pysolar](https://pysolar.org/) |
| Weather forecast | [Open-Meteo](https://open-meteo.com/) |

### Upgrading to Official Zagreb 3D Data

The City of Zagreb publishes high-accuracy **LoD 2.2** 3D building models via ArcGIS Scene
Services (2022–2023 multisensor survey). These cover all 15 city districts and include precise
Z_Min / Z_Max elevation per building mesh. Switching to this data source would meaningfully
improve shadow accuracy.

- Hub: <https://zg3d-zagreb.hub.arcgis.com/>
- Base URL: `https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services/ZG3D_GC_{district}_2022/SceneServer`
- Districts: `Pescenica_Zitnjak`, `Gornji_Grad`, `Novi_Zagreb_zapad`, `Trnje`, `Brezovica`,
  `Novi_Zagreb_istok`, `Donja_Dubrava`, `Tresnjevka_jug`, `Crnomerec`, `Sesvete`,
  `Podsused_Vrapce`, `Gornja_Dubrava`, `Donji_Grad`, `Maksimir`, `Podsljeme`

See `backend/data_pipeline/fetch_buildings.py` for integration notes.

## Quick Start

```bash
# 1. Set up backend
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Fetch OSM data (run once, ~1-2 minutes)
python scripts/fetch_data.py

# 3. Start the API server (also serves the frontend)
uvicorn api.main:app --reload --port 8000

# 4. Open http://localhost:8000 in your browser
```

## Architecture

```
pub-shade-map/
├── backend/
│   ├── data_pipeline/   # OSM data fetching & caching
│   ├── shadow/          # solar position + shadow geometry engine
│   ├── api/             # FastAPI app
│   └── scripts/         # CLI tools (fetch data, pre-compute shade)
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/              # map.js, timeline.js, weather.js
└── data/                # cached GeoJSON + shade timelines (git-ignored)
```

## Limitations & Future Work

- OSM building heights are incomplete; switching to ArcGIS ZG3D data would improve accuracy
- Shadow model is 2.5D (extruded footprints), not full 3D mesh ray-tracing
- No terrain/elevation model — all buildings treated as on flat ground
- Shade timelines are computed on demand; pre-computation script available for performance
