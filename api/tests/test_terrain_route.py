"""GET /terrain: surface elevation at a point, or an honest refusal. P5-00."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.registry import FleetRegistry
from api.tests.auth_fakes import VIEWER_HEADERS, api_kwargs
from common.terrain import Terrain
from common.tests.test_terrain import STEP, install, tile_bytes


def build(terrain: Terrain | None) -> FastAPI:
    return create_api_app(
        cast(FleetRegistry, cast(Any, None)), terrain=terrain, **api_kwargs()
    )


async def get(app: FastAPI, query: str) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=VIEWER_HEADERS
    ) as client:
        return await client.get(f"/terrain?{query}")


async def test_a_known_point_returns_its_elevation_and_dataset(tmp_path: Path) -> None:
    terrain = install(
        tmp_path, {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )

    response = await get(
        build(terrain), f"lat_deg={42.0 - 2 * STEP}&lon_deg={44.0 + 3 * STEP}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["elevation_m"] == 423.0
    assert body["dataset"] == "COP-DEM GLO-30"
    assert body["vertical_datum"] == "EGM2008"


async def test_an_unknown_point_is_404_not_zero(tmp_path: Path) -> None:
    terrain = install(
        tmp_path, {"N41E044": "COP-DEM GLO-30"}, {"N41E044": tile_bytes()}
    )

    response = await get(build(terrain), "lat_deg=10.5&lon_deg=10.5")

    assert response.status_code == 404


async def test_without_terrain_the_api_says_so() -> None:
    response = await get(build(None), "lat_deg=41.5&lon_deg=44.5")

    assert response.status_code == 503


async def test_an_impossible_position_is_refused(tmp_path: Path) -> None:
    terrain = install(tmp_path, {}, {})

    response = await get(build(terrain), "lat_deg=95&lon_deg=44.5")

    assert response.status_code == 422
