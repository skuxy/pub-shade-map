# Zagreb Pub Shade Map

An interactive map showing which Zagreb pubs, bars and café terraces are in the
sun or in the shade — right now, or at any time of any day.

**Live:** https://skuxy.github.io/pub-shade-map/

- Pale yellow markers would be in the sun, but the forecast for that hour is overcast.
- Yellow markers are in direct sun, blue-grey ones in the shade of a building, green ones
  behind a tree crown, dark ones mean the sun is down.
- Drag the time slider or pick a date to see another moment; **Now** follows the clock.
- Click a venue for its sun/shade timeline for the day, hours of direct sun and the forecast.

## How it works

The site is fully static: no backend, no API keys.

1. A build script downloads venues, streets and building footprints from
   OpenStreetMap, and surveyed roof polygons with absolute elevations from the
   City of Zagreb's ZG3D model.
2. For each venue it picks a **terrace point** (3 m outside the venue's
   building, on the street side where it can tell) and computes a **horizon
   profile**: for each degree of compass bearing, the elevation angle up to
   which surrounding roofs hide the sky.
3. The result is one file, `frontend/data/pubs.json`. The browser computes the
   sun's position (SunCalc) and a venue is in shade when the sun is lower than
   its horizon in the sun's direction.

The profile depends only on the geometry, so any date and time is a table
lookup, and the time slider recolours every marker instantly.

```
backend/
  data_pipeline/sources.py    downloads (Overpass, ZG3D ArcGIS FeatureServer)
  data_pipeline/obstacles.py  ZG3D roofs + OSM gap fill → one Scene
  shadow/horizon.py           terrace point and horizon profile (the maths)
  shadow/trees.py             elevation intervals hidden by tree crowns
  shadow/solar.py             pysolar reference, used for the build summary
  scripts/build_data.py       writes frontend/data/pubs.json
  tests/                      hand-computable synthetic scenes
frontend/
  index.html, css/, js/       Leaflet map, time slider, panel, weather
  data/pubs.json              generated; committed so Pages can serve it
```

## Run it locally

```bash
python3 -m http.server 8000 --directory frontend
```

Then open http://localhost:8000.

## Rebuild the data

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd backend
../.venv/bin/python -m pytest tests -q
../.venv/bin/python scripts/build_data.py --refresh
```

`--refresh` re-downloads everything into `data/` (about 250 MB, a few minutes;
Overpass is sometimes at capacity, and the script retries across mirrors).
Without it, cached downloads are reused. Commit the regenerated
`frontend/data/pubs.json`; pushing to `main` redeploys GitHub Pages.

## What the model does not know

- **Which side the terrace is on.** OSM maps a venue as a point, usually inside
  its building. The terrace point is a guess (street side, nearest to the
  mapped point) and for a building in a dense block the answer can differ a lot
  between sides. The map shows the point it used when you select a venue.
- **Private trees, awnings, parasols.** Trees come from the city's register of
  the trees it maintains (streets, parks; surveyed mostly in 2016, sizes in
  classes such as "6 to 10 m"). Courtyard and garden trees are missing, the
  listed ones have grown since, and a crown is drawn as a plain cylinder.
  Deciduous trees count from mid-April to the end of October.
- **Roof shape.** Each ZG3D roof polygon is treated as flat at its highest
  point, which slightly overstates shade next to pitched roofs.
- **Recent construction.** Much of ZG3D dates from a 2008 survey.
- **Clouds.** The forecast panel and the "overcast" hint come from Open-Meteo;
  the sun/shade status itself is geometric.

## Data and licences

- Venues, streets, fallback footprints: © OpenStreetMap contributors, ODbL.
- Roof polygons and elevations, tree register: Grad Zagreb, "Otvorena dozvola".
- Weather: Open-Meteo. Map tiles: OpenStreetMap.
- Code: MIT (see `LICENSE`).
