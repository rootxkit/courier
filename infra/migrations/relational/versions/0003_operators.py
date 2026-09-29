"""operators and operator_sessions: who may use the API and the console

Revision ID: 0003_operators
Revises: 0002_airspace_policy
Create Date: 2026-09-29

P6-08. Named accounts, never a shared login: an audit log that says "the
operator did it" names no one.

## operators

`username` is stored lower-case and unique, so "Admin" and "admin" cannot be
two accounts. `password_hash` is a self-describing scrypt string (algorithm,
cost parameters, salt, digest) written and read only by `api/auth.py`; the
cost can be raised later without a migration, because each hash says what it
was made with.

An account is disabled, never deleted: `events.actor_id` points at it for as
long as the audit log exists.

`failed_logins` and `locked_until` throttle password guessing per account.
They are state, not audit - every attempt is also an `events` row.

## operator_sessions

Server-side, so a session can be revoked and stops working at once. The
token is never stored: only its SHA-256, so a copy of this table does not
log anyone in. A session ends at `expires_at` (absolute), after
`idle_timeout` without use, or at `revoked_at`, whichever is first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003_operators"
down_revision: str | None = "0002_airspace_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operators",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "username = lower(username) AND length(username) BETWEEN 3 AND 64",
            name="operators_username_lower_case",
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'operator', 'admin')", name="operators_role_known"
        ),
        sa.CheckConstraint("failed_logins >= 0", name="operators_failed_non_negative"),
    )

    op.create_table(
        "operator_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operators.id"),
            nullable=False,
        ),
        # SHA-256 of the token. The token itself is never stored.
        sa.Column("token_sha256", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_addr", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "octet_length(token_sha256) = 32", name="operator_sessions_sha256"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="operator_sessions_expire_later"
        ),
    )
    op.create_index(
        "operator_sessions_by_operator",
        "operator_sessions",
        ["operator_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("operator_sessions")
    op.drop_table("operators")
