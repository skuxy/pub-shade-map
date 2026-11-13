"""
Build ``frontend/data/pubs.json`` — everything the map needs, in one file.

For every venue: where its terrace is assumed to be, and its horizon profile
(how high the surrounding roofs reach, per degree of compass bearing).  The
browser combines that with the sun's position to tell sun from shade at any
moment, so no server is needed.

Usage (from backend/):
    python scripts/build_data.py              # use cached downloads in data/
    python scripts/build_data.py --refresh    # re-download OSM and ZG3D first
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_pipeline import sources                       # noqa: E402
from data_pipeline.obstacles import CENTRE, build_scene, load_trees  # noqa: E402
from shadow.horizon import N_AZIMUTHS, is_shaded         # noqa: E402
from shadow.solar import get_sun_timeline                # noqa: E402
from shadow.trees import TREE_RADIUS_M, tree_intervals   # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "frontend" / "data" / "pubs.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--refresh", action="store_true", help="re-download all inputs")
    args = parser.parse_args()

    sources.ensure(refresh=args.refresh)

    t0 = time.time()
    scene, stats = build_scene()
    print(f"Scene: {len(scene.polys):,} obstacles {stats} ({time.time() - t0:.0f}s)")

    trees = load_trees()
    tree_index = STRtree(shapely.points(trees["x"], trees["y"]))
    stats["trees"] = len(trees["x"])

    def foliage(x: float, y: float, evergreen: bool, horizon) -> list[int]:
        """
        Flat [bin, low, high, …] in tenths of a degree for one kind of tree.
        Foliage below the roof horizon can never matter (the roof already
        shades those angles), so it is cut off; this also keeps the file small.
        """
        # crowns are at most 17 m wide, so this reach catches every crown in range
        near = tree_index.query(Point(x, y).buffer(TREE_RADIUS_M + 10.0), predicate="intersects")
        near = near[trees["evergreen"][near] == evergreen]
        flat: list[int] = []
        for b, lo, hi in tree_intervals(x, y, trees["x"][near], trees["y"][near],
                                        trees["crown"][near], trees["height"][near]):
            lo = max(lo, float(horizon[b]))
            if hi > lo:
                flat += [b, int(round(lo * 10)), int(round(hi * 10))]
        return flat

    t0 = time.time()
    pubs, horizons, kinds, shaded_by_trees = [], [], collections.Counter(), 0
    for f in sources.load("pubs.geojson"):
        lon, lat = f["geometry"]["coordinates"][:2]
        x, y = (float(v) for v in CENTRE.to_xy(lon, lat))
        tx, ty, kind = scene.terrace_point(x, y)
        horizon = scene.horizon(tx, ty, ground=scene.ground_at(tx, ty))
        tlon, tlat = (float(v) for v in CENTRE.to_lonlat(tx, ty))
        kinds[kind] += 1
        deciduous, evergreen = foliage(tx, ty, False, horizon), foliage(tx, ty, True, horizon)
        shaded_by_trees += bool(deciduous or evergreen)
        horizons.append(horizon)
        p = f["properties"]
        pubs.append({
            "id": p["id"],
            "name": p["name"],
            "amenity": p["amenity"],
            "seating": p["outdoor_seating"],
            "address": p["address"],
            "website": p["website"],
            "hours": p["opening_hours"],
            "lat": round(lat, 6), "lon": round(lon, 6),
            "terrace": [round(tlat, 6), round(tlon, 6)],
            "kind": kind,
            # tenths of a degree, index = azimuth clockwise from north
            "horizon": [int(round(v * 10)) for v in horizon],
            # foliage as flat [bin, low, high, …] triples, tenths of a degree:
            # the sun is behind a crown when low ≤ elevation ≤ high in its bin
            "trees": deciduous,          # leafless in winter
            "evergreens": evergreen,
        })
    if not pubs:
        raise SystemExit("No venues in pubs.geojson; rerun with --refresh.")
    stats["venues_with_trees"] = shaded_by_trees
    print(f"Horizons: {len(pubs)} venues {dict(kinds)}, {shaded_by_trees} with trees "
          f"within {TREE_RADIUS_M:.0f} m ({time.time() - t0:.0f}s)")

    # Sanity summary: share of daylight in direct sun on the solstices/equinox.
    for day in (date(2026, 6, 21), date(2026, 9, 22), date(2026, 12, 21)):
        sun = get_sun_timeline(day)
        share = [100 * sum(not is_shaded(h, s["azimuth"], s["elevation"]) for s in sun) / len(sun)
                 for h in horizons]
        q = np.percentile(share, [10, 50, 90]).round()
        print(f"  {day}: sunny share of daylight p10/p50/p90 = {q[0]:.0f}/{q[1]:.0f}/{q[2]:.0f} %")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "azimuths": N_AZIMUTHS,
        "stats": {**stats, "venues": len(pubs), **{f"terrace_{k}": v for k, v in kinds.items()}},
        "attribution": "Venues, streets: © OpenStreetMap contributors (ODbL). "
                       "Building heights and trees: Grad Zagreb, ZG3D and tree register "
                       "(Otvorena dozvola).",
        "pubs": pubs,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    print(f"Wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
