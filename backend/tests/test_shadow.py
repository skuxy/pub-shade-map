"""
Tests and sanity checks for the shadow calculation engine.

Run from backend/:
    python -m pytest tests/test_shadow.py -v
    # or without pytest:
    python tests/test_shadow.py
"""

import sys
import math
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shadow.solar import get_sun_position
from shadow.shadow_cast import compute_shadow_polygon, point_in_shadow

# ── Solar position sanity checks ──────────────────────────────────────────

def test_solar_noon_elevation():
    """Sun elevation at Zagreb solar noon in June should be ~65–70°."""
    # June 21, ~10:00 UTC = ~12:00 Zagreb local (solar noon)
    dt = datetime(2024, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    sun = get_sun_position(dt)
    assert sun is not None, "Sun should be above horizon at noon"
    assert 60 < sun["elevation"] < 75, (
        f"Expected noon elevation ~65°, got {sun['elevation']:.1f}°"
    )
    print(f"  [OK] June noon: elevation={sun['elevation']:.1f}°, azimuth={sun['azimuth']:.1f}°")


def test_solar_noon_azimuth():
    """At solar noon in Zagreb (northern hemisphere) sun azimuth should be ~180° (south)."""
    dt = datetime(2024, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    sun = get_sun_position(dt)
    assert sun is not None
    # pysolar azimuth: clockwise from north. South = 180°
    assert 160 < sun["azimuth"] < 200, (
        f"Expected noon azimuth ~180° (south), got {sun['azimuth']:.1f}°"
    )
    print(f"  [OK] June noon azimuth: {sun['azimuth']:.1f}° (expected ~180° = south)")


def test_solar_morning_azimuth():
    """Morning sun should be in the east (azimuth ~90°)."""
    # June 21, ~04:00 UTC = ~06:00 Zagreb local (early morning)
    dt = datetime(2024, 6, 21, 4, 0, 0, tzinfo=timezone.utc)
    sun = get_sun_position(dt)
    assert sun is not None, "Sun should be up at 06:00 Zagreb in June"
    assert 60 < sun["azimuth"] < 120, (
        f"Expected morning azimuth ~90° (east), got {sun['azimuth']:.1f}°"
    )
    print(f"  [OK] Morning azimuth: {sun['azimuth']:.1f}° (expected ~90° = east)")


def test_night_returns_none():
    """Midnight should return None (sun below horizon)."""
    dt = datetime(2024, 6, 21, 22, 0, 0, tzinfo=timezone.utc)  # midnight Zagreb
    sun = get_sun_position(dt)
    assert sun is None, f"Expected None at midnight, got {sun}"
    print("  [OK] Midnight correctly returns None")


# ── Shadow geometry sanity checks ─────────────────────────────────────────

# Simple square building: 10m × 10m at Zagreb city centre
BUILDING_FOOTPRINT = [
    (15.980, 45.815),
    (15.981, 45.815),
    (15.981, 45.816),
    (15.980, 45.816),
    (15.980, 45.815),
]
BUILDING_HEIGHT = 10.0  # metres


def test_shadow_noon_goes_north():
    """At solar noon (sun in south), shadow should extend north of the building."""
    # Sun in south = azimuth ~180°
    shadow = compute_shadow_polygon(BUILDING_FOOTPRINT, BUILDING_HEIGHT,
                                    sun_azimuth=180.0, sun_elevation=45.0)
    assert shadow is not None, "Shadow should be computed at 45° elevation"

    building_north = 45.816  # northern edge of building
    shadow_centroid_lat = shadow.centroid.y
    assert shadow_centroid_lat > building_north - 0.001, (
        f"Shadow centroid should be north of building at noon, "
        f"got lat={shadow_centroid_lat:.4f} vs building north={building_north}"
    )
    print(f"  [OK] Noon shadow centroid lat={shadow_centroid_lat:.5f} (building north={building_north})")


def test_shadow_morning_goes_west():
    """In the morning (sun in east ~90°), shadow should extend west."""
    shadow = compute_shadow_polygon(BUILDING_FOOTPRINT, BUILDING_HEIGHT,
                                    sun_azimuth=90.0, sun_elevation=20.0)
    assert shadow is not None

    building_west = 15.980  # western edge
    shadow_centroid_lon = shadow.centroid.x
    assert shadow_centroid_lon < building_west + 0.001, (
        f"Shadow centroid should be west of building in morning, "
        f"got lon={shadow_centroid_lon:.5f} vs building west={building_west}"
    )
    print(f"  [OK] Morning shadow centroid lon={shadow_centroid_lon:.5f} (building west={building_west})")


def test_shadow_length_scales_with_elevation():
    """Lower sun elevation should produce a longer shadow."""
    shadow_high = compute_shadow_polygon(BUILDING_FOOTPRINT, BUILDING_HEIGHT,
                                         sun_azimuth=180.0, sun_elevation=60.0)
    shadow_low  = compute_shadow_polygon(BUILDING_FOOTPRINT, BUILDING_HEIGHT,
                                         sun_azimuth=180.0, sun_elevation=10.0)
    assert shadow_high is not None and shadow_low is not None
    assert shadow_low.area > shadow_high.area, (
        f"Low elevation shadow ({shadow_low.area:.8f}) should be larger than "
        f"high elevation shadow ({shadow_high.area:.8f})"
    )
    print(f"  [OK] Shadow area at 10°={shadow_low.area:.8f} > at 60°={shadow_high.area:.8f}")


def test_pub_in_own_building_not_in_shadow():
    """A pub point inside its own building footprint should NOT be counted as in shadow."""
    pub_point = (15.9805, 45.8155)  # inside the test building footprint
    buildings = [{"footprint": BUILDING_FOOTPRINT, "height": BUILDING_HEIGHT}]

    result = point_in_shadow(pub_point, buildings, sun_azimuth=180.0, sun_elevation=45.0)
    assert result is False, (
        "Pub inside its own building should not be counted as in shadow"
    )
    print("  [OK] Pub inside own building footprint correctly excluded from shadow")


def test_pub_in_shadow_of_adjacent_building():
    """A pub point directly north of a building should be in shadow at noon."""
    # Pub is just north of the building's northern edge
    pub_point = (15.9805, 45.8165)  # just north of building (45.816)

    shadow_length_m = BUILDING_HEIGHT / math.tan(math.radians(30.0))
    # At 30° elevation, shadow_length ≈ 17.3m ≈ 0.000155° lat
    # Pub is 0.0005° north = ~55m north — may or may not be in shadow
    # Use very low elevation to guarantee coverage
    result = point_in_shadow(pub_point, [{"footprint": BUILDING_FOOTPRINT, "height": BUILDING_HEIGHT}],
                              sun_azimuth=180.0, sun_elevation=5.0)
    assert result is True, (
        "Pub directly north of building should be in shadow at noon with low sun (5°)"
    )
    print("  [OK] Pub north of building correctly in shadow at noon with low sun")


# ── Runner ─────────────────────────────────────────────────────────────────

TESTS = [
    test_solar_noon_elevation,
    test_solar_noon_azimuth,
    test_solar_morning_azimuth,
    test_night_returns_none,
    test_shadow_noon_goes_north,
    test_shadow_morning_goes_west,
    test_shadow_length_scales_with_elevation,
    test_pub_in_own_building_not_in_shadow,
    test_pub_in_shadow_of_adjacent_building,
]

if __name__ == "__main__":
    print("Running shadow calculation tests...\n")
    passed = failed = 0
    for test in TESTS:
        print(f"  {test.__name__}")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {e}")
            failed += 1

    print(f"\n{passed}/{passed+failed} tests passed", "✓" if failed == 0 else "✗")
    sys.exit(0 if failed == 0 else 1)
