"""
Fetch building footprints from the City of Zagreb DKP (Digital Cadastral Plan)
FeatureServer and optionally enrich them with OSM height data.

Background
──────────
The DKP layer is the official cadastral building registry maintained by the
City of Zagreb.  Its footprint geometries are more precise than crowd-sourced
OSM data because they come from official land-survey records.

The DKP layer itself does not carry measured building heights — only the
``VRSTA`` (building type) and ``VRSTA_UPORABE`` (usage type) classification
attributes.  Heights are therefore estimated using two sources:

  1. **OSM spatial join** (preferred): for each DKP polygon we look up the
     OSM building whose centroid falls inside it and borrow its ``height`` or
     ``building:levels`` value.
  2. **VRSTA heuristics** (fallback): when no OSM match is found, a height
     is estimated from the building type:

     VRSTA value (Croatian)  │ English              │ Estimated height
     ────────────────────────┼──────────────────────┼─────────────────
     Stambena                │ Residential          │  9 m  (3 floors)
     Poslovna                │ Commercial / office  │ 12 m  (4 floors)
     Stambeno-poslovna       │ Mixed use            │ 10 m
     Industrijska            │ Industrial           │  7 m
     Gospodarska             │ Agricultural / farm  │  5 m
     Sakralna                │ Religious            │ 15 m
     Javna                   │ Public / civic       │ 10 m
     (other / unknown)       │ —                    │  8 m  (default)

Endpoint
────────
https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services/
  zgrada_DKP_prikaz_k1/FeatureServer/0/query

The service returns up to 1 000 features per request (``exceededTransferLimit``
is set when there are more).  Pagination is handled via ``resultOffset``.

ZG3D note
─────────
The ArcGIS ZG3D SceneServer endpoints (which carry surveyed Z_Min/Z_Max
heights at LoD 2.2 accuracy) currently return HTTP 400 "Invalid URL" and are
not accessible.  Once access is restored the OSM-height enrichment step here
can be replaced with a Z_Max − Z_Min lookup from those services for
dramatically improved shadow accuracy.

Usage
─────
This module is called by ``fetch_buildings.py`` when the
``USE_DKP_FOOTPRINTS`` environment variable is set to ``1``.  You can also
run it standalone::

    cd backend
    python -m data_pipeline.fetch_buildings_dkp
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import Polygon, shape

from .cache import DATA_DIR, save_geojson, load_geojson

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DKP_BASE_URL = (
    "https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services"
    "/zgrada_DKP_prikaz_k1/FeatureServer/0"
)

# Zagreb bounding box (west, south, east, north) — EPSG:4326
BBOX_WGS84 = (15.87, 45.72, 16.12, 45.87)

BUILDINGS_DKP_CACHE = DATA_DIR / "buildings_dkp.geojson"
BUILDINGS_OSM_CACHE = DATA_DIR / "buildings.geojson"

DEFAULT_HEIGHT_M = 8.0
METRES_PER_LEVEL = 3.0

# VRSTA → estimated height in metres
VRSTA_HEIGHT: dict[str, float] = {
    "stambena":              9.0,
    "poslovna":             12.0,
    "stambeno-poslovna":    10.0,
    "industrijska":          7.0,
    "gospodarska":           5.0,
    "sakralna":             15.0,
    "javna":                10.0,
}

BATCH_SIZE    = 1_000   # max features per DKP request
MAX_BATCHES   = 300     # safety cap (~300 000 buildings)
REQUEST_TIMEOUT = 60.0  # seconds


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _esri_ring_to_coords(ring: list[list[float]]) -> list[list[float]]:
    """Convert an Esri ring [[x, y], ...] to GeoJSON [[lon, lat], ...] format.

    Esri polygon rings use [x, y] which for EPSG:4326 output means [lon, lat].
    The ring is closed (last coord == first) per GeoJSON spec; Esri rings are
    already closed so no adjustment is needed.
    """
    return ring  # already [lon, lat] when outSR=4326


def _esri_feature_to_geojson(feature: dict[str, Any]) -> dict | None:
    """Convert a single Esri feature dict to a GeoJSON Feature, or None."""
    geom  = feature.get("geometry", {})
    attrs = feature.get("attributes", {})

    rings = geom.get("rings")
    if not rings or len(rings[0]) < 3:
        return None

    # First ring is the outer boundary; subsequent rings are holes.
    coordinates = [_esri_ring_to_coords(r) for r in rings]

    vrsta = (attrs.get("VRSTA") or "").lower().strip()
    height = VRSTA_HEIGHT.get(vrsta, DEFAULT_HEIGHT_M)

    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coordinates},
        "properties": {
            "id":    f"dkp/{attrs.get('OBJECTID', '')}",
            "height":        height,
            "vrsta":         attrs.get("VRSTA", ""),
            "vrsta_uporabe": attrs.get("VRSTA_UPORABE", ""),
            "height_source": "vrsta_heuristic",
            "area_m2":       attrs.get("Shape__Area"),
        },
    }


# ---------------------------------------------------------------------------
# OSM height enrichment
# ---------------------------------------------------------------------------

def _enrich_with_osm_heights(
    dkp_features: list[dict],
    osm_features: list[dict],
) -> list[dict]:
    """
    For each DKP building whose height came from a VRSTA heuristic, look for
    an OSM building whose centroid falls inside the DKP polygon and copy its
    height.

    This is O(n × m) in the worst case but in practice OSM centroids are
    spatially indexed against DKP polygons using a Shapely STRtree, giving
    O(n log m) performance.

    Args:
        dkp_features: GeoJSON features from the DKP layer (mutable — heights
                      will be updated in-place).
        osm_features: GeoJSON features from the OSM buildings cache.

    Returns:
        The same *dkp_features* list with ``height`` and ``height_source``
        updated where a match was found.
    """
    from shapely import STRtree

    # Build Shapely polygons + height lookup for OSM buildings.
    osm_polys: list[Polygon] = []
    osm_heights: list[float] = []

    for f in osm_features:
        try:
            poly = shape(f["geometry"])
            if poly.is_valid and not poly.is_empty:
                h = f["properties"].get("height", DEFAULT_HEIGHT_M)
                osm_polys.append(poly.centroid)   # use centroid for fast lookup
                osm_heights.append(float(h))
        except Exception:
            pass

    if not osm_polys:
        return dkp_features

    osm_tree = STRtree(osm_polys)

    enriched = 0
    for f in dkp_features:
        if f["properties"].get("height_source") != "vrsta_heuristic":
            continue
        try:
            dkp_poly = shape(f["geometry"])
            if not dkp_poly.is_valid:
                continue
            # Find OSM centroids that fall inside this DKP polygon.
            candidate_idxs = osm_tree.query(dkp_poly)
            for idx in candidate_idxs:
                if dkp_poly.contains(osm_polys[idx]):
                    f["properties"]["height"]        = osm_heights[idx]
                    f["properties"]["height_source"] = "osm"
                    enriched += 1
                    break
        except Exception:
            pass

    print(f"[dkp] Enriched {enriched}/{len(dkp_features)} buildings with OSM heights.")
    return dkp_features


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

async def fetch_buildings_dkp(
    session: httpx.AsyncClient,
    enrich_from_osm: bool = True,
) -> list[dict]:
    """
    Fetch all DKP building footprints for the Zagreb bbox and return them as
    a list of GeoJSON Feature dicts.

    The results are paginated (1 000 features per page) and cached to
    ``data/buildings_dkp.geojson``.

    Args:
        session:          An open ``httpx.AsyncClient`` to reuse.
        enrich_from_osm:  If True (default) and ``data/buildings.geojson``
                          exists, overlay OSM heights onto DKP footprints.

    Returns:
        List of GeoJSON Feature dicts with polygon geometry and height.
    """
    print("[dkp] Fetching DKP building footprints from ArcGIS FeatureServer …")

    west, south, east, north = BBOX_WGS84
    bbox_str = f"{west},{south},{east},{north}"

    features: list[dict] = []
    offset = 0

    for _ in range(MAX_BATCHES):
        url = (
            f"{DKP_BASE_URL}/query"
            f"?where=1%3D1"
            f"&geometry={bbox_str}"
            f"&geometryType=esriGeometryEnvelope"
            f"&inSR=4326"
            f"&outSR=4326"
            f"&outFields=OBJECTID,VRSTA,VRSTA_UPORABE,Shape__Area"
            f"&returnGeometry=true"
            f"&resultOffset={offset}"
            f"&resultRecordCount={BATCH_SIZE}"
            f"&f=json"
        )

        try:
            resp = await session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            print(f"[dkp] Request failed at offset {offset}: {exc}")
            break

        if "error" in data:
            print(f"[dkp] API error: {data['error']}")
            break

        batch = data.get("features", [])
        if not batch:
            break

        for raw in batch:
            feat = _esri_feature_to_geojson(raw)
            if feat:
                features.append(feat)

        print(f"[dkp] Fetched {len(features)} features so far …")

        if not data.get("exceededTransferLimit", False):
            break   # all features returned in this batch

        offset += BATCH_SIZE
        # Brief pause to be polite to the ArcGIS server.
        await asyncio.sleep(0.5)

    print(f"[dkp] Total: {len(features)} DKP buildings fetched.")

    # Optionally enrich heights from OSM.
    if enrich_from_osm and BUILDINGS_OSM_CACHE.exists():
        osm_geojson = json.loads(BUILDINGS_OSM_CACHE.read_text(encoding="utf-8"))
        osm_features = osm_geojson.get("features", [])
        print(f"[dkp] Enriching heights using {len(osm_features)} OSM buildings …")
        features = _enrich_with_osm_heights(features, osm_features)

    height_sources: dict[str, int] = {}
    for f in features:
        src = f["properties"].get("height_source", "unknown")
        height_sources[src] = height_sources.get(src, 0) + 1

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "source": "City of Zagreb DKP FeatureServer",
            "count": len(features),
            "height_sources": height_sources,
            "note": (
                "Footprints: official cadastral data. "
                "Heights: OSM where available, VRSTA heuristic otherwise. "
                "For surveyed heights see ZG3D SceneServer (currently inaccessible)."
            ),
        },
    }

    await save_geojson(BUILDINGS_DKP_CACHE, geojson)
    print(f"[dkp] Saved to {BUILDINGS_DKP_CACHE}")
    return features


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    async with httpx.AsyncClient() as session:
        features = await fetch_buildings_dkp(session)
    print(f"Done — {len(features)} buildings written to {BUILDINGS_DKP_CACHE}")


if __name__ == "__main__":
    asyncio.run(_main())
