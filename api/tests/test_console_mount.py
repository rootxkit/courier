"""The built console is served at /app, and / leads to it. P6-01.

Both sides: with a build, /app serves it and / redirects there; without one,
/app does not exist and / falls back to the minimal map. The console page
itself carries no data, so it loads signed out and asks the API, which
refuses (test_auth_routes.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.registry import FleetRegistry
from api.tests.auth_fakes import api_kwargs

INDEX = "<!doctype html><title>console</title><div id=root></div>"


def build(console_app_dir: Path | None) -> FastAPI:
    return create_api_app(
        cast(FleetRegistry, cast(Any, None)),
        console_app_dir=console_app_dir,
        **api_kwargs(),
    )


async def get(app: FastAPI, path: str) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def test_a_built_console_is_served_and_home_leads_to_it(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    app = build(tmp_path)

    home = await get(app, "/")
    page = await get(app, "/app/")
    asset = await get(app, "/app/assets/app.js")

    assert home.status_code in (302, 307)
    assert home.headers["location"] == "/app/"
    assert page.status_code == 200
    assert page.text == INDEX
    assert asset.status_code == 200


async def test_without_a_build_home_falls_back_to_the_minimal_map(
    tmp_path: Path,
) -> None:
    # An empty directory: configured, but `npm run build` has not run.
    app = build(tmp_path)

    home = await get(app, "/")
    page = await get(app, "/app/")

    assert home.headers["location"] == "/map"
    assert page.status_code == 404
