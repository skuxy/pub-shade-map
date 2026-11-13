"""
Tree shade, kept separate from the building horizon.

A roof hides everything below one angle, so a single number per bearing
describes it.  A tree does not: the sun can shine *under* the crown.  A tree is
therefore modelled as a vertical cylinder of foliage (the crown) on a bare
trunk, and for each compass bearing the result is a list of elevation
*intervals* the foliage covers, as seen from the observer:

    tree shade  ⇔  low ≤ sunElevation ≤ high   for some interval at sunAzimuth

Standing under the crown, the interval reaches straight up (high = 90°).
Tree shade is partial in reality (dappled light, bare branches in winter);
the caller decides how to present it and when deciduous crowns count.
"""

from __future__ import annotations

import numpy as np

TREE_RADIUS_M = 150.0          # trees further away are left to the roofs
CROWN_BASE_SHARE = 0.35        # crown starts at this share of the tree's height …
CROWN_BASE_MAX_M = 4.0         # … but no higher than this (street trees are pruned up)
MIN_INTERVAL_DEG = 1.0         # ignore foliage that never rises above this


def crown_base(height_m):
    """Height of the lowest foliage for a tree of *height_m*."""
    return np.minimum(CROWN_BASE_SHARE * np.asarray(height_m, dtype=float), CROWN_BASE_MAX_M)


def tree_intervals(x, y, tx, ty, crown_width, height, n_azimuths: int = 360,
                   max_radius_m: float = TREE_RADIUS_M) -> list[tuple[int, float, float]]:
    """
    Elevation intervals hidden by tree crowns, per azimuth bin.

    Args:
        x, y:         observer, local metres (ground level, same as the trees).
        tx, ty:       tree positions, local metres.
        crown_width:  crown diameter per tree, metres.
        height:       tree height per tree, metres.

    Returns:
        ``(bin, low_deg, high_deg)`` tuples sorted by bin then low; overlapping
        intervals of one bin are merged.  Bin *i* is the bearing
        ``i * 360 / n_azimuths`` degrees clockwise from north.
    """
    cx = np.asarray(tx, dtype=float) - x
    cy = np.asarray(ty, dtype=float) - y
    r = np.asarray(crown_width, dtype=float) / 2.0
    top = np.asarray(height, dtype=float)
    dist = np.hypot(cx, cy)
    near = (dist - r <= max_radius_m) & (r > 0) & (top > 0)
    if not near.any():
        return []
    cx, cy, r, top, dist = cx[near], cy[near], r[near], top[near], dist[near]
    base = crown_base(top)

    az = np.radians(np.arange(n_azimuths) * 360.0 / n_azimuths)
    ux, uy = np.sin(az)[:, None], np.cos(az)[:, None]        # (n, 1): north = +y
    along = ux * cx + uy * cy                                # (n, trees)
    perp2 = dist ** 2 - along ** 2
    half = np.sqrt(np.clip(r ** 2 - perp2, 0.0, None))
    d_out = along + half                                     # ray leaves the crown
    d_in = np.maximum(along - half, 0.0)                     # ray enters it (0: under it)
    hit = (perp2 < r ** 2) & (d_out > 1e-6)

    with np.errstate(divide="ignore", invalid="ignore"):
        low = np.degrees(np.arctan2(base, d_out))
        high = np.where(d_in <= 1e-6, 90.0, np.degrees(np.arctan2(top, d_in)))

    out: list[tuple[int, float, float]] = []
    for i, j in zip(*np.nonzero(hit & (high >= MIN_INTERVAL_DEG))):
        out.append((int(i), float(low[i, j]), float(high[i, j])))
    out.sort()
    merged: list[tuple[int, float, float]] = []
    for b, lo, hi in out:
        if merged and merged[-1][0] == b and lo <= merged[-1][2]:
            merged[-1] = (b, merged[-1][1], max(merged[-1][2], hi))
        else:
            merged.append((b, lo, hi))
    return merged


def in_tree_shade(intervals, sun_azimuth_deg: float, sun_elevation_deg: float,
                  n_azimuths: int = 360) -> bool:
    """True if the sun is behind foliage (nearest-bin lookup, like the roofs)."""
    b = int(np.floor(sun_azimuth_deg * n_azimuths / 360.0 + 0.5)) % n_azimuths
    return any(i == b and lo <= sun_elevation_deg <= hi for i, lo, hi in intervals)
