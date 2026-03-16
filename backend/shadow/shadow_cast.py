"""
2.5D shadow polygon computation using Shapely.

Shadow geometry
───────────────
A building is modelled as a vertical extrusion of its 2D footprint polygon.
For a given sun azimuth and elevation the ground shadow consists of:

  1. The building footprint itself (the base of the extrusion).
  2. The footprint shifted by the shadow vector (the "tip" of the shadow).
  3. A parallelogram for each footprint edge, sweeping from the original edge
     to the corresponding shifted edge.

Taking the Shapely ``unary_union`` of all these parts gives the **exact**
shadow polygon for any building shape, including non-convex outlines such as
L-shaped or U-shaped buildings.  The earlier convex-hull approximation
over-estimated shadow area for such shapes and is no longer used.

Coordinate system
─────────────────
All coordinates are (longitude, latitude) in WGS-84 degrees.
Shadow vectors are converted from metres to degrees using local scale factors:
  1° latitude  ≈ 111 320 m
  1° longitude ≈ 111 320 * cos(lat) m

Shadow radius limit
───────────────────
Nearby buildings are pre-filtered to 500 m from the pub.  At that radius a
10 m building would cast a significant shadow only when the sun elevation
is below ~1.1°.  The 500 m cutoff therefore covers all practically relevant
shadow sources.
"""

import math
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union

LAT_M_PER_DEG = 111_320.0          # metres per degree latitude (constant)
MIN_SUN_ELEVATION_DEG = 0.5         # ignore near-zero elevations (numerical noise)
MAX_SHADOW_LENGTH_M = 500.0         # cap runaway shadows at very low sun angles


def _lon_m_per_deg(lat: float) -> float:
    """Metres per degree of longitude at the given latitude."""
    return LAT_M_PER_DEG * math.cos(math.radians(lat))


def _shadow_vector(
    height: float,
    sun_azimuth: float,
    sun_elevation: float,
    ref_lat: float,
) -> tuple[float, float]:
    """
    Return the (Δlon, Δlat) shadow offset in degrees for a building of *height*
    metres given the sun position.

    The shadow is cast *opposite* the sun direction.
    """
    shadow_length_m = min(
        height / math.tan(math.radians(sun_elevation)),
        MAX_SHADOW_LENGTH_M,
    )
    # pysolar azimuth: 0° = south, increases clockwise viewed from above.
    # Shadow direction is 180° opposite the sun.
    shadow_azimuth = (sun_azimuth + 180.0) % 360.0

    az_rad = math.radians(shadow_azimuth)
    # Decompose into north (+lat) and east (+lon) components
    delta_north_m = shadow_length_m * math.cos(az_rad)
    delta_east_m  = shadow_length_m * math.sin(az_rad)

    delta_lat = delta_north_m / LAT_M_PER_DEG
    delta_lon = delta_east_m  / _lon_m_per_deg(ref_lat)

    return delta_lon, delta_lat


def compute_shadow_polygon(
    footprint: list[tuple[float, float]],
    height: float,
    sun_azimuth: float,
    sun_elevation: float,
) -> Polygon | None:
    """
    Compute the ground shadow cast by a building.

    The shadow is built as the union of:
      - The building footprint (base).
      - The footprint shifted by the shadow vector (tip).
      - One parallelogram per footprint edge, sweeping the edge from its
        original position to its shifted position.

    This is the exact shadow for extruded-prism buildings and handles
    non-convex footprints correctly (L-shapes, U-shapes, courtyards, etc.).
    The previous convex-hull approach over-estimated shadow area for such
    buildings and has been replaced by this method.

    Args:
        footprint:     List of (lon, lat) pairs forming the building outline
                       (GeoJSON ring — last coord equals first).
        height:        Building height in metres.
        sun_azimuth:   Sun azimuth in degrees (pysolar convention,
                       clockwise from north).
        sun_elevation: Sun elevation above horizon in degrees.

    Returns:
        A Shapely geometry representing the shadow area, or None if the sun
        is below the minimum elevation threshold or the footprint is
        degenerate.
    """
    if sun_elevation < MIN_SUN_ELEVATION_DEG or height <= 0 or len(footprint) < 3:
        return None

    try:
        base = Polygon(footprint)
        if not base.is_valid or base.is_empty:
            return None

        ref_lat = base.centroid.y
        dx, dy = _shadow_vector(height, sun_azimuth, sun_elevation, ref_lat)

        shifted_coords = [(x + dx, y + dy) for x, y in footprint]
        shifted = Polygon(shifted_coords)

        # Build exact shadow: base footprint + shifted tip + edge sweeps.
        # The footprint ring is closed (footprint[-1] == footprint[0]),
        # so iterating range(len - 1) covers every edge exactly once.
        parts = [base, shifted]
        for i in range(len(footprint) - 1):
            p1, p2 = footprint[i], footprint[i + 1]
            q1 = (p1[0] + dx, p1[1] + dy)
            q2 = (p2[0] + dx, p2[1] + dy)
            try:
                quad = Polygon([p1, p2, q2, q1])
                if quad.is_valid and not quad.is_empty:
                    parts.append(quad)
            except Exception:
                pass

        shadow = unary_union(parts)
        return shadow if not shadow.is_empty else None

    except Exception:
        return None


def point_in_shadow(
    point: tuple[float, float],
    buildings_near: list[dict],
    sun_azimuth: float,
    sun_elevation: float,
) -> bool:
    """
    Return True if *point* (lon, lat) is covered by the shadow of at least one
    building in *buildings_near*.

    Buildings whose footprint contains the pub point are skipped — that is the
    building the pub occupies; its terrace is outside it, and including it would
    cause the pub to always appear in shade.

    Args:
        point:          (lon, lat) of the pub.
        buildings_near: List of building dicts with keys "footprint" and "height".
        sun_azimuth:    Sun azimuth in degrees.
        sun_elevation:  Sun elevation in degrees.
    """
    if sun_elevation < MIN_SUN_ELEVATION_DEG:
        return False  # Night / horizon — no meaningful shadow

    pt = Point(point)
    pub_lon, pub_lat = point

    # Directional filter: only buildings "toward the sun" from the pub can cast
    # shadows on it. The sun is at sun_azimuth (clockwise from north), so the
    # dot product of (pub→building centroid) with the sun direction must be > 0.
    sun_az_rad = math.radians(sun_azimuth)
    sun_east  = math.sin(sun_az_rad)
    sun_north = math.cos(sun_az_rad)

    for building in buildings_near:
        footprint = building.get("footprint", [])
        height = building.get("height", 0.0)
        if len(footprint) < 3:
            continue

        # Directional pre-filter using pre-computed centroid (or fallback).
        centroid = building.get("centroid")
        if centroid is None:
            lons = [p[0] for p in footprint]
            lats = [p[1] for p in footprint]
            centroid = (sum(lons) / len(lons), sum(lats) / len(lats))
        dx = centroid[0] - pub_lon
        dy = centroid[1] - pub_lat
        if dx * sun_east + dy * sun_north <= 0:
            continue  # building is behind or beside pub relative to sun

        # Skip the building the pub itself occupies — its terrace is outside.
        try:
            if Polygon(footprint).contains(pt):
                continue
        except Exception:
            continue

        shadow = compute_shadow_polygon(footprint, height, sun_azimuth, sun_elevation)
        if shadow is not None and shadow.contains(pt):
            return True

    return False
