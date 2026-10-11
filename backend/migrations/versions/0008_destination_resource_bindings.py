"""Stable external resource binding for live destination listings (Eventbrite)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_resource_bindings"
down_revision = "0007_mixed_mode_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "destination_resource_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("destination_key", sa.String(48), nullable=False),
        sa.Column("account_key", sa.String(160), nullable=False),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("external_url", sa.String(1000)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("resource_metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "bound_by_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("destination_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("destination_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("first_bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "destination_key", "account_key", "event_id", name="uq_resource_binding_local"
        ),
        sa.UniqueConstraint(
            "destination_key", "account_key", "external_id", name="uq_resource_binding_remote"
        ),
        sa.CheckConstraint("state IN ('DRAFT', 'PUBLISHED')", name="ck_resource_binding_state"),
        sa.CheckConstraint("revision > 0", name="ck_resource_binding_revision"),
        sa.CheckConstraint(
            "external_url IS NULL OR external_url LIKE 'https://%'",
            name="ck_resource_binding_https",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION protect_resource_binding_identity() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'destination resource bindings cannot be deleted';
            END IF;
            IF NEW.destination_key IS DISTINCT FROM OLD.destination_key OR
               NEW.account_key IS DISTINCT FROM OLD.account_key OR
               NEW.event_id IS DISTINCT FROM OLD.event_id OR
               NEW.external_id IS DISTINCT FROM OLD.external_id OR
               NEW.bound_by_execution_id IS DISTINCT FROM OLD.bound_by_execution_id OR
               NEW.first_bound_at IS DISTINCT FROM OLD.first_bound_at THEN
                RAISE EXCEPTION 'destination resource binding identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER destination_resource_binding_identity "
        "BEFORE UPDATE OR DELETE ON destination_resource_bindings "
        "FOR EACH ROW EXECUTE FUNCTION protect_resource_binding_identity()"
    )


def downgrade():
    op.execute(
        "DROP TRIGGER destination_resource_binding_identity ON destination_resource_bindings"
    )
    op.execute("DROP FUNCTION protect_resource_binding_identity()")
    op.drop_table("destination_resource_bindings")
