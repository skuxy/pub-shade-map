"""
Fetch building footprints with heights and cache them as a GeoJSON
FeatureCollection.

Data source selection
─────────────────────
Two sources are supported, selected by the ``USE_DKP_FOOTPRINTS`` environment
variable:

  USE_DKP_FOOTPRINTS=0 (default)
    OpenStreetMap via Overpass API.  Height resolution priority:
      1. ``height`` tag (metres, float)
      2. ``building:levels`` × 3.0 m  (standard floor-height estimate)
      3. Fallback: 8.0 m (~2 storeys)

  USE_DKP_FOOTPRINTS=1
    City of Zagreb DKP (Digital Cadastral Plan) FeatureServer.
    Footprint polygons come from official land-survey records and are
    generally more accurate than OSM.  Heights are estimated from the
    building-type (VRSTA) field and enriched with OSM height tags via a
    Shapely spatial join (see ``fetch_buildings_dkp.py`` for details).

ZG3D upgrade path
─────────────────
The City of Zagreb also publishes LoD 2.2 3D building meshes covering all 15
city districts from a 2022–2023 multisensor survey (Z_Min / Z_Max per mesh
node).  These would give precise surveyed heights.

  Hub:      https://zg3d-zagreb.hub.arcgis.com/
  Base URL: https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services/
            ZG3D_GC_{district}_2022/SceneServer
  Districts: Pescenica_Zitnjak, Gornji_Grad, Novi_Zagreb_zapad, Trnje,
             Brezovica, Novi_Zagreb_istok, Donja_Dubrava, Tresnjevka_jug,
             Crnomerec, Sesvete, Podsused_Vrapce, Gornja_Dubrava, Donji_Grad,
             Maksimir, Podsljeme

  Status: SceneServer endpoints currently return HTTP 400 "Invalid URL".
  Once access is restored, replace the height values with (Z_Max − Z_Min)
  from those services, joined to the DKP footprints by spatial intersection.
"""

import asyncio
import os
import httpx
from pathlib import Path
from .cache import DATA_DIR, save_geojson

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_URL = OVERPASS_MIRRORS[0]

# Zagreb bounding box (south, west, north, east)
BBOX = (45.72, 15.87, 45.87, 16.12)

BUILDINGS_CACHE = DATA_DIR / "buildings.geojson"

DEFAULT_HEIGHT_M = 8.0        # fallback for buildings with no height data
METRES_PER_LEVEL = 3.0        # standard floor-to-floor height estimate

OVERPASS_QUERY = """
[out:json][timeout:120];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]({s},{w},{n},{e});
);
out geom tags;
""".strip()


def _build_query() -> str:
    s, w, n, e = BBOX
    return OVERPASS_QUERY.format(s=s, w=w, n=n, e=e)


def _parse_height(tags: dict) -> float:
    """Return building height in metres from OSM tags."""
    if "height" in tags:
        try:
            # Tags like "12", "12 m", "12.5"
            return float(tags["height"].split()[0])
        except (ValueError, IndexError):
            pass

    if "building:levels" in tags:
        try:
            levels = float(tags["building:levels"])
            min_level = float(tags.get("building:min_level", 0))
            return max(1.0, levels - min_level) * METRES_PER_LEVEL
        except ValueError:
            pass

    return DEFAULT_HEIGHT_M


def _way_to_feature(el: dict) -> dict | None:
    """Convert an Overpass way/relation element to a GeoJSON Feature."""
    tags = el.get("tags", {})
    height = _parse_height(tags)

    if el["type"] == "way":
        geometry = el.get("geometry", [])
        if len(geometry) < 3:
            return None
        coords = [[pt["lon"], pt["lat"]] for pt in geometry]
        # Close ring if not already closed
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        geojson_geom = {"type": "Polygon", "coordinates": [coords]}

    elif el["type"] == "relation":
        # Use the outer member ways as the footprint ring
        outer_coords = []
        for member in el.get("members", []):
            if member.get("role") == "outer" and member.get("type") == "way":
                pts = member.get("geometry", [])
                outer_coords.extend([[pt["lon"], pt["lat"]] for pt in pts])
        if len(outer_coords) < 3:
            return None
        if outer_coords[0] != outer_coords[-1]:
            outer_coords.append(outer_coords[0])
        geojson_geom = {"type": "Polygon", "coordinates": [outer_coords]}

    else:
        return None

    return {
        "type": "Feature",
        "geometry": geojson_geom,
        "properties": {
            "id": f"{el['type']}/{el['id']}",
            "height": height,
            "levels": tags.get("building:levels", ""),
            "building": tags.get("building", "yes"),
        },
    }


async def fetch_buildings(session: httpx.AsyncClient) -> list[dict]:
    """
    Fetch building footprints for Zagreb and cache to ``data/buildings.geojson``.

    If the environment variable ``USE_DKP_FOOTPRINTS=1`` is set, the City of
    Zagreb DKP cadastral layer is used instead of Overpass.  OSM is always
    fetched first when using DKP so that OSM heights can enrich the DKP data.

    Falls back to Overpass if the DKP fetch fails.
    """
    if os.environ.get("USE_DKP_FOOTPRINTS", "0") == "1":
        try:
            from .fetch_buildings_dkp import fetch_buildings_dkp, BUILDINGS_DKP_CACHE
            print("[buildings] USE_DKP_FOOTPRINTS=1 — fetching from DKP …")
            # Ensure OSM cache exists so DKP enrichment can use OSM heights.
            if not (DATA_DIR / "buildings.geojson").exists():
                print("[buildings] Fetching OSM buildings first (for height enrichment) …")
                await _fetch_osm_buildings(session)
            features = await fetch_buildings_dkp(session, enrich_from_osm=True)
            # Point the main cache file at the DKP result.
            import shutil
            shutil.copy2(str(BUILDINGS_DKP_CACHE), str(BUILDINGS_CACHE))
            print(f"[buildings] DKP buildings written to {BUILDINGS_CACHE}")
            return features
        except Exception as exc:
            print(f"[buildings] DKP fetch failed ({exc}), falling back to OSM.")

    return await _fetch_osm_buildings(session)


async def _fetch_osm_buildings(session: httpx.AsyncClient) -> list[dict]:
    """Query Overpass for building footprints in Zagreb. Cached to data/buildings.geojson."""
    print("Querying Overpass API for buildings …")
    query = _build_query()
    last_err = None

    for mirror in OVERPASS_MIRRORS:
        for attempt in range(3):
            try:
                response = await session.post(
                    mirror, data={"data": query}, timeout=240.0,
                )
                response.raise_for_status()
                elements = response.json().get("elements", [])
                break
            except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
                last_err = e
                wait = 10 * (attempt + 1)
                print(f"  [{mirror}] attempt {attempt+1} failed: {e}. Retrying in {wait}s…")
                await asyncio.sleep(wait)
        else:
            continue
        break
    else:
        raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_err}")

    features = [f for el in elements if (f := _way_to_feature(el)) is not None]

    with_explicit_height = sum(
        1 for f in features
        if f["properties"]["levels"] or "height" in str(f["properties"])
    )

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "source": "OpenStreetMap via Overpass API",
            "bbox": {"south": BBOX[0], "west": BBOX[1], "north": BBOX[2], "east": BBOX[3]},
            "count": len(features),
            "with_explicit_height": with_explicit_height,
            "default_height_m": DEFAULT_HEIGHT_M,
            "note": (
                "For higher accuracy replace with ZG3D ArcGIS Scene Services "
                "(see module docstring)"
            ),
        },
    }

    await save_geojson(BUILDINGS_CACHE, geojson)
    print(f"  Saved {len(features)} buildings to {BUILDINGS_CACHE}")
    return features
