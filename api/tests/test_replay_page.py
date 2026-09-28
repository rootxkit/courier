"""The replay page is served by the API, with the same map assets. P10-03.

No database: serving files must not depend on one, and the routes that do
read the databases are covered in `test_replay_pg.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.assets import STATIC
from api.registry import FleetRegistry
from api.replay import ReplayStore

PAGE = STATIC / "replay.html"


def app_with(basemap_dir: Path | None, *, replay: bool = True) -> FastAPI:
    # Neither is touched by a request for a file.
    unused = cast(Any, None)
    store = ReplayStore(
        telemetry=unused,
        relational=None,
        gap_threshold_s=3.0,
        evidence_slack_s=5.0,
        flight_split_s=120.0,
        max_samples=10,
    )
    return create_api_app(
        cast(FleetRegistry, unused),
        replay=store if replay else None,
        basemap_dir=basemap_dir,
    )


async def get(app: FastAPI, path: str) -> tuple[int, bytes]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)
    return response.status_code, response.content


async def test_the_api_serves_the_replay_page_and_its_libraries() -> None:
    page_status, page = await get(app_with(None), "/replay")
    lib_status, _ = await get(app_with(None), "/static/vendor/maplibre-gl-4.7.1.js")

    assert page_status == 200
    assert b"flight replay" in page
    assert lib_status == 200


async def test_without_a_replay_store_there_is_no_replay_page() -> None:
    status, _ = await get(app_with(None, replay=False), "/replay")

    assert status == 404


async def test_the_replay_page_draws_the_installed_base_map(tmp_path: Path) -> None:
    (tmp_path / "basemap.pmtiles").write_bytes(b"PMTiles")

    status, body = await get(app_with(tmp_path), "/basemap/basemap.pmtiles")

    assert (status, body) == (200, b"PMTiles")


async def test_a_missing_base_map_is_a_404_not_a_failure(tmp_path: Path) -> None:
    status, _ = await get(app_with(tmp_path / "absent"), "/basemap/basemap.pmtiles")
    page_status, _ = await get(app_with(tmp_path / "absent"), "/replay")

    assert status == 404
    assert page_status == 200


def test_the_page_loads_nothing_from_the_internet() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assets = [
        ref
        for ref in re.findall(r'(?:src|href)="([^"]+)"', page)
        if ref.endswith((".js", ".css"))
    ]

    assert assets
    assert all(ref.startswith("/static/vendor/") for ref in assets), assets
    for ref in assets:
        assert (STATIC / ref.removeprefix("/static/")).is_file(), ref


def test_every_string_has_both_languages() -> None:
    """CLAUDE.md: en and ka from day one. A key missing from `ka` silently
    falls back to English, which is how a half-translated page ships."""
    page = PAGE.read_text(encoding="utf-8")
    en = re.search(r"\n        en: \{(.*?)\n        \},", page, re.S)
    ka = re.search(r"\n        ka: \{(.*?)\n        \},", page, re.S)
    assert en is not None and ka is not None

    def keys(block: str) -> set[str]:
        return set(re.findall(r"^\s+(\w+):", block, re.M))

    assert keys(en.group(1)) == keys(ka.group(1))
    # And every key the page uses exists.
    used = set(re.findall(r'\bt\("(\w+)"\)', page)) | set(
        re.findall(r'data-i18n="(\w+)"', page)
    )
    assert used <= keys(en.group(1)), used - keys(en.group(1))


def test_the_page_never_joins_two_segments() -> None:
    """The one rule the page exists to keep. Lines are built per segment from
    the API's `segments`, and from nothing else."""
    page = PAGE.read_text(encoding="utf-8")

    assert "replay.segments.map" in page
    assert page.count('type: "LineString"') == 1
