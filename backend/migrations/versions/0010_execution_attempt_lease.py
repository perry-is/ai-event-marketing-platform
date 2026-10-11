"""Durable execution-attempt leases for interrupted work recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0010_execution_attempt_lease"
down_revision = "0009_credential_vault"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "execution_attempts",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Legacy RUNNING rows get the same conservative lease as new attempts. Recovery
    # still waits for expiry; a process restart alone never claims active work is stale.
    op.execute(
        "UPDATE execution_attempts SET lease_expires_at = started_at + interval '30 minutes' "
        "WHERE status = 'RUNNING' AND lease_expires_at IS NULL"
    )
    op.create_index(
        "ix_execution_attempt_running_lease",
        "execution_attempts",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade():
    op.drop_index("ix_execution_attempt_running_lease", table_name="execution_attempts")
    op.drop_column("execution_attempts", "lease_expires_at")
