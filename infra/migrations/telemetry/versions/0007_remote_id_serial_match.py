"""known_drones.serial and remote_id_observations.matched_drone_id

Revision ID: 0007_remote_id_serial_match
Revises: 0006_remote_id_observations
Create Date: 2026-09-30

P1-15: an aircraft seen both ways is one track, matched by serial.

`known_drones.serial` is the projection of the relational `drones.serial`,
kept by the same path that keeps the label (`api/registry.py` through
`BindingResolver.register_drone`). It must be the serial the aircraft
broadcasts: ANSI/CTA-2063-A, as its Remote ID module sends it. Nullable,
because an aircraft registered here before this column, or with no Remote
ID, has none. Unique where set: two aircraft claiming one serial would make
the match a guess.

`remote_id_observations.matched_drone_id` records, on every observation,
which of our aircraft its serial matched at the time, so replay and any
later inquiry can tell a broadcast by our own aircraft from a stranger's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007_remote_id_serial_match"
down_revision: str | None = "0006_remote_id_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("known_drones", sa.Column("serial", sa.Text(), nullable=True))
    op.create_index(
        "known_drones_serial_unique",
        "known_drones",
        ["serial"],
        unique=True,
        postgresql_where=sa.text("serial IS NOT NULL"),
    )
    op.add_column(
        "remote_id_observations",
        sa.Column("matched_drone_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("remote_id_observations", "matched_drone_id")
    op.drop_index("known_drones_serial_unique", table_name="known_drones")
    op.drop_column("known_drones", "serial")
