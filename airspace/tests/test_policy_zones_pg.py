"""Policy and zones as the service loads them, from the real database."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.policy import PolicyMissingError, load_policy
from airspace.zones import ZoneType, load_zones

pytestmark = pytest.mark.postgres

SQUARE = "POLYGON((44.80 41.70, 44.82 41.70, 44.82 41.72, 44.80 41.72, 44.80 41.70))"


@pytest.fixture
async def zones(relational_engine: AsyncEngine) -> AsyncIterator[None]:
    async with relational_engine.begin() as connection:
        for name, kind in (("pg-no-fly", "no_fly"), ("pg-corridor", "corridor")):
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_zones "
                    "(name, type, geom, min_alt_amsl_m, max_alt_amsl_m) "
                    "VALUES (:n, :t, ST_GeomFromText(:w, 4326), 400, 700)"
                ),
                {"n": name, "t": kind, "w": SQUARE},
            )
    yield
    async with relational_engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM airspace_zones WHERE name LIKE 'pg-%'")
        )


async def test_the_seeded_policy_is_the_stage_0_policy(
    relational_engine: AsyncEngine,
) -> None:
    policy = await load_policy(relational_engine)
    assert (
        policy.t_cpa_max_s,
        policy.d_horizontal_min_m,
        policy.d_vertical_min_m,
        policy.neighbour_radius_m,
    ) == (60, 60, 20, 800)


async def test_a_second_policy_row_is_refused(relational_engine: AsyncEngine) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_policy (id, t_cpa_max_s, d_horizontal_min_m, "
                    "d_vertical_min_m, neighbour_radius_m) VALUES (2, 30, 60, 20, 800)"
                )
            )


async def test_a_missing_policy_is_refused_rather_than_guessed(
    relational_engine: AsyncEngine,
) -> None:
    """The service must not start on invented thresholds. The row is put back
    afterwards: the test database is shared by the session."""
    async with relational_engine.begin() as connection:
        saved = (
            await connection.execute(sa.text("SELECT * FROM airspace_policy"))
        ).one()
        await connection.execute(sa.text("DELETE FROM airspace_policy"))
    try:
        with pytest.raises(PolicyMissingError):
            await load_policy(relational_engine)
    finally:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_policy (id, t_cpa_max_s, d_horizontal_min_m, "
                    "d_vertical_min_m, neighbour_radius_m) "
                    "VALUES (:id, :t, :h, :v, :r)"
                ),
                {
                    "id": saved.id,
                    "t": saved.t_cpa_max_s,
                    "h": saved.d_horizontal_min_m,
                    "v": saved.d_vertical_min_m,
                    "r": saved.neighbour_radius_m,
                },
            )


async def test_zones_load_with_their_band_and_only_enforceable_types(
    relational_engine: AsyncEngine, zones: None
) -> None:
    loaded = [
        z for z in await load_zones(relational_engine) if z.name.startswith("pg-")
    ]

    assert [z.name for z in loaded] == ["pg-no-fly"]
    zone = loaded[0]
    assert zone.type is ZoneType.NO_FLY
    assert (zone.min_alt_amsl_m, zone.max_alt_amsl_m) == (400, 700)
    assert zone.contains(41.71, 44.81, 500)
    assert not zone.contains(41.71, 44.81, 800)
