"""
Horizon profiles: exact sun/shade for a point among extruded buildings.

Model
─────
Every building is a vertical prism: its 2D footprint extruded to ``height``
metres above flat ground.  Stand at a point P and look along compass bearing
``az``.  If that ray enters a footprint at horizontal distance ``d``, the
building hides everything below the elevation angle ``atan(height / d)``.
The largest such angle over all buildings is the *horizon* in that direction.

    P is in shade  ⇔  sun elevation ≤ horizon(sun azimuth)

The profile depends only on the geometry, not on the date, so it is computed
once per pub (one value per degree of azimuth) and any date or time is then a
table lookup.  Unlike building one shadow polygon per building per time step,
it is exact for concave footprints and courtyards, needs no direction or
distance heuristics, and cannot miss a long or tall building.

Coordinates
───────────
Geometry is done in a local metric frame (east, north in metres) centred on
the city, aligned with true north so sun azimuths can be used directly.  At
city scale (±15 km) the equirectangular approximation is good to centimetres
per kilometre.

Limits of the model
───────────────────
Flat roofs at a single height per polygon (ZG3D gives the ridge, so eaves are
slightly over-estimated), no trees or awnings, and the ground itself never
blocks the sun — but roof tops can be given as absolute elevations, so a
building further up a slope does count for more (see ``Scene``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

N_AZIMUTHS = 360              # one horizon value per degree
MIN_ELEVATION_DEG = 1.0       # obstructions lower than this are ignored
MAX_RADIUS_M = 1500.0         # how far away a building is considered
GROUND_RADII_M = (30.0, 100.0, 300.0)  # where to look for the local ground level
NEAR_WALL_M = 1.5             # a venue mapped this close to a wall belongs to it
STREET_REACH_M = 40.0         # streets further than this do not attract the terrace
TERRACE_OFFSET_M = 3.0        # terrace point sits this far outside the facade
# ...and, if possible, as far from any other building (fallbacks for alleys)
TERRACE_CLEARANCES_M = (TERRACE_OFFSET_M - 0.05, 1.5, 0.75)
_EDGE_CHUNK = 4000            # edges per numpy block (bounds memory to ~50 MB)


# ---------------------------------------------------------------------------
# Local metric projection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocalProjection:
    """Equirectangular projection around (lon0, lat0): x = east m, y = north m."""

    lon0: float
    lat0: float

    @property
    def m_per_deg_lat(self) -> float:
        p = math.radians(self.lat0)
        return 111_132.92 - 559.82 * math.cos(2 * p) + 1.175 * math.cos(4 * p)

    @property
    def m_per_deg_lon(self) -> float:
        p = math.radians(self.lat0)
        return 111_412.84 * math.cos(p) - 93.5 * math.cos(3 * p)

    def to_xy(self, lon, lat):
        return ((np.asarray(lon) - self.lon0) * self.m_per_deg_lon,
                (np.asarray(lat) - self.lat0) * self.m_per_deg_lat)

    def to_lonlat(self, x, y):
        return (np.asarray(x) / self.m_per_deg_lon + self.lon0,
                np.asarray(y) / self.m_per_deg_lat + self.lat0)

    def project(self, geom):
        """Return a copy of a lon/lat shapely geometry in local metres."""
        def _f(coords):
            x, y = self.to_xy(coords[:, 0], coords[:, 1])
            return np.column_stack([x, y])
        return shapely.transform(geom, _f)


# ---------------------------------------------------------------------------
# Scene: all buildings, indexed
# ---------------------------------------------------------------------------
class Scene:
    """
    Buildings in local metres with a spatial index and a flat edge table.

    Args:
        polygons: shapely (Multi)Polygons in **local metres**; holes are honoured.
        heights:  roof height above the building's own base, in metres.
        bases:    optional elevation of each building's base above sea level
                  (NaN where unknown).  Where both the base and the observer's
                  ground level are known, the roof is taken at its absolute
                  elevation ``base + height``; otherwise the ground is assumed
                  level between observer and building.
        streets:  optional shapely LineStrings in local metres; used only to
                  prefer the street side when placing a terrace point.
    """

    def __init__(self, polygons, heights, bases=None, streets=None):
        polys = np.asarray(polygons, dtype=object)
        heights = np.asarray(heights, dtype=float)
        bases = (np.full(len(heights), np.nan) if bases is None
                 else np.asarray(bases, dtype=float))
        if not (len(polys) == len(heights) == len(bases)):
            raise ValueError("polygons, heights and bases differ in length")
        # One entry per simple Polygon: a MultiPolygon becomes its parts.
        if len(polys):
            polys, part_of = shapely.get_parts(polys, return_index=True)
            heights, bases = heights[part_of], bases[part_of]
        self.polys, self.heights, self.bases = polys, heights, bases
        self.tree = STRtree(self.polys)
        self.streets = (shapely.union_all(np.asarray(streets, dtype=object))
                        if streets is not None and len(streets) else None)

        # Flat table of every ring segment (exteriors and courtyards) with the
        # index of the building it belongs to, sorted by building.
        if len(self.polys):
            rings, ring_owner = shapely.get_rings(self.polys, return_index=True)
            coords, coord_ring = shapely.get_coordinates(rings, return_index=True)
            same_ring = coord_ring[:-1] == coord_ring[1:]
            self._edge_a = coords[:-1][same_ring]
            self._edge_b = coords[1:][same_ring]
            self._edge_owner = ring_owner[coord_ring[:-1][same_ring]]
        else:
            self._edge_a = self._edge_b = np.zeros((0, 2))
            self._edge_owner = np.zeros(0, dtype=int)
        # owner is non-decreasing, so each building's edges are one slice.
        self._edge_start = np.searchsorted(self._edge_owner, np.arange(len(self.polys)), "left")
        self._edge_end = np.searchsorted(self._edge_owner, np.arange(len(self.polys)), "right")

    # -- queries ------------------------------------------------------------
    def containing(self, x: float, y: float) -> np.ndarray:
        """Indices of buildings whose footprint contains (x, y)."""
        return self.tree.query(Point(x, y), predicate="within")

    def ground_at(self, x: float, y: float) -> float | None:
        """Ground elevation near (x, y): median base of the surrounding buildings."""
        pt = Point(x, y)
        for radius in GROUND_RADII_M:
            idx = self.tree.query(pt, predicate="dwithin", distance=radius)
            known = self.bases[idx][~np.isnan(self.bases[idx])]
            if len(known):
                return float(np.median(known))
        return None

    def _edges_of(self, building_idx: np.ndarray, eff_height: np.ndarray):
        sel = np.concatenate([
            np.arange(self._edge_start[i], self._edge_end[i]) for i in building_idx
        ]) if len(building_idx) else np.zeros(0, dtype=int)
        h = np.repeat(eff_height, self._edge_end[building_idx] - self._edge_start[building_idx])
        return self._edge_a[sel], self._edge_b[sel], h

    # -- terrace point ------------------------------------------------------
    def terrace_point(self, x: float, y: float) -> tuple[float, float, str]:
        """
        Where to evaluate sun/shade for a venue mapped at (x, y).

        OSM maps most venues as a node *inside* their building.  Guests sit
        outside, so the evaluation point is moved ``TERRACE_OFFSET_M`` outside
        that building's walls, to a spot that keeps clear of neighbouring
        buildings.  Among such spots the one minimising

            distance from the mapped node + distance to the nearest street

        is taken: the street side, unless the node was clearly mapped towards
        another side.  This is a guess — which side a terrace is on is not in
        the data — so the chosen point is published for the map to show.

        Returns ``(x, y, kind)`` where kind is
          "open"    – the mapped point is already outdoors; used as is,
          "facade"  – moved outside the venue's own building,
          "indoor"  – no free spot found; the mapped point is kept and the
                      enclosing building is ignored when casting shade.
        """
        node = Point(x, y)
        own_idx = self.containing(x, y)
        if len(own_idx) == 0:
            # A node a step outside a wall is mapping noise, not a terrace.
            near = self.tree.query_nearest(node, max_distance=NEAR_WALL_M)
            if len(near) == 0:
                return x, y, "open"
            own_idx = near[:1]

        own = shapely.union_all(self.polys[own_idx])
        ring = own.buffer(TERRACE_OFFSET_M, quad_segs=4).boundary

        # Keep the parts of that ring that are as far from every neighbour as
        # they are from the venue's own wall (open street or yard).  Where the
        # surroundings are too tight for that, accept less clearance.
        for clearance in TERRACE_CLEARANCES_M:
            near = np.setdiff1d(
                self.tree.query(ring.buffer(clearance), predicate="intersects"), own_idx,
            )
            free = ring
            if len(near):
                free = ring.difference(
                    shapely.union_all(shapely.buffer(self.polys[near], clearance))
                )
            if not free.is_empty:
                break
        else:
            return (x, y, "indoor") if len(self.containing(x, y)) else (x, y, "open")

        # Candidate spots every metre along the free stretches.
        lines = [g for g in getattr(free, "geoms", [free]) if g.length > 0]
        if not lines:
            spot = nearest_points(free, node)[0]
            return float(spot.x), float(spot.y), "facade"
        cands = np.concatenate([
            shapely.line_interpolate_point(g, np.linspace(0, g.length, max(2, int(g.length) + 1)))
            for g in lines
        ])
        cost = shapely.distance(cands, node)
        if self.streets is not None:
            cost = cost + np.minimum(shapely.distance(cands, self.streets), STREET_REACH_M)
        spot = cands[int(np.argmin(cost))]
        return float(spot.x), float(spot.y), "facade"

    # -- horizon ------------------------------------------------------------
    def horizon(
        self,
        x: float,
        y: float,
        ground: float | None = None,
        n_azimuths: int = N_AZIMUTHS,
        min_elevation_deg: float = MIN_ELEVATION_DEG,
        max_radius_m: float = MAX_RADIUS_M,
    ) -> np.ndarray:
        """
        Obstruction angle in degrees for each of ``n_azimuths`` equally spaced
        bearings (index k ↔ azimuth k·360/n, clockwise from true north) as
        seen from the ground at (x, y).

        ``ground`` is the observer's elevation above sea level (see
        ``ground_at``); None means "assume level ground everywhere".

        Buildings that contain the point are ignored (a point indoors has no
        meaningful sky; see ``terrace_point``).
        """
        horizon = np.zeros(n_azimuths)
        if not len(self.polys):
            return horizon

        pt = Point(x, y)
        cand = self.tree.query(pt, predicate="dwithin", distance=max_radius_m)
        if len(cand) == 0:
            return horizon
        # Roof height above the observer's feet.
        eff = self.heights[cand]
        if ground is not None:
            absolute = self.bases[cand] + self.heights[cand] - ground
            eff = np.where(np.isnan(absolute), eff, absolute)
        dist = shapely.distance(self.polys[cand], pt)
        tan_min = math.tan(math.radians(min_elevation_deg))
        keep = (eff >= dist * tan_min) & (eff > 0) & ~shapely.contains_properly(self.polys[cand], pt)
        order = np.argsort(cand[keep])
        cand, eff = cand[keep][order], eff[keep][order]

        a, b, h = self._edges_of(cand, eff)
        if len(a) == 0:
            return horizon

        az = np.radians(np.arange(n_azimuths) * (360.0 / n_azimuths))
        ux, uy = np.sin(az)[None, :], np.cos(az)[None, :]       # ray directions

        for start in range(0, len(a), _EDGE_CHUNK):
            sl = slice(start, start + _EDGE_CHUNK)
            wx = (a[sl, 0] - x)[:, None]
            wy = (a[sl, 1] - y)[:, None]
            ex = (b[sl, 0] - a[sl, 0])[:, None]
            ey = (b[sl, 1] - a[sl, 1])[:, None]
            # Solve P + t·u = A + s·e  for ray parameter t and edge parameter s.
            denom = ux * ey - uy * ex
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (wx * ey - wy * ex) / denom
                s = (wx * uy - wy * ux) / denom
                hit = (denom != 0) & (s >= 0.0) & (s <= 1.0) & (t > 1e-9)
                angle = np.where(hit, np.degrees(np.arctan2(h[sl][:, None], t)), 0.0)
            np.maximum(horizon, angle.max(axis=0), out=horizon)

        horizon[horizon < min_elevation_deg] = 0.0
        return horizon


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def horizon_at(horizon, azimuth_deg: float) -> float:
    """Horizon angle at an arbitrary azimuth (nearest sample, wraps at 360°)."""
    n = len(horizon)
    return float(horizon[int(math.floor((azimuth_deg % 360.0) * n / 360.0 + 0.5)) % n])


def is_shaded(horizon, sun_azimuth_deg: float, sun_elevation_deg: float) -> bool:
    """
    True when the sun is hidden behind the horizon profile, or is down.

    The frontend applies the same rule (frontend/js/app.js, ``statusAt``).
    """
    if sun_elevation_deg <= 0:
        return True
    return sun_elevation_deg < horizon_at(horizon, sun_azimuth_deg)
