"""
Sun position calculations for Zagreb using pysolar.

Zagreb coordinates: 45.815°N, 15.982°E, ~120 m elevation.
All datetimes are UTC-aware. Local Zagreb time is UTC+1 (CET) or UTC+2 (CEST).
"""

import math
from datetime import date, datetime, timedelta, timezone

from pysolar.solar import get_altitude, get_azimuth

ZAGREB_LAT = 45.815
ZAGREB_LON = 15.982
ZAGREB_ELEVATION_M = 120.0


def get_sun_position(dt: datetime) -> dict | None:
    """
    Return solar azimuth and elevation for Zagreb at the given UTC datetime.

    Returns None when the sun is at or below the horizon (elevation <= 0).

    Args:
        dt: timezone-aware UTC datetime.

    Returns:
        {"azimuth": float, "elevation": float} in degrees, or None.
    """
    if dt.tzinfo is None:
        raise ValueError("dt must be timezone-aware (UTC)")

    elevation = get_altitude(ZAGREB_LAT, ZAGREB_LON, dt, ZAGREB_ELEVATION_M)
    if elevation <= 0:
        return None

    azimuth = get_azimuth(ZAGREB_LAT, ZAGREB_LON, dt, ZAGREB_ELEVATION_M)

    return {"azimuth": float(azimuth), "elevation": float(elevation)}


def get_daylight_steps(target_date: date, step_minutes: int = 5) -> list[datetime]:
    """
    Return a list of UTC datetimes spaced *step_minutes* apart covering all
    daylight hours on *target_date* for Zagreb.

    Only timestamps where sun elevation > 0 are included.

    Args:
        target_date: The calendar date (Zagreb local, used as UTC date boundary).
        step_minutes: Interval between steps in minutes (default 5).

    Returns:
        List of timezone-aware UTC datetimes during daylight.
    """
    steps = []
    # Walk the full 24 h of the date in UTC
    start = datetime(target_date.year, target_date.month, target_date.day,
                     0, 0, 0, tzinfo=timezone.utc)
    total_steps = (24 * 60) // step_minutes

    for i in range(total_steps):
        dt = start + timedelta(minutes=i * step_minutes)
        elevation = get_altitude(ZAGREB_LAT, ZAGREB_LON, dt, ZAGREB_ELEVATION_M)
        if elevation > 0:
            steps.append(dt)

    return steps
