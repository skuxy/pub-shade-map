"""
2.5D shadow polygon computation using Shapely.

A building is modelled as a vertical extrusion of its 2D footprint polygon.
Given the sun's azimuth and elevation, the shadow on the ground is approximated
as the convex hull of the footprint vertices plus the same vertices shifted by
the shadow vector.

Coordinate system note:
  All coordinates are (longitude, latitude) in WGS-84 degrees.
  Shadow vectors are converted from metres to degrees using local scale factors:
    1° latitude  ≈ 111 320 m
    1° longitude ≈ 111 320 * cos(lat) m

Shadow radius limit:
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

    Args:
        footprint:     List of (lon, lat) pairs forming the building outline.
        height:        Building height in metres.
        sun_azimuth:   Sun azimuth in degrees (pysolar convention).
        sun_elevation: Sun elevation above horizon in degrees.

    Returns:
        A Shapely Polygon representing the shadow area, or None if the sun is
        below the minimum elevation threshold or the footprint is degenerate.
    """
    if sun_elevation < MIN_SUN_ELEVATION_DEG or height <= 0 or len(footprint) < 3:
        return None

    try:
        base = Polygon(footprint)
        if not base.is_valid or base.is_empty:
            return None

        # Reference latitude for degree conversion (centroid)
        ref_lat = base.centroid.y

        dx, dy = _shadow_vector(height, sun_azimuth, sun_elevation, ref_lat)

        # Shift every vertex of the footprint by the shadow vector
        shifted_coords = [(x + dx, y + dy) for x, y in footprint]

        # Shadow = convex hull of original footprint + shifted footprint
        shadow = Polygon(footprint).union(Polygon(shifted_coords)).convex_hull
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

    for building in buildings_near:
        footprint = building.get("footprint", [])
        height = building.get("height", 0.0)
        if len(footprint) < 3:
            continue

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
