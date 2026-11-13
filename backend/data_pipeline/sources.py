"""
Download the raw inputs and cache them under ``data/`` (git-ignored).

  pubs.geojson       OSM venues (pub / bar / biergarten, cafés with seating)
  streets.geojson    OSM streets, used to find the street side of a building
  buildings.geojson  OSM building footprints (gap filler outside ZG3D)
  trees.geojson      City of Zagreb tree register: position, height and
                     crown-width class, species
  zg3d.geojson       City of Zagreb ZG3D LoD2 roof polygons with surveyed
                     Z_Min / Z_Max elevations — the primary height source

Licences: OSM data © OpenStreetMap contributors (ODbL).  ZG3D © Grad Zagreb,
"Otvorena dozvola" (Croatian open licence).
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import shapely
from shapely.geometry import LineString, mapping
from shapely.ops import polygonize, unary_union

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))

# Zagreb bounding box
SOUTH, WEST, NORTH, EAST = 45.72, 15.87, 45.87, 16.12

# overpass-api.de answers 406 to library-default User-Agents, so identify the app.
HEADERS = {"User-Agent": "pub-shade-map/0.2 (+https://github.com/skuxy/pub-shade-map)"}

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

ARCGIS = "https://services8.arcgis.com/Usi0jGQwMmBUpFjr/arcgis/rest/services"

METRES_PER_LEVEL = 3.3     # calibrated against ZG3D roof heights
ROOF_ALLOWANCE_M = 3.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _save(name: str, features: list[dict], **meta) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"type": "FeatureCollection", "metadata": meta, "features": features},
        ensure_ascii=False,
    ), encoding="utf-8")
    tmp.replace(path)                       # never leave a half-written cache
    print(f"  saved {len(features):,} features → {path.name}")
    return path


def load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))["features"]


def overpass(body: str, timeout_s: int = 180) -> list[dict]:
    """Run an Overpass QL query body over the Zagreb bbox; return its elements."""
    query = f"[out:json][timeout:{timeout_s}][bbox:{SOUTH},{WEST},{NORTH},{EAST}];{body}"
    last: Exception | None = None
    for mirror in OVERPASS_MIRRORS:
        for attempt in range(3):
            try:
                r = httpx.post(mirror, data={"data": query}, headers=HEADERS,
                               timeout=timeout_s + 60)
                r.raise_for_status()
                reply = r.json()
                # A server-side timeout still answers 200, with a partial list.
                if "error" in reply.get("remark", ""):
                    raise ValueError(f"Overpass gave up: {reply['remark']}")
                return reply["elements"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last = exc
                print(f"  {mirror} attempt {attempt + 1} failed: {exc!r}")
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"all Overpass mirrors failed: {last!r}")


# ---------------------------------------------------------------------------
# OSM venues
# ---------------------------------------------------------------------------
def fetch_pubs() -> Path:
    print("Fetching venues from OSM …")
    elements = overpass(
        # Zagreb's caffe bars are mostly amenity=cafe without an outdoor_seating
        # tag, so take every cafe; the ones tagged "no" are dropped below.
        '(nwr["amenity"~"^(pub|bar|biergarten|cafe)$"];);'
        "out center tags;"
    )
    features = []
    for el in elements:
        tags = el.get("tags", {})
        pos = el if el["type"] == "node" else el.get("center")
        if not pos or tags.get("outdoor_seating") == "no":
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pos["lon"], pos["lat"]]},
            "properties": {
                "id": f"{el['type']}/{el['id']}",
                "name": tags.get("name") or tags.get("name:hr") or tags.get("name:en") or "Unnamed",
                "amenity": tags.get("amenity", ""),
                "outdoor_seating": tags.get("outdoor_seating", ""),
                "address": " ".join(filter(None, [tags.get("addr:street"),
                                                  tags.get("addr:housenumber")])),
                "website": tags.get("website") or tags.get("contact:website", ""),
                "opening_hours": tags.get("opening_hours", ""),
            },
        })
    return _save("pubs.geojson", features, source="OpenStreetMap")


# ---------------------------------------------------------------------------
# OSM streets
# ---------------------------------------------------------------------------
def fetch_streets() -> Path:
    print("Fetching streets from OSM …")
    elements = overpass(
        'way["highway"~"^(pedestrian|living_street|residential|unclassified|'
        'tertiary|secondary|primary)$"];out geom;'
    )
    features = [{
        "type": "Feature",
        "geometry": {"type": "LineString",
                     "coordinates": [[p["lon"], p["lat"]] for p in el["geometry"]]},
        "properties": {"id": f"way/{el['id']}", "highway": el["tags"]["highway"]},
    } for el in elements if len(el.get("geometry", [])) >= 2]
    return _save("streets.geojson", features, source="OpenStreetMap")


# ---------------------------------------------------------------------------
# OSM buildings
# ---------------------------------------------------------------------------
_NUMBER = re.compile(r"^\s*~?\s*(\d+(?:[.,]\d+)?)\s*(m|ft|feet|')?\s*$", re.I)


def parse_osm_height(tags: dict) -> float | None:
    """
    Height of the top of a building above ground, in metres, from OSM tags —
    or None when the tags say nothing usable.

    ``height`` wins ("12", "12.5 m", "12,5", "40 ft", "40'").  Otherwise
    ``building:levels`` (+ ``roof:levels``) × 3.3 m + 3 m of roof.
    """
    m = _NUMBER.match(str(tags.get("height", "")))
    if m:
        value = float(m.group(1).replace(",", "."))
        if m.group(2) and m.group(2).lower() != "m":
            value *= 0.3048
        if value > 0:
            return value
    try:
        levels = float(str(tags["building:levels"]).replace(",", "."))
        levels += float(str(tags.get("roof:levels", 0)).replace(",", "."))
    except (KeyError, ValueError):
        return None
    return levels * METRES_PER_LEVEL + ROOF_ALLOWANCE_M if levels > 0 else None


def _rings(members: list[dict], role: str):
    lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
             for m in members
             if m.get("type") == "way" and (m.get("role") or "outer") == role and len(m.get("geometry", [])) >= 2]
    return unary_union(list(polygonize(unary_union(lines)))) if lines else None


def _building_geometry(el: dict):
    if el["type"] == "way":
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(pts) < 4 or pts[0] != pts[-1]:
            return None
        geom = shapely.Polygon(pts)
    else:                                    # multipolygon relation, holes kept
        outer = _rings(el.get("members", []), "outer")
        if outer is None:
            return None
        inner = _rings(el.get("members", []), "inner")
        geom = outer.difference(inner) if inner is not None else outer
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    return geom if not geom.is_empty and geom.area > 0 else None


def fetch_osm_buildings() -> Path:
    print("Fetching building footprints from OSM (large) …")
    elements = overpass('(way["building"];relation["building"];);out geom;', timeout_s=300)
    features = []
    for el in elements:
        geom = _building_geometry(el)
        if geom is None or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        tags = el.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "id": f"{el['type']}/{el['id']}",
                "building": tags.get("building", "yes"),
                "height": parse_osm_height(tags),
            },
        })
    return _save("buildings.geojson", features, source="OpenStreetMap")


# ---------------------------------------------------------------------------
# ZG3D (City of Zagreb LoD2 roofs)
# ---------------------------------------------------------------------------
def _arcgis(url: str, params: dict) -> dict:
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.get(url, params=params, headers=HEADERS, timeout=120)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"ArcGIS request failed: {url}: {last!r}")


def _zg3d_layer(name: str) -> list[dict]:
    url = f"{ARCGIS}/{name}/FeatureServer/0/query"
    where = {
        "where": "1=1",
        "geometry": f"{WEST},{SOUTH},{EAST},{NORTH}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }
    count = _arcgis(url, {**where, "returnCountOnly": "true", "f": "json"})["count"]
    features: list[dict] = []
    for offset in range(0, count, 2000):
        page = _arcgis(url, {
            **where,
            "outFields": "OBJECTID,Godina_izv,Z_Min,Z_Max",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "geometryPrecision": 7,
            "f": "geojson",
        })["features"]
        for f in page:
            p = f["properties"]
            f["properties"] = {"id": f"{name}/{p['OBJECTID']}", "year": p.get("Godina_izv"),
                               "z_min": p.get("Z_Min"), "z_max": p.get("Z_Max")}
        features += page
    if len(features) != count:
        raise RuntimeError(f"{name}: expected {count} features, got {len(features)}")
    print(f"  {name}: {count:,}")
    return features


def fetch_zg3d() -> Path:
    print("Fetching ZG3D roof polygons from the City of Zagreb …")
    # Service names are discovered, not hard-coded: they are not uniform
    # (Donji Grad is "ZG3D_GC_Donji_Grad_2022_" with a trailing underscore).
    services = _arcgis(ARCGIS, {"f": "json"})["services"]
    layers = sorted(s["name"] for s in services
                    if s["name"].startswith("ZG3D_GC_") and s["type"] == "FeatureServer")
    if not layers:
        raise RuntimeError("no ZG3D_GC_* FeatureServer layers found")
    with ThreadPoolExecutor(4) as pool:
        features = [f for layer in pool.map(_zg3d_layer, layers) for f in layer]
    return _save("zg3d.geojson", features, source="Grad Zagreb ZG3D", layers=layers)


# ---------------------------------------------------------------------------
# City tree register
# ---------------------------------------------------------------------------
# The register records size in classes, not measurements; use class midpoints.
TREE_HEIGHT_M = {"do 5m": 4.0, "od 6 od 10m": 8.0, "od 11 od 15m": 13.0,
                 "od 16 od 20m": 18.0, "od 21 od 25m": 23.0, "više od 25m": 28.0}
TREE_CROWN_M = {"do 5m": 4.0, "od 6 do 10m": 8.0, "od 11 do 15m": 13.0, "više od 15m": 17.0}
# Genera (or full names) that keep their leaves in winter.  Larch, bald cypress,
# dawn redwood and ginkgo are conifers or look like them but are deciduous.
EVERGREEN = ("PINUS", "PICEA", "ABIES", "CHAMAECYPARIS", "THUJA", "THUJOPSIS", "TAXUS",
             "CEDRUS", "JUNIPERUS", "CUPRESSUS", "CUPRESSOCYPARIS", "CUPRESOCYPARIS", "X CUPRESSOCYPARIS",
             "PSEUDOTSUGA", "TSUGA", "SEQUOIADENDRON", "SEQUOIA", "CALOCEDRUS",
             "PLATYCLADUS", "CRYPTOMERIA", "ILEX", "BUXUS", "MAGNOLIA GRANDIFLORA",
             "PRUNUS LAUROCERASUS", "QUERCUS ILEX", "LAURUS", "WASHINGTONIA")


def is_evergreen(species: str) -> bool:
    name = " ".join((species or "").upper().split())
    return any(name == e or name.startswith(e + " ") for e in EVERGREEN)


def fetch_trees() -> Path:
    print("Fetching the tree register from the City of Zagreb …")
    url = f"{ARCGIS}/stablo_SpatialJoin/FeatureServer/0/query"
    where = {
        "where": "1=1",
        "geometry": f"{WEST},{SOUTH},{EAST},{NORTH}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }
    count = _arcgis(url, {**where, "returnCountOnly": "true", "f": "json"})["count"]

    def page(offset: int) -> list[dict]:
        return _arcgis(url, {
            **where,
            "outFields": "OBJECTID,vrsta,visina,pr_krosnje",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "outSR": 4326,
            "geometryPrecision": 7,
            "f": "geojson",
        })["features"]

    with ThreadPoolExecutor(4) as pool:
        raw = [f for chunk in pool.map(page, range(0, count, 2000)) for f in chunk]
    if len(raw) != count:
        raise RuntimeError(f"trees: expected {count} features, got {len(raw)}")

    features = []
    for f in raw:
        p = f["properties"]
        height = TREE_HEIGHT_M.get((p.get("visina") or "").strip())
        crown = TREE_CROWN_M.get((p.get("pr_krosnje") or "").strip())
        if f.get("geometry") is None or height is None or crown is None:
            continue                      # size not recorded
        species = " ".join((p.get("vrsta") or "").split())
        f["properties"] = {"id": p["OBJECTID"], "species": species, "height": height,
                           "crown": crown, "evergreen": is_evergreen(species)}
        features.append(f)
    print(f"  {len(features):,} of {count:,} trees have a recorded size")
    return _save("trees.geojson", features, source="Grad Zagreb, katastar zelenila (stabla)")


FETCHERS = {
    "trees.geojson": fetch_trees,
    "pubs.geojson": fetch_pubs,
    "streets.geojson": fetch_streets,
    "buildings.geojson": fetch_osm_buildings,
    "zg3d.geojson": fetch_zg3d,
}


def ensure(refresh: bool = False) -> None:
    """Download whatever is missing from the cache (everything if *refresh*)."""
    for name, fetch in FETCHERS.items():
        if refresh or not (DATA_DIR / name).exists():
            fetch()
