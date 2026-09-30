"""`remote_id_observations` against a real TimescaleDB. P1-15.

What only the database can show: the point is stored longitude-first, a
missing position is NULL and not (0, 0), a repeated observation is one row,
two receivers are two, and an AMSL height that does not name its geoid is
refused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_store import RemoteIdRow, RemoteIdWriter, row_from_observation
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, frame, location, pack

pytestmark = pytest.mark.postgres


def a_row() -> RemoteIdRow:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    payload = pack(basic(), location(lat=41.7151, lon=44.8271))
    found = tracker.take(frame(payload), now_s=0.0)
    assert found is not None
    # Its own aircraft, so tests sharing the database do not see each other.
    return replace(
        row_from_observation(found, ts=NOW, payload=payload, geoid_model="EGM2008"),
        aircraft_id=uuid4(),
    )


async def stored(
    engine: AsyncEngine, row: RemoteIdRow
) -> list[sa.Row[tuple[object, ...]]]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    sa.text(
                        "SELECT receiver_id, ST_Y(geom) AS lat, ST_X(geom) AS lon, "
                        "geom IS NULL AS no_point, alt_hae_m, alt_amsl_m, "
                        "geoid_model, payload, ST_Y(operator_geom) AS op_lat "
                        "FROM remote_id_observations WHERE aircraft_id = :id "
                        "ORDER BY receiver_id"
                    ),
                    {"id": row.aircraft_id},
                )
            ).all()
        )


async def test_a_row_is_stored_with_its_point_the_right_way_round(
    engine: AsyncEngine,
) -> None:
    row = a_row()
    await RemoteIdWriter(engine).write([row])

    [back] = await stored(engine, row)
    assert (back.lat, back.lon) == pytest.approx((41.7151, 44.8271))
    assert (back.alt_hae_m, back.alt_amsl_m, back.geoid_model) == (
        520.0,
        500.0,
        "EGM2008",
    )
    assert bytes(back.payload) == row.payload
    assert back.op_lat is None


async def test_no_position_is_null_not_zero(engine: AsyncEngine) -> None:
    row = replace(a_row(), lat_deg=None, lon_deg=None)
    await RemoteIdWriter(engine).write([row])

    [back] = await stored(engine, row)
    assert back.no_point is True


async def test_a_repeat_is_one_row_and_a_second_receiver_is_another(
    engine: AsyncEngine,
) -> None:
    row = a_row()
    writer = RemoteIdWriter(engine)
    await writer.write([row])
    await writer.write([row, replace(row, receiver_id="rx-2")])
    await writer.write([replace(row, ts=NOW + timedelta(seconds=1))])

    back = await stored(engine, row)
    assert [r.receiver_id for r in back] == ["rx-1", "rx-1", "rx-2"]


async def test_an_amsl_height_must_name_its_geoid(engine: AsyncEngine) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        await RemoteIdWriter(engine).write([replace(a_row(), geoid_model=None)])
