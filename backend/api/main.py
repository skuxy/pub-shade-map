"""
FastAPI application — serves pub shade data, weather proxying, and the
frontend static files.

Startup loads pubs and buildings into memory from their GeoJSON caches
so that shade requests are fast without repeated disk I/O.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from data_pipeline.cache import DATA_DIR, load_geojson
from data_pipeline.fetch_pubs import fetch_pubs
from data_pipeline.fetch_buildings import fetch_buildings
from shadow.shade_timeline import compute_shade_timeline

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"
PUBS_CACHE    = DATA_DIR / "pubs.geojson"
BUILDINGS_CACHE = DATA_DIR / "buildings.geojson"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Zagreb Pub Shade Map API",
    description="Predicts sun/shade periods for Zagreb pubs using 2.5D shadow casting.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory state (populated on startup)
# ---------------------------------------------------------------------------
_pubs: list[dict] = []       # GeoJSON features
_buildings: list[dict] = []  # dicts with footprint + height

# Zagreb time offset (approximate — does not handle DST boundary automatically)
ZAGREB_UTC_OFFSET_H = 1  # CET; use 2 for CEST (summer)


def _zagreb_today() -> date:
    """Return today's date in Zagreb local time (approximate, ignores DST)."""
    now_utc = datetime.now(timezone.utc)
    return (now_utc.replace(hour=(now_utc.hour + ZAGREB_UTC_OFFSET_H) % 24)).date()


def _feature_to_building(feature: dict) -> dict | None:
    """Convert a GeoJSON building feature to the internal building dict."""
    geom = feature.get("geometry", {})
    props = feature.get("properties", {})

    if geom.get("type") != "Polygon":
        return None

    rings = geom.get("coordinates", [])
    if not rings:
        return None

    footprint = [(c[0], c[1]) for c in rings[0]]
    height = float(props.get("height", 8.0))

    return {
        "id": props.get("id", ""),
        "footprint": footprint,
        "height": height,
    }


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    global _pubs, _buildings

    async with httpx.AsyncClient() as session:
        # Auto-fetch pubs if cache is missing (e.g. fresh Render deployment)
        pubs_geojson = await load_geojson(PUBS_CACHE)
        if not pubs_geojson:
            print("[startup] data/pubs.geojson not found — fetching from OSM …")
            await fetch_pubs(session)
            pubs_geojson = await load_geojson(PUBS_CACHE)

        if pubs_geojson:
            _pubs = pubs_geojson.get("features", [])
            print(f"[startup] Loaded {len(_pubs)} pubs.")
        else:
            print("[startup] ERROR: could not load or fetch pubs.")

        # Auto-fetch buildings if cache is missing
        buildings_geojson = await load_geojson(BUILDINGS_CACHE)
        if not buildings_geojson:
            print("[startup] data/buildings.geojson not found — fetching from OSM …")
            await fetch_buildings(session)
            buildings_geojson = await load_geojson(BUILDINGS_CACHE)

        if buildings_geojson:
            raw = buildings_geojson.get("features", [])
            _buildings = [b for f in raw if (b := _feature_to_building(f)) is not None]
            print(f"[startup] Loaded {len(_buildings)} buildings.")
        else:
            print("[startup] ERROR: could not load or fetch buildings.")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "pubs_loaded": len(_pubs),
        "buildings_loaded": len(_buildings),
    }


@app.get("/api/pubs")
async def get_pubs():
    """Return all pubs as a GeoJSON FeatureCollection."""
    return JSONResponse({
        "type": "FeatureCollection",
        "features": _pubs,
    })


@app.get("/api/shade/{pub_id:path}")
async def get_shade(
    pub_id: str,
    date_str: str = Query(default=None, alias="date"),
):
    """
    Return the sun/shade timeline for *pub_id* on *date_str* (YYYY-MM-DD).

    If no date is supplied the timeline is computed for today in Zagreb time.
    """
    # Resolve date
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        target_date = _zagreb_today()

    # Find the pub
    pub = next(
        (f for f in _pubs if f["properties"]["id"] == pub_id),
        None,
    )
    if pub is None:
        raise HTTPException(status_code=404, detail=f"Pub '{pub_id}' not found")

    if not _buildings:
        raise HTTPException(
            status_code=503,
            detail="Building data not loaded — run fetch_data.py first",
        )

    # Compute timeline
    timeline = compute_shade_timeline(pub, _buildings, target_date, step_minutes=5)

    return {
        "pub_id": pub_id,
        "pub_name": pub["properties"].get("name", ""),
        "date": target_date.isoformat(),
        "step_minutes": 5,
        "timeline": timeline,
    }


@app.get("/api/weather")
async def get_weather(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """
    Proxy a 3-day hourly weather forecast from Open-Meteo for (lat, lon).

    Variables returned: temperature_2m, precipitation_probability,
    weathercode, cloudcover, windspeed_10m.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,precipitation_probability,weathercode,"
        "cloudcover,windspeed_10m"
        "&forecast_days=3"
        "&timezone=Europe%2FZagreb"
    )
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            return JSONResponse(resp.json())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Weather API error: {exc}")


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        candidate = FRONTEND_DIR / full_path
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
