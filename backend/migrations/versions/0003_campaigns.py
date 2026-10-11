"""Add Campaign workflows and immutable CampaignVersion snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_campaigns"
down_revision = "0002_event_intake"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_campaigns_revision_positive"),
        sa.CheckConstraint("state IN ('DRAFT', 'GENERATING', 'REVIEW')", name="ck_campaigns_state"),
    )
    op.create_index(
        "ix_campaigns_event_state_updated", "campaigns", ["event_id", "state", "updated_at"]
    )

    op.create_table(
        "campaign_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_snapshot_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("generator_id", sa.String(80), nullable=False),
        sa.Column("generator_version", sa.String(40), nullable=False),
        sa.Column("revision_request", sa.Text()),
        sa.Column("revision_targets", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_number > 0", name="ck_campaign_versions_number_positive"),
        sa.CheckConstraint(
            "event_revision > 0", name="ck_campaign_versions_event_revision_positive"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_campaign_versions_hash_format"
        ),
        sa.UniqueConstraint("campaign_id", "version_number", name="uq_campaign_version_number"),
        sa.UniqueConstraint("campaign_id", "id", name="uq_campaign_version_campaign_id"),
        sa.ForeignKeyConstraint(
            ["campaign_id", "parent_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_campaign_version_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index(
        "ix_campaign_versions_campaign_created",
        "campaign_versions",
        ["campaign_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_campaigns_current_version",
        "campaigns",
        "campaign_versions",
        ["id", "current_version_id"],
        ["campaign_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION reject_campaign_version_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'campaign versions are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_versions_immutable
        BEFORE UPDATE OR DELETE ON campaign_versions
        FOR EACH ROW EXECUTE FUNCTION reject_campaign_version_mutation();
        """
    )


def downgrade():
    op.execute("DROP TRIGGER campaign_versions_immutable ON campaign_versions")
    op.execute("DROP FUNCTION reject_campaign_version_mutation()")
    op.drop_constraint("fk_campaigns_current_version", "campaigns", type_="foreignkey")
    op.drop_index("ix_campaign_versions_campaign_created", table_name="campaign_versions")
    op.drop_table("campaign_versions")
    op.drop_index("ix_campaigns_event_state_updated", table_name="campaigns")
    op.drop_table("campaigns")
