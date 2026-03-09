# Backend

Python + FastAPI backend for shadow computation and data serving.

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Data Pipeline

```bash
# Fetch OSM pubs and buildings — run once, takes ~1-2 minutes
python scripts/fetch_data.py

# Optional: pre-compute shade timelines for today + next N days
python scripts/precompute.py --days 3
```

## Running the API

```bash
uvicorn api.main:app --reload --port 8000
```

The API also serves the frontend at `http://localhost:8000`.

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pubs` | All pubs as GeoJSON FeatureCollection |
| GET | `/api/shade/{pub_id}?date=YYYY-MM-DD` | Shade timeline for a pub on a date |
| GET | `/api/weather?lat=&lon=` | 3-day weather forecast (proxied from Open-Meteo) |
| GET | `/health` | Health check |

### `GET /api/shade/{pub_id}`

Query params:
- `date` (optional): `YYYY-MM-DD` in Zagreb local time. Defaults to today.

Response:
```json
{
  "pub_id": "node/123456",
  "date": "2024-06-15",
  "timeline": [
    {
      "time": "2024-06-15T04:30:00+00:00",
      "in_shade": false,
      "sun_azimuth": 62.3,
      "sun_elevation": 2.1
    },
    ...
  ]
}
```

## Shadow Algorithm

1. Load building footprints within 200 m of the pub
2. For each 5-minute daylight step, compute sun position (azimuth + elevation) via `pysolar`
3. Per building, compute shadow polygon:
   - `shadow_length = building_height / tan(radians(sun_elevation))`
   - Shadow direction = `sun_azimuth + 180°` (opposite the sun)
   - Offset each footprint vertex by the shadow vector (converted from metres to degrees)
   - Shadow polygon = `convex_hull(footprint_vertices ∪ offset_vertices)`
4. Pub is in shade if its point falls within any shadow polygon

## Building Data Note

OSM building heights (`height` tag or `building:levels * 3 m`) are used by default.
For higher accuracy the City of Zagreb provides LoD 2.2 3D meshes via 15 ArcGIS
Scene Services (2022–2023 survey). Each service exposes `Z_Min`/`Z_Max` per mesh node.

Base URL pattern:
```
https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services/ZG3D_GC_{district}_2022/SceneServer
```

Districts: `Pescenica_Zitnjak`, `Gornji_Grad`, `Novi_Zagreb_zapad`, `Trnje`, `Brezovica`,
`Novi_Zagreb_istok`, `Donja_Dubrava`, `Tresnjevka_jug`, `Crnomerec`, `Sesvete`,
`Podsused_Vrapce`, `Gornja_Dubrava`, `Donji_Grad`, `Maksimir`, `Podsljeme`

See `data_pipeline/fetch_buildings.py` for integration guidance.
