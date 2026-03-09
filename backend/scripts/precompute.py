"""
CLI script: pre-compute shade timelines for all pubs and save to disk.

Pre-computed timelines are stored as:
    data/shade/{pub_id_safe}/{date}.json

This avoids on-demand computation cost when serving many concurrent users.
The API automatically uses pre-computed data when available (future enhancement).

Usage:
    python scripts/precompute.py                  # today only
    python scripts/precompute.py --days 3         # today + next 2 days
    python scripts/precompute.py --date 2024-07-01 --days 3
"""

import asyncio
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_pipeline.cache import DATA_DIR, load_geojson
from shadow.shade_timeline import compute_shade_timeline


def _pub_id_safe(pub_id: str) -> str:
    """Convert e.g. 'node/123456' to 'node_123456' for filesystem use."""
    return pub_id.replace("/", "_")


def _feature_to_building(feature: dict) -> dict | None:
    geom = feature.get("geometry", {})
    props = feature.get("properties", {})
    if geom.get("type") != "Polygon":
        return None
    rings = geom.get("coordinates", [])
    if not rings:
        return None
    footprint = [(c[0], c[1]) for c in rings[0]]
    height = float(props.get("height", 8.0))
    return {"id": props.get("id", ""), "footprint": footprint, "height": height}


async def main(start_date: date, days: int) -> None:
    pubs_geojson = await load_geojson(DATA_DIR / "pubs.geojson")
    buildings_geojson = await load_geojson(DATA_DIR / "buildings.geojson")

    if not pubs_geojson:
        print("ERROR: data/pubs.geojson not found. Run fetch_data.py first.")
        sys.exit(1)
    if not buildings_geojson:
        print("ERROR: data/buildings.geojson not found. Run fetch_data.py first.")
        sys.exit(1)

    pubs = pubs_geojson["features"]
    buildings = [
        b for f in buildings_geojson["features"]
        if (b := _feature_to_building(f)) is not None
    ]

    dates = [start_date + timedelta(days=i) for i in range(days)]
    total = len(pubs) * len(dates)
    done = 0

    print(f"Pre-computing shade for {len(pubs)} pubs × {len(dates)} days "
          f"({total} timelines) …\n")

    for target_date in dates:
        date_str = target_date.isoformat()
        for pub in pubs:
            pub_id = pub["properties"]["id"]
            safe_id = _pub_id_safe(pub_id)

            out_path = DATA_DIR / "shade" / safe_id / f"{date_str}.json"
            if out_path.exists():
                done += 1
                continue  # Already computed

            timeline = compute_shade_timeline(pub, buildings, target_date, step_minutes=5)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps({
                "pub_id": pub_id,
                "pub_name": pub["properties"].get("name", ""),
                "date": date_str,
                "step_minutes": 5,
                "timeline": timeline,
            }, ensure_ascii=False))

            done += 1
            pct = done / total * 100
            print(f"  [{done}/{total}  {pct:.1f}%]  {pub['properties'].get('name','?')} — {date_str}",
                  end="\r", flush=True)

    print(f"\nDone. {done} timelines saved to {DATA_DIR / 'shade'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pre-compute shade timelines.")
    parser.add_argument("--date", default=None, help="Start date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="Number of days to compute (default: 1)")
    args = parser.parse_args()

    if args.date:
        try:
            start = date.fromisoformat(args.date)
        except ValueError:
            print("ERROR: --date must be YYYY-MM-DD")
            sys.exit(1)
    else:
        start = date.today()

    asyncio.run(main(start, args.days))
