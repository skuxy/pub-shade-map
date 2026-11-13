"""
Turn the cached downloads into one ``Scene`` of shade-casting buildings.

ZG3D roof polygons are the obstacles wherever the city surveyed them: each
carries the elevation of its lowest and highest point, so a roof is placed at
its true absolute height.  OSM footprints fill the gaps (outside the city
limits, or buildings newer than the survey), with heights from OSM tags or,
failing that, a typical height for the footprint's size.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import shape

from shadow.horizon import LocalProjection, Scene

from . import sources

CENTRE = LocalProjection(lon0=15.982, lat0=45.815)

ZG3D_MIN_HEIGHT_M = 1.0       # lower: kerbs, walls, slivers
ZG3D_MAX_HEIGHT_M = 120.0     # higher than anything in Zagreb: broken meshes
OSM_GAP_COVERAGE = 0.30       # OSM footprint is "missing from ZG3D" below this

# Median ZG3D height of Zagreb buildings by OSM footprint area (m²).  Used
# only for OSM gap-fillers that carry no height or levels tag.
AREA_CLASS_HEIGHT_M = [(40, 4.2), (80, 6.6), (150, 9.2), (300, 10.7), (600, 13.3), (np.inf, 14.3)]


def _polygonal(geom):
    """Valid polygonal part of *geom*, or None."""
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        geom = shapely.union_all([g for g in geom.geoms
                                  if g.geom_type in ("Polygon", "MultiPolygon")])
    if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return geom


def _fallback_height(area_m2: float) -> float:
    return next(h for limit, h in AREA_CLASS_HEIGHT_M if area_m2 < limit)


def build_scene() -> tuple[Scene, dict]:
    """Return the Scene (local metres around ``CENTRE``) and a few counts."""
    # ── ZG3D ────────────────────────────────────────────────────────────
    polys, heights, bases, seen = [], [], [], set()
    raw = sources.load("zg3d.geojson")
    for f in raw:
        p = f["properties"]
        if p["z_min"] is None or p["z_max"] is None or f["geometry"] is None:
            continue
        h = p["z_max"] - p["z_min"]
        if not ZG3D_MIN_HEIGHT_M <= h <= ZG3D_MAX_HEIGHT_M:
            continue
        geom = _polygonal(shape(f["geometry"]))
        if geom is None or geom.wkb in seen:      # duplicates across district borders
            continue
        seen.add(geom.wkb)
        polys.append(geom)
        heights.append(h)
        bases.append(p["z_min"])
    zg = CENTRE.project(np.array(polys, dtype=object))
    n_zg3d = len(zg)

    # ── OSM gap fill ────────────────────────────────────────────────────
    osm = sources.load("buildings.geojson")
    og = np.array([_polygonal(shape(f["geometry"])) for f in osm], dtype=object)
    ok = np.array([g is not None for g in og], dtype=bool)
    osm = [f for f, keep in zip(osm, ok) if keep]
    og = CENTRE.project(og[ok])
    area = shapely.area(og)

    oi, zi = STRtree(zg).query(og, predicate="intersects")
    covered = np.zeros(len(og))
    np.add.at(covered, oi, shapely.area(shapely.intersection(og[oi], zg[zi])))
    gap = covered / np.maximum(area, 1e-9) < OSM_GAP_COVERAGE

    n_tagged = 0
    for i in np.flatnonzero(gap):
        h = osm[i]["properties"]["height"]
        n_tagged += h is not None
        heights.append(h if h is not None else _fallback_height(area[i]))
        bases.append(np.nan)

    streets = CENTRE.project(np.array(
        [shape(f["geometry"]) for f in sources.load("streets.geojson")], dtype=object))

    scene = Scene(np.concatenate([zg, og[gap]]), heights, bases, streets)
    stats = {
        "zg3d_polygons": n_zg3d,
        "osm_gap_buildings": int(gap.sum()),
        "osm_gap_with_tagged_height": int(n_tagged),
        "streets": len(streets),
    }
    return scene, stats
