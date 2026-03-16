"""
FastAPI application — serves pub shade data, weather proxying, and the
frontend static files.

Startup:
  1. Loads (or auto-fetches) pubs + buildings into memory.
  2. Builds a Shapely STRtree spatial index over building footprints so that
     nearby-building lookups are O(log n) instead of O(n).
  3. Kicks off a background task that pre-computes shade timelines for all
     pubs for today and the next 3 days, storing results in _shade_cache.

Caching:
  - Shade timelines: in-memory dict keyed by (pub_id, date). Shade for a
    given date is deterministic and never changes, so no TTL is needed.
  - Weather: in-memory dict keyed by a ~1 km grid cell (2 d.p. lat/lon),
    cached for WEATHER_CACHE_TTL_S seconds (1 hour by default).
"""

import asyncio
import math
import json
import psutil
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiofiles
import httpx
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from shapely.geometry import box as shapely_box, Polygon
from shapely import STRtree

from data_pipeline.cache import DATA_DIR, load_geojson
from data_pipeline.fetch_pubs import fetch_pubs
from data_pipeline.fetch_buildings import fetch_buildings
from shadow.shade_timeline import compute_shade_timeline, find_nearby_buildings, BUILDING_SEARCH_RADIUS_M

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FRONTEND_DIR    = Path(__file__).parent.parent.parent / "frontend"
PUBS_CACHE      = DATA_DIR / "pubs.geojson"
BUILDINGS_CACHE = DATA_DIR / "buildings.geojson"

WEATHER_CACHE_TTL_S  = 3600   # 1 hour
PRECOMPUTE_DAYS      = 3      # pre-compute today + next N days
PRECOMPUTE_WORKERS   = 4      # concurrent shade timelines (shapely releases GIL)
SHADE_CACHE_DIR      = DATA_DIR / "shade_cache"

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
# In-memory state
# ---------------------------------------------------------------------------
_pubs: list[dict] = []
_buildings: list[dict] = []
_building_polys: list[Polygon] = []   # parallel to _buildings, for STRtree
_strtree: STRtree | None = None        # spatial index

# (pub_id, date_iso) -> timeline list
_shade_cache: dict[tuple[str, str], list[dict]] = {}

# grid_key -> (fetched_at: datetime, data: dict)
_weather_cache: dict[str, tuple[datetime, dict]] = {}

# Handle for the background precompute task (cancelled on shutdown)
_precompute_task: asyncio.Task | None = None

# Startup timestamp for uptime tracking
_started_at: datetime | None = None

# Zagreb time offset (CET = UTC+1; flip to 2 in summer if needed)
ZAGREB_UTC_OFFSET_H = 1


def _zagreb_today() -> date:
    now_utc = datetime.now(timezone.utc)
    return (now_utc + timedelta(hours=ZAGREB_UTC_OFFSET_H)).date()


def _feature_to_building(feature: dict) -> dict | None:
    geom  = feature.get("geometry", {})
    props = feature.get("properties", {})
    if geom.get("type") != "Polygon":
        return None
    rings = geom.get("coordinates", [])
    if not rings:
        return None
    footprint = [(c[0], c[1]) for c in rings[0]]
    height = float(props.get("height", 8.0))
    return {"id": props.get("id", ""), "footprint": footprint, "height": height}


def _get_nearby_buildings(pub_lon: float, pub_lat: float) -> list[dict]:
    """
    Return buildings within BUILDING_SEARCH_RADIUS_M of (pub_lon, pub_lat).

    Uses the STRtree spatial index when available (fast path), falling back
    to the brute-force haversine scan otherwise.
    """
    if _strtree is None or not _building_polys:
        return find_nearby_buildings(pub_lon, pub_lat, _buildings)

    # Convert radius to approximate degree offsets for a bounding-box query
    lat_deg = BUILDING_SEARCH_RADIUS_M / 111_320.0
    lon_deg = BUILDING_SEARCH_RADIUS_M / (111_320.0 * math.cos(math.radians(pub_lat)))
    search_box = shapely_box(
        pub_lon - lon_deg, pub_lat - lat_deg,
        pub_lon + lon_deg, pub_lat + lat_deg,
    )
    indices = _strtree.query(search_box)
    return [_buildings[i] for i in indices]


# ---------------------------------------------------------------------------
# Disk cache helpers
# ---------------------------------------------------------------------------
async def _save_shade_cache(target_date: date) -> None:
    """Persist all cached timelines for *target_date* to disk."""
    SHADE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = target_date.isoformat()
    data = {
        pub_id: timeline
        for (pub_id, d), timeline in _shade_cache.items()
        if d == date_str
    }
    cache_file = SHADE_CACHE_DIR / f"{date_str}.json"
    async with aiofiles.open(str(cache_file), "w") as f:
        await f.write(json.dumps(data))
    print(f"[cache] Saved {len(data)} timelines → {cache_file.name}")


async def _load_shade_cache(target_date: date) -> int:
    """Load cached timelines for *target_date* from disk into _shade_cache."""
    cache_file = SHADE_CACHE_DIR / f"{target_date.isoformat()}.json"
    if not cache_file.exists():
        return 0
    try:
        async with aiofiles.open(str(cache_file)) as f:
            data = json.loads(await f.read())
        date_str = target_date.isoformat()
        for pub_id, timeline in data.items():
            _shade_cache[(pub_id, date_str)] = timeline
        print(f"[cache] Loaded {len(data)} timelines from disk for {target_date}")
        return len(data)
    except Exception as exc:
        print(f"[cache] Warning: could not load disk cache for {target_date}: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Background pre-computation
# ---------------------------------------------------------------------------
def _compute_one(pub: dict, target_date: date, nearby: list[dict]) -> list[dict]:
    """Compute a single shade timeline — runs in a thread pool worker."""
    return compute_shade_timeline(
        pub, _buildings, target_date,
        step_minutes=5,
        nearby_buildings=nearby,
    )


async def _precompute_shade() -> None:
    """
    Pre-compute shade timelines for all pubs for today + PRECOMPUTE_DAYS.

    Steps:
      1. Load any existing disk cache for each date (survives restarts).
      2. Compute remaining timelines in parallel (PRECOMPUTE_WORKERS concurrent
         threads — shapely releases the GIL, so true parallelism on multi-core).
      3. Save completed timelines back to disk.
    """
    if not _pubs or not _buildings:
        return

    today = _zagreb_today()
    dates = [today + timedelta(days=i) for i in range(PRECOMPUTE_DAYS)]

    # 1. Load from disk (skip recompute for anything already cached)
    for d in dates:
        await _load_shade_cache(d)

    sem      = asyncio.Semaphore(PRECOMPUTE_WORKERS)
    progress = [0]
    total    = len(_pubs) * len(dates)
    needed   = total - len(_shade_cache)
    print(f"[precompute] {len(_shade_cache)} loaded from disk; {needed} to compute …")

    async def _one(pub: dict, target_date: date, nearby: list[dict]) -> None:
        cache_key = (pub["properties"]["id"], target_date.isoformat())
        if cache_key in _shade_cache:
            progress[0] += 1
            return
        async with sem:
            if cache_key in _shade_cache:   # re-check after acquiring
                progress[0] += 1
                return
            timeline = await asyncio.to_thread(_compute_one, pub, target_date, nearby)
            _shade_cache[cache_key] = timeline
            progress[0] += 1
            if progress[0] % 100 == 0:
                print(f"[precompute] {progress[0]}/{total} …")

    tasks = [
        _one(pub, d, _get_nearby_buildings(
            float(pub["geometry"]["coordinates"][0]),
            float(pub["geometry"]["coordinates"][1]),
        ))
        for pub in _pubs
        for d in dates
    ]
    await asyncio.gather(*tasks)

    # 2. Persist to disk so the next restart is instant
    for d in dates:
        await _save_shade_cache(d)

    print(f"[precompute] Done. {len(_shade_cache)} timelines cached.")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup() -> None:
    global _pubs, _buildings, _building_polys, _strtree, _started_at, _precompute_task
    _started_at = datetime.now(timezone.utc)

    async with httpx.AsyncClient() as session:
        pubs_geojson = await load_geojson(PUBS_CACHE)
        if not pubs_geojson:
            print("[startup] Fetching pubs from OSM …")
            await fetch_pubs(session)
            pubs_geojson = await load_geojson(PUBS_CACHE)
        if pubs_geojson:
            _pubs = pubs_geojson.get("features", [])
            print(f"[startup] Loaded {len(_pubs)} pubs.")
        else:
            print("[startup] ERROR: could not load pubs.")

        buildings_geojson = await load_geojson(BUILDINGS_CACHE)
        if not buildings_geojson:
            print("[startup] Fetching buildings from OSM …")
            await fetch_buildings(session)
            buildings_geojson = await load_geojson(BUILDINGS_CACHE)
        if buildings_geojson:
            raw = buildings_geojson.get("features", [])
            _buildings = [b for f in raw if (b := _feature_to_building(f)) is not None]
            print(f"[startup] Loaded {len(_buildings)} buildings.")

            # Build STRtree spatial index
            _building_polys = []
            valid_buildings  = []
            for b in _buildings:
                try:
                    poly = Polygon(b["footprint"])
                    if poly.is_valid and not poly.is_empty:
                        _building_polys.append(poly)
                        valid_buildings.append(b)
                except Exception:
                    pass
            _buildings = valid_buildings
            _strtree = STRtree(_building_polys)
            print(f"[startup] STRtree built over {len(_building_polys)} building polygons.")

            # Pre-compute centroids so shadow_cast.py doesn't recompute them
            # on every point_in_shadow call (170 time steps × N buildings).
            for b in _buildings:
                fp = b["footprint"]
                lons = [p[0] for p in fp]
                lats = [p[1] for p in fp]
                b["centroid"] = (sum(lons) / len(lons), sum(lats) / len(lats))
        else:
            print("[startup] ERROR: could not load buildings.")

    # Kick off pre-computation in the background (non-blocking)
    _precompute_task = asyncio.create_task(_precompute_shade())


@app.on_event("shutdown")
async def shutdown() -> None:
    global _precompute_task
    if _precompute_task and not _precompute_task.done():
        _precompute_task.cancel()
        try:
            await _precompute_task
        except asyncio.CancelledError:
            pass
    print("[shutdown] Clean shutdown complete.")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    now = datetime.now(timezone.utc)
    uptime_s = (now - _started_at).total_seconds() if _started_at else None

    mem = psutil.virtual_memory()
    proc = psutil.Process(os.getpid())
    proc_mem_mb = proc.memory_info().rss / 1024 / 1024

    precompute_running = (
        _precompute_task is not None
        and not _precompute_task.done()
    )

    today = _zagreb_today()
    dates_cached = {d for (_, d) in _shade_cache}
    pubs_cached_today = sum(
        1 for (_, d) in _shade_cache if d == today.isoformat()
    )

    return {
        "status": "ok",
        "uptime_seconds": round(uptime_s) if uptime_s is not None else None,
        "pubs_loaded": len(_pubs),
        "buildings_loaded": len(_buildings),
        "strtree_ready": _strtree is not None,
        "shade_cache": {
            "total_entries": len(_shade_cache),
            "dates_covered": sorted(dates_cached),
            "pubs_cached_today": pubs_cached_today,
            "precompute_running": precompute_running,
        },
        "weather_cache_entries": len(_weather_cache),
        "memory": {
            "process_rss_mb": round(proc_mem_mb, 1),
            "system_used_pct": mem.percent,
            "system_available_mb": round(mem.available / 1024 / 1024, 1),
        },
    }


@app.get("/api/pubs")
async def get_pubs():
    """Return all pubs as a GeoJSON FeatureCollection."""
    return JSONResponse({"type": "FeatureCollection", "features": _pubs})


@app.get("/api/shade/{pub_id:path}")
async def get_shade(
    pub_id: str,
    date_str: str = Query(default=None, alias="date"),
):
    """
    Return the sun/shade timeline for *pub_id* on *date_str* (YYYY-MM-DD).
    Served from cache when available; computed on demand otherwise.
    """
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        target_date = _zagreb_today()

    pub = next((f for f in _pubs if f["properties"]["id"] == pub_id), None)
    if pub is None:
        raise HTTPException(status_code=404, detail=f"Pub '{pub_id}' not found")

    if not _buildings:
        raise HTTPException(status_code=503, detail="Building data not loaded")

    cache_key = (pub_id, target_date.isoformat())

    if cache_key not in _shade_cache:
        coords  = pub["geometry"]["coordinates"]
        pub_lon, pub_lat = float(coords[0]), float(coords[1])
        nearby  = _get_nearby_buildings(pub_lon, pub_lat)
        timeline = await asyncio.to_thread(_compute_one, pub, target_date, nearby)
        _shade_cache[cache_key] = timeline

    return {
        "pub_id":      pub_id,
        "pub_name":    pub["properties"].get("name", ""),
        "date":        target_date.isoformat(),
        "step_minutes": 5,
        "timeline":    _shade_cache[cache_key],
    }


@app.get("/api/weather")
async def get_weather(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """
    Proxy a 3-day hourly weather forecast from Open-Meteo.
    Results are cached per ~1 km grid cell for WEATHER_CACHE_TTL_S seconds.
    """
    # Round to 2 d.p. ≈ ~1 km grid cell
    grid_key = f"{round(lat, 2)}_{round(lon, 2)}"
    now = datetime.now(timezone.utc)

    cached = _weather_cache.get(grid_key)
    if cached:
        fetched_at, data = cached
        if (now - fetched_at).total_seconds() < WEATHER_CACHE_TTL_S:
            return JSONResponse(data)

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
            data = resp.json()
            _weather_cache[grid_key] = (now, data)
            return JSONResponse(data)
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
