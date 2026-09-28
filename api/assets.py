"""The map's own files, served by whichever app draws a map. P1-12, P10-03.

The live console (`api/telemetry_ws.py`) and the replay page (`api/app.py`)
both draw on the same vendored libraries and the same base map, and both must
work with no internet. One function mounts them, so the two cannot drift:
a base map missing on one and present on the other would be a fault nobody
looks for.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from common import get_logger

_log = get_logger(__name__)

STATIC = Path(__file__).parent / "static"


def mount_map_assets(app: FastAPI, basemap_dir: Path | None) -> None:
    """`/static` always; `/basemap` if installed, else a plain 404.

    The base map is per machine and may be absent. That is a 404 the page
    handles by saying so - never an app that will not start, and never a 500:
    Starlette's StaticFiles raises on every request to a directory that does
    not exist. Checked at startup, so a base map fetched later needs a
    restart.
    """
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    if basemap_dir is not None and basemap_dir.is_dir():
        app.mount("/basemap", StaticFiles(directory=basemap_dir), name="basemap")
        return
    _log.warning(
        "no base map installed; the map will draw without one",
        extra={"basemap_dir": str(basemap_dir)},
    )

    @app.get("/basemap/{path:path}", include_in_schema=False)
    async def no_basemap(path: str) -> Response:
        return Response(status_code=404)
