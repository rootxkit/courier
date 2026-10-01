"""Drop remote_id_observations: Remote ID ingest left courier's scope

Revision ID: 0009_drop_remote_id_observations
Revises: 0008_archive_retention_index
Create Date: 2026-10-01

P1-15, removed. Courier is a delivery platform. Ingesting Remote ID
broadcasts is the national UTM's job; courier will report its own fleet to
the UTM as network Remote ID and read other traffic from the UTM's API. The
ingest that wrote `remote_id_observations` and the replay that read it are
gone, so the table has neither writer nor reader.

0006 and 0007 are not edited (CLAUDE.md): this revision undoes what they
created for Remote ID alone.

## Dropped

`remote_id_observations`, the hypertable 0006 created, with everything that
lives on it: its two check constraints, the `geom` and `operator_geom`
columns, its indexes (0006's three and the hypertable's own on `ts`), and
0007's `matched_drone_id` column. Dropping the table drops its chunks. The
rows are not kept anywhere; the downgrade recreates an empty table.

## Kept: known_drones.serial

0007 also added `known_drones.serial` and its partial unique index
`known_drones_serial_unique`. They stay. The column is the projection of the
relational `drones.serial`, the manufacturer serial that identifies one of
our own airframes, and it is still maintained by the registry: `POST
/drones` (`api/registry.py`) and `tools/register_aircraft.py --serial`
write it through `BindingResolver.register_drone`, and
`api/tests/test_registry_pg.py` checks that it is projected and kept when
an aircraft retires. The Gateway never connects to the relational database,
so this column is the only place the telemetry side can learn an airframe's
serial, which reporting our fleet to the UTM will need. Dropping it would
leave the registry writing a column that does not exist.

## Downgrade

Recreates the table exactly as 0006 and 0007 left it, `matched_drone_id`
included, as an empty hypertable with the same chunk interval. The
observations themselves do not come back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_drop_remote_id_observations"
down_revision: str | None = "0008_archive_retention_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "remote_id_observations"
# As 0006 created it.
CHUNK_INTERVAL = "7 days"


def upgrade() -> None:
    op.drop_table(TABLE)


def downgrade() -> None:
    # 0006_remote_id_observations, then 0007's matched_drone_id.
    op.create_table(
        TABLE,
        sa.Column("aircraft_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receiver_id", sa.Text(), nullable=False),
        sa.Column("transmitter", sa.Text(), nullable=False),
        sa.Column("ua_id", sa.Text(), nullable=False),
        sa.Column("id_type", sa.SmallInteger(), nullable=False),
        sa.Column("ua_type", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=True),
        sa.Column("alt_hae_m", sa.Double(), nullable=True),
        sa.Column("alt_amsl_m", sa.Double(), nullable=True),
        sa.Column("geoid_model", sa.Text(), nullable=True),
        sa.Column("alt_above_takeoff_m", sa.Double(), nullable=True),
        sa.Column("track_deg", sa.Double(), nullable=True),
        sa.Column("vx_ms", sa.Double(), nullable=True),
        sa.Column("vy_ms", sa.Double(), nullable=True),
        sa.Column("vz_ms", sa.Double(), nullable=True),
        sa.Column("groundspeed_ms", sa.Double(), nullable=True),
        sa.Column("climb_ms", sa.Double(), nullable=True),
        sa.Column("operator_id", sa.Text(), nullable=True),
        sa.Column("rssi_dbm", sa.Double(), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "track_deg IS NULL OR (track_deg >= 0 AND track_deg < 360)",
            name="remote_id_observations_track_is_a_bearing",
        ),
        sa.CheckConstraint(
            "alt_amsl_m IS NULL OR geoid_model IS NOT NULL",
            name="remote_id_observations_amsl_names_its_geoid",
        ),
    )
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN geom geometry(Point, 4326)")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN operator_geom geometry(Point, 4326)")
    op.execute(
        f"SELECT create_hypertable('{TABLE}', 'ts', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}')"
    )
    op.create_index(
        "remote_id_observations_by_aircraft_ts",
        TABLE,
        ["aircraft_id", sa.text("ts DESC")],
    )
    op.execute(
        f"CREATE INDEX remote_id_observations_geom_gist ON {TABLE} USING gist (geom)"
    )
    op.create_index(
        "remote_id_observations_unique",
        TABLE,
        ["aircraft_id", "ts", "receiver_id"],
        unique=True,
    )
    op.add_column(
        TABLE, sa.Column("matched_drone_id", UUID(as_uuid=True), nullable=True)
    )
