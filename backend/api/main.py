"""
FastAPI application — serves pub shade data, weather proxying, and the
frontend static files.

Startup sequence
────────────────
1. Load (or auto-fetch) pubs + buildings into memory.
   OSM cache files are considered stale after CACHE_MAX_AGE_DAYS days and
   are re-fetched automatically.
2. Build a Shapely STRtree spatial index over building footprints so that
   nearby-building lookups are O(log n) instead of O(n).
3. Pre-compute building centroids once so the directional shadow filter in
   shadow_cast.py does not recompute them on every call.
4. Kick off a background task that pre-computes shade timelines for all pubs
   for today and the next PRECOMPUTE_DAYS days, storing results in
   _shade_cache.  Up to PRECOMPUTE_WORKERS timelines are computed in
   parallel (Shapely releases the GIL so true parallelism is possible).

Caching
───────
- Shade timelines: in-memory dict keyed by (pub_id, date_iso).  Shade for
  a given date is deterministic and never changes, so no TTL is needed.
  Timelines are also persisted to SHADE_CACHE_DIR/{date}.json so that
  restarts do not require recomputation.
- Sunny scores: derived from shade timelines (% of daylight steps in sun),
  stored in _sunny_scores keyed by (pub_id, date_iso).
- Weather: in-memory dict keyed by a ~1 km grid cell (2 d.p. lat/lon),
  cached for WEATHER_CACHE_TTL_S seconds (1 hour by default).

Timezone
────────
Croatia observes CET (UTC+1) in winter and CEST (UTC+2) in summer.  The
``_zagreb_today()`` helper uses ``zoneinfo.ZoneInfo('Europe/Zagreb')`` so
it returns the correct local date regardless of DST.  A hardcoded
UTC+1 offset would return the wrong date between midnight and 1 AM during
CEST (late March – late October).
"""

import asyncio
import math
import json
import psutil
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiofiles
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from shapely.geometry import box as shapely_box, Polygon
from shapely import STRtree

from data_pipeline.cache import DATA_DIR, load_geojson
from data_pipeline.fetch_pubs import fetch_pubs
from data_pipeline.fetch_buildings import fetch_buildings
from shadow.shade_timeline import compute_shade_timeline, find_nearby_buildings, BUILDING_SEARCH_RADIUS_M
from shadow.solar import get_sun_timeline

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FRONTEND_DIR    = Path(__file__).parent.parent.parent / "frontend"
PUBS_CACHE      = DATA_DIR / "pubs.geojson"
BUILDINGS_CACHE = DATA_DIR / "buildings.geojson"

WEATHER_CACHE_TTL_S  = 3600   # seconds — weather forecast TTL
PRECOMPUTE_DAYS      = 3      # pre-compute shade for today + this many days
PRECOMPUTE_WORKERS   = 4      # concurrent shade computations (Shapely releases GIL)
SHADE_CACHE_DIR      = DATA_DIR / "shade_cache"
CACHE_MAX_AGE_DAYS   = 7      # re-fetch OSM data after this many days

ZAGREB_TZ = ZoneInfo("Europe/Zagreb")

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

# (pub_id, date_iso) -> shade timeline list
_shade_cache: dict[tuple[str, str], list[dict]] = {}

# (pub_id, date_iso) -> % of daylight steps where pub is in sun (0–100)
_sunny_scores: dict[tuple[str, str], int] = {}

# grid_key -> (fetched_at: datetime, data: dict)
_weather_cache: dict[str, tuple[datetime, dict]] = {}

# Handle for the background precompute task (cancelled on shutdown)
_precompute_task: asyncio.Task | None = None

# Startup timestamp for uptime tracking
_started_at: datetime | None = None


def _zagreb_today() -> date:
    """Return today's date in the Europe/Zagreb timezone (DST-aware)."""
    return datetime.now(ZAGREB_TZ).date()


def _osm_cache_is_stale(path: Path) -> bool:
    """Return True if *path* does not exist or is older than CACHE_MAX_AGE_DAYS."""
    if not path.exists():
        return True
    age_s = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    return age_s > CACHE_MAX_AGE_DAYS * 86_400


def _compute_sunny_score(timeline: list[dict]) -> int:
    """Return the percentage of daylight steps where the pub is in sun (0–100)."""
    if not timeline:
        return 0
    sun_steps = sum(1 for t in timeline if not t["in_shade"])
    return round(sun_steps / len(timeline) * 100)


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
    """
    Load cached timelines for *target_date* from disk into _shade_cache and
    rebuild _sunny_scores for each loaded entry.
    """
    cache_file = SHADE_CACHE_DIR / f"{target_date.isoformat()}.json"
    if not cache_file.exists():
        return 0
    try:
        async with aiofiles.open(str(cache_file)) as f:
            data = json.loads(await f.read())
        date_str = target_date.isoformat()
        for pub_id, timeline in data.items():
            _shade_cache[(pub_id, date_str)] = timeline
            _sunny_scores[(pub_id, date_str)] = _compute_sunny_score(timeline)
        print(f"[cache] Loaded {len(data)} timelines from disk for {target_date}")
        return len(data)
    except Exception as exc:
        print(f"[cache] Warning: could not load disk cache for {target_date}: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Background pre-computation
# ---------------------------------------------------------------------------
def _compute_one(
    pub: dict,
    target_date: date,
    nearby: list[dict],
    sun_timeline: list[dict],
) -> list[dict]:
    """Compute a single shade timeline — runs in a thread pool worker."""
    return compute_shade_timeline(
        pub, _buildings, target_date,
        step_minutes=5,
        nearby_buildings=nearby,
        sun_timeline=sun_timeline,
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

    # Pre-compute sun positions once per date (shared across all pubs).
    # Each get_sun_timeline call costs ~2–3 s; without this, it would be
    # called once per pub × date = 1332 times instead of 3 times.
    print(f"[precompute] Pre-computing sun positions for {len(dates)} dates …")
    sun_timelines: dict[str, list[dict]] = {}
    for d in dates:
        sun_timelines[d.isoformat()] = await asyncio.to_thread(get_sun_timeline, d)
    print(f"[precompute] Sun positions ready.")

    async def _one(
        pub: dict,
        target_date: date,
        nearby: list[dict],
        sun_tl: list[dict],
    ) -> None:
        cache_key = (pub["properties"]["id"], target_date.isoformat())
        if cache_key in _shade_cache:
            progress[0] += 1
            return
        async with sem:
            if cache_key in _shade_cache:   # re-check after acquiring
                progress[0] += 1
                return
            timeline = await asyncio.to_thread(_compute_one, pub, target_date, nearby, sun_tl)
            _shade_cache[cache_key] = timeline
            _sunny_scores[cache_key] = _compute_sunny_score(timeline)
            progress[0] += 1
            if progress[0] % 50 == 0:
                print(f"[precompute] {progress[0]}/{total} …")

    # Build tasks: compute nearby buildings ONCE per pub (reused across dates).
    tasks = []
    for pub in _pubs:
        coords  = pub["geometry"]["coordinates"]
        nearby  = _get_nearby_buildings(float(coords[0]), float(coords[1]))
        for d in dates:
            tasks.append(_one(pub, d, nearby, sun_timelines[d.isoformat()]))

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
        if _osm_cache_is_stale(PUBS_CACHE):
            print(f"[startup] Pubs cache missing or >{CACHE_MAX_AGE_DAYS}d old — fetching from OSM …")
            await fetch_pubs(session)
        pubs_geojson = await load_geojson(PUBS_CACHE)
        if pubs_geojson:
            _pubs = pubs_geojson.get("features", [])
            print(f"[startup] Loaded {len(_pubs)} pubs.")
        else:
            print("[startup] ERROR: could not load pubs.")

        if _osm_cache_is_stale(BUILDINGS_CACHE):
            print(f"[startup] Buildings cache missing or >{CACHE_MAX_AGE_DAYS}d old — fetching from OSM …")
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

            # Pre-attach centroids and pre-built Shapely polygons to each
            # building dict so shadow_cast.py never reconstructs them in the
            # hot path (point_in_shadow is called 170 × N_buildings per pub).
            for b, poly in zip(_buildings, _building_polys):
                fp = b["footprint"]
                lons = [p[0] for p in fp]
                lats = [p[1] for p in fp]
                b["centroid"] = (sum(lons) / len(lons), sum(lats) / len(lats))
                b["poly"]     = poly   # pre-built Polygon for own-building check
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
    """
    Return all pubs as a GeoJSON FeatureCollection.

    Each feature's ``properties`` includes ``sunny_score_today`` (0–100) when
    the shade timeline for today has already been pre-computed.  The frontend
    uses this to colour markers and populate the filter/sort controls without
    needing per-pub ``/api/shade`` calls.
    """
    today_iso = _zagreb_today().isoformat()
    features = []
    for pub in _pubs:
        pub_id = pub["properties"]["id"]
        score  = _sunny_scores.get((pub_id, today_iso))
        if score is not None:
            # Shallow copy — do not mutate the in-memory pub dict.
            f = {**pub, "properties": {**pub["properties"], "sunny_score_today": score}}
        else:
            f = pub
        features.append(f)
    return JSONResponse({"type": "FeatureCollection", "features": features})


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
        nearby    = _get_nearby_buildings(pub_lon, pub_lat)
        sun_tl    = await asyncio.to_thread(get_sun_timeline, target_date)
        timeline  = await asyncio.to_thread(_compute_one, pub, target_date, nearby, sun_tl)
        _shade_cache[cache_key] = timeline
        _sunny_scores[cache_key] = _compute_sunny_score(timeline)

    return {
        "pub_id":       pub_id,
        "pub_name":     pub["properties"].get("name", ""),
        "date":         target_date.isoformat(),
        "step_minutes": 5,
        "sunny_pct":    _sunny_scores.get(cache_key, 0),
        "timeline":     _shade_cache[cache_key],
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


@app.get("/api/current-status")
async def get_current_status():
    """
    Return the current sun/shade status and today's sunny score for every pub
    in a single response.

    The frontend calls this endpoint once on load (and every 5 minutes) instead
    of issuing one ``/api/shade`` request per pub — replacing ~440 individual
    calls with a single round-trip.

    Response shape::

        {
          "as_of": "2026-03-16T10:30:00+00:00",
          "pubs": {
            "<pub_id>": {
              "status":           "sun" | "shade" | "night" | "unknown",
              "sunny_score_today": 0–100   (omitted if not yet computed)
            },
            ...
          }
        }
    """
    now_utc  = datetime.now(timezone.utc)
    today    = _zagreb_today()
    date_iso = today.isoformat()
    # Pre-compute the ISO string of "now" once for comparisons below.
    now_iso  = now_utc.isoformat()

    pubs_status: dict[str, dict] = {}
    for pub in _pubs:
        pub_id    = pub["properties"]["id"]
        cache_key = (pub_id, date_iso)
        timeline  = _shade_cache.get(cache_key)

        if not timeline:
            entry = {"status": "unknown"}
        else:
            # Find first timeline entry at or after the current UTC time.
            current = next((t for t in timeline if t["time"] >= now_iso), None)
            if current is None:
                entry = {"status": "night"}
            else:
                entry = {"status": "shade" if current["in_shade"] else "sun"}

        score = _sunny_scores.get(cache_key)
        if score is not None:
            entry["sunny_score_today"] = score

        pubs_status[pub_id] = entry

    return JSONResponse({"as_of": now_iso, "pubs": pubs_status})


@app.post("/api/admin/refresh")
async def admin_refresh(key: str = Query(..., description="REFRESH_KEY env var value")):
    """
    Force a full OSM data re-fetch and rebuild the in-memory state.

    Requires the ``key`` query parameter to match the ``REFRESH_KEY``
    environment variable.  Set it on the server to prevent unauthorised
    re-fetches (which are slow and hit the Overpass API).

    Use this after OSM data has been updated and you want the map to reflect
    the changes without waiting for the 7-day cache TTL.

    Example::

        POST /api/admin/refresh?key=<your-secret>
    """
    global _pubs, _buildings, _building_polys, _strtree, _precompute_task

    expected_key = os.environ.get("REFRESH_KEY", "")
    if not expected_key or key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid or missing REFRESH_KEY")

    # Cancel any running precompute before wiping state.
    if _precompute_task and not _precompute_task.done():
        _precompute_task.cancel()
        try:
            await _precompute_task
        except asyncio.CancelledError:
            pass

    # Delete OSM caches so startup() re-fetches them.
    for f in [PUBS_CACHE, BUILDINGS_CACHE]:
        if f.exists():
            f.unlink()

    # Re-run the full startup sequence.
    await startup()
    return {"status": "refreshed", "pubs": len(_pubs), "buildings": len(_buildings)}


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
