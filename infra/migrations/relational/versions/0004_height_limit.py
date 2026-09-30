"""airspace_policy.max_height_agl_m: the height limit above the ground

Revision ID: 0004_height_limit
Revises: 0003_operators
Create Date: 2026-09-30

P5-19. The airspace monitor warns when an aircraft is higher above the ground
than this. Height above ground is computed from the aircraft's AMSL altitude
and the DEM (P5-00), not taken from telemetry.

Seeded with 120 m, the limit the owner set on 2026-09-30. NULL means no limit
is evaluated; it never means "unlimited by rule". There is deliberately no
minimum height: the owner has none.

Like the other thresholds, this is policy and not code. An operator changes it
by updating the row, not with a deploy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_height_limit"
down_revision: str | None = "0003_operators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "airspace_policy", sa.Column("max_height_agl_m", sa.Float(), nullable=True)
    )
    op.create_check_constraint(
        "airspace_policy_height_limit_positive",
        "airspace_policy",
        "max_height_agl_m IS NULL OR max_height_agl_m > 0",
    )
    op.execute("UPDATE airspace_policy SET max_height_agl_m = 120 WHERE id = 1")


def downgrade() -> None:
    op.drop_constraint(
        "airspace_policy_height_limit_positive", "airspace_policy", type_="check"
    )
    op.drop_column("airspace_policy", "max_height_agl_m")
