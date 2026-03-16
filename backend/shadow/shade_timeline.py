"""
Compute a full sun/shade timeline for a pub on a given date.

Buildings within 500 m of the pub are considered as potential shadow sources.
The timeline is sampled at 5-minute intervals across all daylight hours.
"""

import math
from datetime import date, datetime

from .solar import get_daylight_steps, get_sun_position
from .shadow_cast import point_in_shadow

BUILDING_SEARCH_RADIUS_M = 500.0    # metres — see shadow_cast.py for rationale
LAT_M_PER_DEG = 111_320.0


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Return approximate great-circle distance in metres between two WGS-84 points."""
    r = 6_371_000.0  # Earth radius metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _building_centroid(footprint: list[list[float]]) -> tuple[float, float]:
    """Return the simple arithmetic centroid (lon, lat) of a footprint ring."""
    lons = [p[0] for p in footprint]
    lats = [p[1] for p in footprint]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def find_nearby_buildings(
    pub_lon: float,
    pub_lat: float,
    buildings: list[dict],
    radius_m: float = BUILDING_SEARCH_RADIUS_M,
) -> list[dict]:
    """
    Return buildings whose centroid is within *radius_m* metres of the pub.

    Args:
        pub_lon:   Pub longitude.
        pub_lat:   Pub latitude.
        buildings: List of building dicts (GeoJSON feature properties + footprint).
        radius_m:  Search radius in metres (default 500 m).

    Returns:
        Filtered list of nearby building dicts.
    """
    nearby = []
    for b in buildings:
        footprint = b.get("footprint", [])
        if not footprint:
            continue
        clon, clat = _building_centroid(footprint)
        dist = _haversine_m(pub_lon, pub_lat, clon, clat)
        if dist <= radius_m:
            nearby.append(b)
    return nearby


def compute_shade_timeline(
    pub: dict,
    buildings: list[dict],
    target_date: date,
    step_minutes: int = 5,
    nearby_buildings: list[dict] | None = None,
) -> list[dict]:
    """
    Compute the sun/shade timeline for *pub* on *target_date*.

    Args:
        pub:               GeoJSON Feature dict for the pub (must have geometry.coordinates).
        buildings:         Full list of building dicts loaded from cache.
        target_date:       The date to evaluate.
        step_minutes:      Time resolution in minutes (default 5).
        nearby_buildings:  Pre-filtered list of nearby buildings (skips find_nearby_buildings
                           if provided — pass this when using a spatial index in the caller).

    Returns:
        List of dicts, one per daylight step:
        {
            "time":          ISO 8601 UTC string,
            "in_shade":      bool,
            "sun_azimuth":   float (degrees),
            "sun_elevation": float (degrees),
        }
    """
    coords = pub["geometry"]["coordinates"]
    pub_lon, pub_lat = float(coords[0]), float(coords[1])

    nearby = nearby_buildings if nearby_buildings is not None \
        else find_nearby_buildings(pub_lon, pub_lat, buildings)

    steps = get_daylight_steps(target_date, step_minutes)
    timeline = []

    for dt in steps:
        sun = get_sun_position(dt)
        if sun is None:
            continue  # sun below horizon — already filtered by get_daylight_steps

        in_shade = point_in_shadow(
            (pub_lon, pub_lat),
            nearby,
            sun["azimuth"],
            sun["elevation"],
        )

        timeline.append({
            "time": dt.isoformat(),
            "in_shade": in_shade,
            "sun_azimuth": round(sun["azimuth"], 2),
            "sun_elevation": round(sun["elevation"], 2),
        })

    return timeline
