"""
Tests for the horizon-profile shade model (shadow/horizon.py).

Scenes are tiny and synthetic so every expected value can be worked out by
hand: metres east (x) and north (y) of the observer, heights in metres.

Run from backend/:  python -m pytest tests -q
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon, box

sys.path.insert(0, str(Path(__file__).parent.parent))

from shadow.horizon import (  # noqa: E402
    LocalProjection, Scene, horizon_at, is_shaded, TERRACE_OFFSET_M,
)


def deg(h, d):
    return math.degrees(math.atan2(h, d))


# ── horizon ───────────────────────────────────────────────────────────────

def test_empty_scene_is_all_sky():
    assert not Scene([], []).horizon(0, 0).any()


def test_block_to_the_north():
    """10 m block whose south wall is 10 m north of the observer → 45° due north."""
    scene = Scene([box(-5, 10, 5, 20)], [10.0])
    hz = scene.horizon(0, 0)
    assert hz[0] == pytest.approx(45.0)
    # 20° east of north the ray meets the same wall further along: d = 10/cos(20°)
    assert hz[20] == pytest.approx(deg(10, 10 / math.cos(math.radians(20))))
    assert hz[340] == pytest.approx(hz[20])
    # The wall spans ±atan(5/10) = ±26.57°; outside that the sky is open.
    assert hz[27] == 0 and hz[333] == 0
    assert hz[90] == 0 and hz[180] == 0 and hz[270] == 0


def test_azimuth_is_clockwise_from_north():
    """A block due east must show up at azimuth 90, not 270."""
    scene = Scene([box(10, -5, 20, 5)], [10.0])
    hz = scene.horizon(0, 0)
    assert hz[90] == pytest.approx(45.0)
    assert hz[270] == 0


def test_nearest_building_wins_only_if_it_subtends_more():
    """A low wall close by (5 m at 5 m = 45°) versus a tower behind it (60 m at 30 m ≈ 63.4°)."""
    scene = Scene([box(-5, 5, 5, 6), box(-5, 30, 5, 40)], [5.0, 60.0])
    assert scene.horizon(0, 0)[0] == pytest.approx(deg(60, 30))


def test_concave_building_does_not_shade_its_own_notch():
    """
    U-shaped block open to the south, observer inside the notch.  A convex
    hull would bury the observer; the real building leaves the south open.
    """
    u_shape = Polygon([(-15, 0), (-15, 30), (15, 30), (15, 0), (10, 0),
                       (10, 20), (-10, 20), (-10, 0)])
    scene = Scene([u_shape], [12.0])
    hz = scene.horizon(0, 10)
    assert hz[180] == 0                               # open side
    assert hz[0] == pytest.approx(deg(12, 10))        # back wall 10 m north
    assert hz[90] == pytest.approx(deg(12, 10))       # east arm 10 m away
    assert hz[270] == pytest.approx(deg(12, 10))


def test_courtyard_is_enclosed_on_all_sides():
    """Observer in the hole of a ring-shaped block: walls in every direction."""
    ring = Polygon(box(-20, -20, 20, 20).exterior.coords,
                   [box(-10, -10, 10, 10).exterior.coords])
    scene = Scene([ring], [15.0])
    hz = scene.horizon(0, 0)
    assert hz.min() >= deg(15, 10 * math.sqrt(2)) - 1e-6   # corners are furthest
    assert hz[0] == pytest.approx(deg(15, 10))


def test_long_building_with_far_centroid_still_counts():
    """
    300 m long slab whose centroid is 150 m away and *behind* the observer in
    the east-west sense; its near end is 5 m north.  Centroid-based
    pre-filters used to drop it.
    """
    scene = Scene([box(-300, 5, 2, 15)], [20.0])
    assert scene.horizon(0, 0)[0] == pytest.approx(deg(20, 5))


def test_tall_distant_tower_is_seen():
    """A 100 m tower 600 m away subtends 9.5° — well above a low winter sun."""
    scene = Scene([box(-10, 600, 10, 620)], [100.0])
    assert scene.horizon(0, 0)[0] == pytest.approx(deg(100, 600))


def test_irrelevantly_low_obstructions_are_dropped():
    """8 m at 600 m is 0.76°, below the 1° floor."""
    scene = Scene([box(-10, 600, 10, 620)], [8.0])
    assert scene.horizon(0, 0)[0] == 0


def test_building_containing_the_point_is_ignored():
    scene = Scene([box(-5, -5, 5, 5), box(-5, 10, 5, 20)], [10.0, 10.0])
    hz = scene.horizon(0, 0)
    assert hz[0] == pytest.approx(deg(10, 10))   # only the neighbour counts
    assert hz[180] == 0


# ── lookup ────────────────────────────────────────────────────────────────

def test_is_shaded_compares_elevation_with_horizon():
    hz = Scene([box(-50, 10, 50, 20)], [10.0]).horizon(0, 0)   # 45° to the north
    assert is_shaded(hz, 0, 30)
    assert not is_shaded(hz, 0, 50)
    assert not is_shaded(hz, 180, 5)
    assert not is_shaded(hz, 180, 0.2)       # low sun, open sky: still sun
    assert is_shaded(hz, 180, 0)             # sun on or below the horizon
    assert is_shaded(hz, 180, -2)


def test_horizon_at_uses_the_nearest_sample_and_wraps():
    hz = np.zeros(360)
    hz[0] = 30
    assert horizon_at(hz, 0) == 30
    assert horizon_at(hz, 359.6) == 30       # wraps 359.6 → 0
    assert horizon_at(hz, 0.4) == 30
    assert horizon_at(hz, 0.6) == 0
    assert horizon_at(hz, 359.4) == 0
    assert horizon_at(hz, 360) == 30
    assert horizon_at(hz, -0.2) == 30


def test_infinite_wall_profile():
    """Long wall 10 m to the south, 10 m high: horizon(az) = atan(cos(az − 180°))."""
    hz = Scene([box(-1000, -20, 1000, -10)], [10.0]).horizon(0, 0)
    assert hz[180] == pytest.approx(45.0)
    assert hz[135] == pytest.approx(35.2644, abs=1e-4)
    assert hz[225] == pytest.approx(35.2644, abs=1e-4)
    assert hz[120] == pytest.approx(26.5651, abs=1e-4)
    assert hz[90] == 0 and hz[270] == 0 and hz[0] == 0


def test_corner_is_hit_exactly():
    """20 m block spanning x 10..30, y −10..10: its NW corner is at azimuth 45°."""
    hz = Scene([box(10, -10, 30, 10)], [20.0]).horizon(0, 0)
    assert hz[90] == pytest.approx(63.4349, abs=1e-4)             # atan(20/10)
    assert hz[45] == pytest.approx(deg(20, 10 * math.sqrt(2)), abs=1e-4)
    assert hz[44] == 0 and hz[136] == 0


# ── ground elevation ──────────────────────────────────────────────────────

def test_uphill_building_counts_from_its_absolute_roof():
    """
    Observer's surroundings sit at 120 m; a 10 m house 50 m north stands on
    ground 15 m higher.  Seen from below its roof is 25 m up, not 10.
    """
    here, uphill = box(-30, -30, -20, -20), box(-5, 50, 5, 60)
    scene = Scene([here, uphill], [5.0, 10.0], bases=[120.0, 135.0])
    assert scene.ground_at(0, 0) == 120.0
    assert scene.horizon(0, 0, ground=120.0)[0] == pytest.approx(deg(25, 50))
    assert scene.horizon(0, 0)[0] == pytest.approx(deg(10, 50))          # level-ground fallback


def test_downhill_roof_below_the_observer_is_no_obstacle():
    scene = Scene([box(-5, 20, 5, 30)], [10.0], bases=[100.0])
    assert scene.horizon(0, 0, ground=115.0)[0] == 0


def test_building_without_a_base_is_treated_as_level():
    scene = Scene([box(-5, 20, 5, 30)], [10.0], bases=[float("nan")])
    assert scene.ground_at(0, 0) is None
    assert scene.horizon(0, 0, ground=300.0)[0] == pytest.approx(deg(10, 20))


# ── terrace point ─────────────────────────────────────────────────────────

def test_terrace_point_outdoors_is_kept():
    scene = Scene([box(10, 10, 20, 20)], [10.0])
    assert scene.terrace_point(0, 0) == (0, 0, "open")


def test_terrace_point_moves_to_nearest_wall():
    """Venue mapped 2 m inside the south wall → terrace 3 m outside it."""
    scene = Scene([box(-10, 0, 10, 20)], [10.0])
    x, y, kind = scene.terrace_point(0, 2)
    assert kind == "facade"
    assert y == pytest.approx(-TERRACE_OFFSET_M)
    assert abs(x) <= 1.0                      # candidate spots are a metre apart


def test_terrace_point_avoids_party_walls():
    """
    Terraced row: the venue is mapped right next to the wall it shares with
    its neighbour.  The terrace must go to the street, not into next door.
    """
    own, neighbour = box(0, 0, 10, 20), box(10, 0, 20, 20)
    scene = Scene([own, neighbour], [10.0, 10.0])
    x, y, kind = scene.terrace_point(9, 3)          # 1 m from party wall, 3 m from street
    assert kind == "facade"
    assert y == pytest.approx(-TERRACE_OFFSET_M)
    assert neighbour.distance(Point(x, y)) >= TERRACE_OFFSET_M - 0.1


def test_terrace_prefers_the_street_side():
    """Venue mapped dead centre of a deep building: the street decides the side."""
    building = box(-10, 0, 10, 20)
    north_street = LineString([(-100, 30), (100, 30)])
    south_street = LineString([(-100, -10), (100, -10)])
    _, y, _ = Scene([building], [10.0], streets=[north_street]).terrace_point(0, 10)
    assert y == pytest.approx(20 + TERRACE_OFFSET_M)
    _, y, _ = Scene([building], [10.0], streets=[south_street]).terrace_point(0, 10)
    assert y == pytest.approx(-TERRACE_OFFSET_M)


def test_node_mapped_just_outside_a_wall_is_treated_as_that_building():
    """0.5 m outside the south wall is mapping noise, not a terrace position."""
    scene = Scene([box(-10, 0, 10, 20)], [10.0])
    x, y, kind = scene.terrace_point(0, -0.5)
    assert kind == "facade" and y == pytest.approx(-TERRACE_OFFSET_M)


def test_own_building_shades_its_terrace():
    """South-facing terrace: open to the south, own facade blocks the north."""
    scene = Scene([box(-10, 0, 10, 20)], [12.0])
    x, y, _ = scene.terrace_point(0, 2)
    hz = scene.horizon(x, y)
    assert hz[180] == 0
    assert hz[0] == pytest.approx(deg(12, TERRACE_OFFSET_M))


def test_fully_enclosed_venue_falls_back_to_indoor():
    centre = box(0, 0, 10, 10)
    around = [box(-10, -10, 20, 0), box(-10, 10, 20, 20), box(-10, 0, 0, 10), box(10, 0, 20, 10)]
    scene = Scene([centre] + around, [10.0] * 5)
    assert scene.terrace_point(5, 5) == (5, 5, "indoor")


# ── projection ────────────────────────────────────────────────────────────

def test_projection_scale_and_round_trip():
    proj = LocalProjection(15.982, 45.815)
    x, y = proj.to_xy(15.982 + 0.01, 45.815 + 0.01)
    assert float(x) == pytest.approx(777.0, abs=1.5)    # 0.01° lon at 45.8°N
    assert float(y) == pytest.approx(1111.5, abs=1.5)   # 0.01° lat
    lon, lat = proj.to_lonlat(x, y)
    assert float(lon) == pytest.approx(15.992) and float(lat) == pytest.approx(45.825)


def test_multipolygon_parts_all_cast_shade():
    from shapely.geometry import MultiPolygon, box
    scene = Scene([MultiPolygon([box(-5, 10, 5, 15), box(10, -5, 15, 5)])], [10.0])
    h = scene.horizon(0.0, 0.0)
    assert h[0] == pytest.approx(45.0, abs=0.5)     # part to the north
    assert h[90] == pytest.approx(45.0, abs=0.5)    # part to the east
    assert h[180] == 0.0
