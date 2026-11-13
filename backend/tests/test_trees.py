"""Tree-shade intervals on hand-computable scenes."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shadow.trees import crown_base, in_tree_shade, tree_intervals  # noqa: E402
from data_pipeline.sources import is_evergreen  # noqa: E402


def deg(h, d):
    return math.degrees(math.atan2(h, d))


def at(intervals, b):
    return [(lo, hi) for i, lo, hi in intervals if i == b]


def test_single_tree_due_north():
    # 10 m tree, 6 m crown, 10 m north: foliage from 3.5 m up, between 7 and 13 m away.
    iv = tree_intervals(0, 0, [0], [10], [6], [10])
    (lo, hi), = at(iv, 0)
    assert lo == pytest.approx(deg(3.5, 13), abs=1e-6)
    assert hi == pytest.approx(deg(10, 7), abs=1e-6)
    assert at(iv, 180) == [] and at(iv, 90) == []
    # crown subtends asin(3/10) = 17.5° either side of north
    assert at(iv, 17) and at(iv, 343) and not at(iv, 18) and not at(iv, 342)


def test_azimuth_is_clockwise_from_north():
    iv = tree_intervals(0, 0, [10], [0], [6], [10])      # tree to the east
    assert at(iv, 90) and not at(iv, 270) and not at(iv, 0)


def test_sun_passes_under_the_crown_and_over_the_top():
    iv = tree_intervals(0, 0, [0], [10], [6], [10])
    assert not in_tree_shade(iv, 0, 10)       # under the foliage (low < 15.1°)
    assert in_tree_shade(iv, 0, 30)
    assert not in_tree_shade(iv, 0, 60)       # above the top (high = 55°)
    assert not in_tree_shade(iv, 180, 30)


def test_sitting_under_the_crown():
    iv = tree_intervals(0, 0, [0], [1], [8], [8])        # trunk 1 m away, crown radius 4
    (lo, hi), = at(iv, 0)
    assert hi == 90.0 and lo == pytest.approx(deg(2.8, 5))
    (lo, hi), = at(iv, 180)                                # crown edge 3 m behind
    assert hi == 90.0 and lo == pytest.approx(deg(2.8, 3))


def test_overlapping_trees_merge_and_separate_ones_do_not():
    # A near small tree and a far tall one in the same direction.
    iv = tree_intervals(0, 0, [0, 0], [6, 100], [4, 8], [4, 13])
    near = (deg(1.4, 8), deg(4, 4))
    far = (deg(4, 104), deg(13, 96))
    assert far[1] < near[0]                               # a gap between them
    assert at(iv, 0) == [pytest.approx(far), pytest.approx(near)]
    # Two crowns that overlap in elevation become one interval.
    iv = tree_intervals(0, 0, [0, 0], [10, 14], [6, 6], [10, 10])
    assert len(at(iv, 0)) == 1


def test_far_trees_are_ignored_and_empty_input_is_fine():
    assert tree_intervals(0, 0, [0], [400], [8], [20]) == []
    assert tree_intervals(0, 0, [], [], [], []) == []


def test_crown_base_is_capped():
    assert crown_base(4) == pytest.approx(1.4)
    assert crown_base(28) == 4.0


def test_evergreen_by_genus():
    assert is_evergreen("PINUS NIGRA") and is_evergreen("Picea  abies")
    assert is_evergreen("MAGNOLIA GRANDIFLORA") and not is_evergreen("MAGNOLIA KOBUS")
    assert not is_evergreen("LARIX DECIDUA") and not is_evergreen("ACER PLATANOIDES")
    assert not is_evergreen("") and not is_evergreen(None)
