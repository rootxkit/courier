"""archive retention: investigation holds with an expiry

Revision ID: 0002_archive_retention
Revises: 0001_relay_ingest
Create Date: 2026-09-23

A hold exempts an epoch from both retention rules — the age limit and the
per-station size ceiling — so an investigation can outlive the policy without
anyone editing the policy. Editing the retention period to protect one flight
is how a fleet accidentally keeps everything for a year.

**Every hold expires.** An indefinite hold becomes permanent by neglect, which
is the failure the flag exists to prevent: the archive stops being bounded and
nobody notices until the disk does. So a hold records who set it, when, why,
and the date it lapses.

An expired hold deletes nothing by itself. It returns the epoch to the normal
rules, and the normal rules then apply as they would have anyway. Expiry that
triggered deletion would make a forgotten date destroy evidence, which is worse
than keeping it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_archive_retention"
down_revision: str | None = "0001_relay_ingest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "relay_epochs",
        # Null means no hold. A hold is a date, not a boolean, because the
        # question asked later is always "until when".
        sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "relay_epochs",
        sa.Column("hold_set_by", sa.Text(), nullable=True),
    )
    op.add_column(
        "relay_epochs",
        sa.Column("hold_set_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "relay_epochs",
        # Free text on purpose. "CAA occurrence 2026-114" is the useful answer
        # and no enumeration would have contained it.
        sa.Column("hold_reason", sa.Text(), nullable=True),
    )

    # A hold is all four fields or none. A hold with no owner and no reason is
    # one nobody can evaluate when the date arrives, which is how it gets
    # renewed forever by whoever is afraid to be the one who deleted it.
    op.create_check_constraint(
        "relay_epochs_hold_is_complete",
        "relay_epochs",
        "(hold_until IS NULL AND hold_set_by IS NULL AND hold_set_at IS NULL "
        "AND hold_reason IS NULL) OR (hold_until IS NOT NULL AND "
        "hold_set_by IS NOT NULL AND hold_set_at IS NOT NULL AND "
        "hold_reason IS NOT NULL)",
    )

    # The sweep asks for held epochs and for holds approaching their date.
    op.create_index(
        "relay_epochs_hold_until",
        "relay_epochs",
        ["hold_until"],
        postgresql_where=sa.text("hold_until IS NOT NULL"),
    )

    # Retention deletes whole segments and records that it did. Marking the row
    # rather than deleting it keeps the index honest about what the archive
    # used to contain: a query for a time range can still say "there were
    # 3600 records here and they were deleted on this date for this reason",
    # which is a different answer from "nothing was ever recorded".
    op.add_column(
        "archive_segments",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "archive_segments",
        sa.Column("deleted_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "archive_segments_live_by_hour",
        "archive_segments",
        ["station_id", "hour_start"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("archive_segments_live_by_hour", table_name="archive_segments")
    op.drop_column("archive_segments", "deleted_reason")
    op.drop_column("archive_segments", "deleted_at")
    op.drop_index("relay_epochs_hold_until", table_name="relay_epochs")
    op.drop_constraint("relay_epochs_hold_is_complete", "relay_epochs")
    op.drop_column("relay_epochs", "hold_reason")
    op.drop_column("relay_epochs", "hold_set_at")
    op.drop_column("relay_epochs", "hold_set_by")
    op.drop_column("relay_epochs", "hold_until")
