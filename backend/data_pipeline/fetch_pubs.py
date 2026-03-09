"""
Fetch pub/bar/cafe locations from OpenStreetMap via the Overpass API
and cache them as a GeoJSON FeatureCollection.

Covered amenity types:
  - pub, bar, biergarten
  - cafe with outdoor_seating=yes
"""

import httpx
from pathlib import Path
from .cache import DATA_DIR, save_geojson

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Zagreb bounding box (south, west, north, east)
BBOX = (45.72, 15.87, 45.87, 16.12)

PUBS_CACHE = DATA_DIR / "pubs.geojson"

OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node["amenity"~"^(pub|bar|biergarten)$"]({s},{w},{n},{e});
  way["amenity"~"^(pub|bar|biergarten)$"]({s},{w},{n},{e});
  node["amenity"="cafe"]["outdoor_seating"="yes"]({s},{w},{n},{e});
  way["amenity"="cafe"]["outdoor_seating"="yes"]({s},{w},{n},{e});
);
out center tags;
""".strip()


def _build_query() -> str:
    s, w, n, e = BBOX
    return OVERPASS_QUERY.format(s=s, w=w, n=n, e=e)


def _element_to_feature(el: dict) -> dict | None:
    """Convert an Overpass element to a GeoJSON Feature, or None if unusable."""
    tags = el.get("tags", {})
    name = tags.get("name") or tags.get("name:en") or tags.get("name:hr") or "Unnamed"

    if el["type"] == "node":
        lon, lat = el["lon"], el["lat"]
    elif el["type"] == "way" and "center" in el:
        lon, lat = el["center"]["lon"], el["center"]["lat"]
    else:
        return None

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": f"{el['type']}/{el['id']}",
            "name": name,
            "amenity": tags.get("amenity", ""),
            "outdoor_seating": tags.get("outdoor_seating", ""),
            "address": tags.get("addr:street", ""),
            "website": tags.get("website") or tags.get("contact:website", ""),
            "opening_hours": tags.get("opening_hours", ""),
        },
    }


async def fetch_pubs(session: httpx.AsyncClient) -> list[dict]:
    """
    Query Overpass for pubs/bars in Zagreb and return a list of feature dicts.
    Results are cached to data/pubs.geojson.
    """
    print("Querying Overpass API for pubs/bars …")
    response = await session.post(
        OVERPASS_URL,
        data={"data": _build_query()},
        timeout=90.0,
    )
    response.raise_for_status()
    elements = response.json().get("elements", [])

    features = [f for el in elements if (f := _element_to_feature(el)) is not None]

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "source": "OpenStreetMap via Overpass API",
            "bbox": {"south": BBOX[0], "west": BBOX[1], "north": BBOX[2], "east": BBOX[3]},
            "count": len(features),
        },
    }

    await save_geojson(PUBS_CACHE, geojson)
    print(f"  Saved {len(features)} pubs to {PUBS_CACHE}")
    return features
