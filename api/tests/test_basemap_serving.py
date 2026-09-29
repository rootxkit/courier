"""The console serves its own map: libraries and base map, no internet. P1-12.

No broker needed. The app starts against a dead NATS port, as in
`test_console_snapshot.py`, because serving files must not depend on the bus.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.assets import STATIC
from api.telemetry_ws import create_app
from tests.ports import free_tcp_port

FEED_SECRET = b"test-feed-secret-0123456789abcdef0123"


def app_with(basemap_dir: Path | None) -> FastAPI:
    return create_app(
        f"nats://127.0.0.1:{free_tcp_port()}",
        feed_secret=FEED_SECRET,
        connect_timeout_s=0.5,
        basemap_dir=basemap_dir,
    )


async def get(
    app: FastAPI, path: str, **headers: str
) -> tuple[int, bytes, dict[str, str]]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get(path, headers=headers)
    return response.status_code, response.content, dict(response.headers)


def test_the_page_loads_nothing_from_the_internet() -> None:
    """Every script and stylesheet is served by the console itself. A CDN
    link here is a map that goes blank in the field."""
    page = (STATIC / "map.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="([^"]+)"', page)
    assets = [ref for ref in references if ref.endswith((".js", ".css"))]

    assert assets, "the page references no scripts at all"
    assert all(ref.startswith("/static/vendor/") for ref in assets), assets


def test_every_vendored_asset_the_page_names_exists() -> None:
    page = (STATIC / "map.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:src|href)="/static/([^"]+)"', page):
        assert (STATIC / ref).is_file(), ref


def test_the_vendored_libraries_carry_their_licences() -> None:
    licences = sorted(path.name for path in (STATIC / "vendor").glob("LICENSE-*"))
    assert licences == [
        "LICENSE-PMTiles.txt",
        "LICENSE-basemaps.txt",
        "LICENSE-maplibre-gl.txt",
    ]


async def test_the_vendored_map_library_is_served() -> None:
    status, body, _ = await get(app_with(None), "/static/vendor/maplibre-gl-4.7.1.js")
    assert status == 200
    assert b"maplibre" in body[:2000].lower()


async def test_the_base_map_is_served_with_range_requests(tmp_path: Path) -> None:
    """PMTiles is read by the browser in byte ranges; a server that ignores
    `Range` would send the whole extract for every tile."""
    (tmp_path / "basemap.pmtiles").write_bytes(b"PMTiles" + bytes(range(256)) * 4)

    status, body, headers = await get(
        app_with(tmp_path), "/basemap/basemap.pmtiles", Range="bytes=0-6"
    )

    assert status == 206
    assert body == b"PMTiles"
    assert headers["content-range"].startswith("bytes 0-6/")


async def test_a_machine_without_a_base_map_still_serves_the_console(
    tmp_path: Path,
) -> None:
    """The base map is fetched per machine. Its absence is a 404 the page
    explains, never a console that will not start."""
    missing = tmp_path / "not-fetched"

    app = app_with(missing)
    status, _, _ = await get(app, "/basemap/basemap.pmtiles")
    page_status, page, _ = await get(app_with(missing), "/")

    assert status == 404
    assert page_status == 200
    assert b"no_basemap" in page
