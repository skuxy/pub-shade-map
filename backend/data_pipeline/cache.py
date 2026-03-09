"""
Lightweight async helpers for reading and writing GeoJSON cache files.
"""

import json
import aiofiles
from pathlib import Path

import os

# On Render, DATA_DIR env var points to the mounted persistent disk (/data).
# Locally it defaults to the repo-level data/ directory.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))


async def load_geojson(path: Path) -> dict | None:
    """Return parsed GeoJSON from *path*, or None if the file does not exist."""
    if not path.exists():
        return None
    async with aiofiles.open(path, "r", encoding="utf-8") as fh:
        return json.loads(await fh.read())


async def save_geojson(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed GeoJSON to *path*, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as fh:
        await fh.write(json.dumps(data, ensure_ascii=False, indent=2))


async def load_json(path: Path) -> dict | list | None:
    """Return parsed JSON from *path*, or None if the file does not exist."""
    if not path.exists():
        return None
    async with aiofiles.open(path, "r", encoding="utf-8") as fh:
        return json.loads(await fh.read())


async def save_json(path: Path, data: dict | list) -> None:
    """Write *data* as JSON to *path*, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as fh:
        await fh.write(json.dumps(data, ensure_ascii=False, indent=2))
