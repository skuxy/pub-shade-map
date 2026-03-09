"""
CLI script: fetch and cache OSM pubs and buildings for Zagreb.

Usage:
    python scripts/fetch_data.py [--pubs-only] [--buildings-only]
"""

import asyncio
import argparse
import sys
from pathlib import Path

# Allow imports from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from data_pipeline.fetch_pubs import fetch_pubs
from data_pipeline.fetch_buildings import fetch_buildings


async def main(fetch_p: bool, fetch_b: bool) -> None:
    async with httpx.AsyncClient() as session:
        if fetch_p:
            pubs = await fetch_pubs(session)
            print(f"  -> {len(pubs)} pubs fetched.")

        if fetch_b:
            buildings = await fetch_buildings(session)
            print(f"  -> {len(buildings)} building footprints fetched.")

    print("\nDone. Run 'uvicorn api.main:app --reload --port 8000' to start the server.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch OSM data for Zagreb pub shade map.")
    parser.add_argument("--pubs-only", action="store_true", help="Only fetch pubs")
    parser.add_argument("--buildings-only", action="store_true", help="Only fetch buildings")
    args = parser.parse_args()

    fetch_p = not args.buildings_only
    fetch_b = not args.pubs_only

    asyncio.run(main(fetch_p, fetch_b))
