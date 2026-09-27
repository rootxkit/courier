"""relay-v1 ingest: epoch watermarks, gaps, archive index, ingest events

Revision ID: 0001_relay_ingest
Revises:
Create Date: 2026-09-23

The Gateway's durable state for P1-02. Four tables, each answering one
question, and deliberately none of them holding a row per datagram - the
datagrams live in the file archive, and this is the index over it.

`relay_epochs`       what has been acknowledged, per (station, epoch)
`relay_epoch_gaps`   the holes that are permanent, so they count as satisfied
`archive_segments`   which file covers which hour and which sequence range
`ingest_events`      append-only: losses, link-state changes, protocol faults

Dedupe is bounded by *epoch*, not by time. A time window would be wrong: a
relay that was offline for two hours and replays its backlog is the design
working as intended, and a window would reject it. Per epoch the state is
`highest_contiguous_seq` plus a short list of gaps, which is constant-size
however old the replay is, and is exactly what `resume_from_seq` needs anyway.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_relay_ingest"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relay_epochs",
        sa.Column("station_id", sa.Text(), primary_key=True),
        # relay-v1 §4: 32 lowercase hex characters, generated when the relay's
        # queue database is created. Half of the dedupe key.
        sa.Column("epoch", sa.Text(), primary_key=True),
        # The cumulative watermark. Every sequence number up to and including
        # this one is either stored in the archive or covered by a row in
        # relay_epoch_gaps. `resume_from_seq` is this plus one.
        #
        # -1, not 0, for an epoch that has stored nothing: 0 would claim record
        # zero had arrived.
        sa.Column(
            "highest_contiguous_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("-1"),
        ),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        # Set when the station declares a different epoch. A closed epoch is
        # retained for a configurable period and then dropped; see the note on
        # the failure mode in gateway/ingest_store_pg.py.
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "epoch ~ '^[0-9a-f]{32}$'", name="relay_epochs_epoch_is_128_bit_hex"
        ),
        sa.CheckConstraint(
            "highest_contiguous_seq >= -1",
            name="relay_epochs_watermark_not_below_empty",
        ),
    )

    # Finding the epochs still open for a station, to close them when a new one
    # is declared. Partial, because closed epochs are the vast majority over
    # time and are never scanned by this query.
    op.create_index(
        "relay_epochs_open_by_station",
        "relay_epochs",
        ["station_id"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    # Retention sweeps ask for closed epochs older than a cutoff.
    op.create_index(
        "relay_epochs_closed_at",
        "relay_epochs",
        ["closed_at"],
        postgresql_where=sa.text("closed_at IS NOT NULL"),
    )

    op.create_table(
        "relay_epoch_gaps",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("station_id", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Text(), nullable=False),
        # relay-v1 §11: from_seq inclusive, to_seq EXCLUSIVE. The records that
        # are gone are [from_seq, to_seq). Reading to_seq as inclusive claims
        # one more record was lost than actually was.
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        # An open string by protocol §11, so later reasons need no version bump.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id", "epoch"],
            ["relay_epochs.station_id", "relay_epochs.epoch"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("to_seq > from_seq", name="relay_epoch_gaps_non_empty"),
        sa.CheckConstraint("from_seq >= 0", name="relay_epoch_gaps_from_non_negative"),
        # The same gap re-reported on a later reconnect must not become a
        # second row. Protocol §11 says a recorded gap advances the resume
        # point precisely so that does not happen, but the constraint is here
        # because the arithmetic that prevents it is the thing most likely to
        # be got wrong.
        sa.UniqueConstraint(
            "station_id", "epoch", "from_seq", "to_seq", name="relay_epoch_gaps_unique"
        ),
    )
    op.create_index(
        "relay_epoch_gaps_by_epoch",
        "relay_epoch_gaps",
        ["station_id", "epoch", "from_seq"],
    )

    op.create_table(
        "archive_segments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("station_id", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Text(), nullable=False),
        # Path relative to the archive root, so the root can move without
        # rewriting the index.
        sa.Column("relative_path", sa.Text(), nullable=False),
        # The hour this segment covers, from the record's own recv_utc_ns.
        sa.Column("hour_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        # Kept alongside hour_start because recv_utc_ns comes from the
        # station's clock, which relay-v1 §9 says may be wrong. A station whose
        # clock is off shows up as these disagreeing with stored_at rather than
        # as an hour silently filed in the wrong place.
        sa.Column("first_recv_utc_ns", sa.BigInteger(), nullable=False),
        sa.Column("last_recv_utc_ns", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("compressed_bytes", sa.BigInteger(), nullable=False),
        # What a correct read decompresses to. A truncated segment is detected
        # by comparing against this: zstd returns a short read silently rather
        # than raising.
        sa.Column("uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("last_seq >= first_seq", name="archive_segments_seq_order"),
        sa.CheckConstraint("record_count > 0", name="archive_segments_non_empty"),
        # One row per append, so a segment appears once per batch written into
        # it; the unique key is the path plus the range it added.
        sa.UniqueConstraint(
            "relative_path", "first_seq", "last_seq", name="archive_segments_unique"
        ),
    )
    # The query the archive exists for: "what covers this time range".
    op.create_index(
        "archive_segments_by_hour",
        "archive_segments",
        ["station_id", "hour_start"],
    )
    op.create_index(
        "archive_segments_by_seq",
        "archive_segments",
        ["station_id", "epoch", "first_seq"],
    )

    op.create_table(
        "ingest_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("station_id", sa.Text(), nullable=False),
        # Null for events that are about the station rather than one epoch,
        # such as a link-state change observed before any hello.
        sa.Column("epoch", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ingest_events_by_station_ts",
        "ingest_events",
        ["station_id", sa.text("ts DESC")],
    )
    op.create_index("ingest_events_by_type", "ingest_events", ["event_type", "ts"])


def downgrade() -> None:
    op.drop_table("ingest_events")
    op.drop_table("archive_segments")
    op.drop_table("relay_epoch_gaps")
    op.drop_table("relay_epochs")
