"""Persist immutable campaign graphics and version-bound manifest entries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_campaign_graphics"
down_revision = "0004_approval_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_campaign_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_asset_id", postgresql.UUID(as_uuid=True)),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("generation_mode", sa.String(16), nullable=False),
        sa.Column("model_id", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("storage_key", sa.String(80), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("generation_metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="RESTRICT", name="fk_assets_campaign"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "source_campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_assets_source_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "source_asset_id"],
            ["assets.campaign_id", "assets.id"],
            name="fk_assets_source_asset",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("campaign_id", "id", name="uq_assets_campaign_id"),
        sa.UniqueConstraint("storage_key", name="uq_assets_storage_key"),
        sa.CheckConstraint(
            "asset_type IN ('VISUAL_LAYER', 'CAMPAIGN_GRAPHIC')", name="ck_assets_type"
        ),
        sa.CheckConstraint(
            "generation_mode IN ('GENERATE', 'EDIT', 'COMPOSE')",
            name="ck_assets_generation_mode",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="ck_assets_mime_type",
        ),
        sa.CheckConstraint("byte_count > 0", name="ck_assets_byte_count_positive"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_assets_dimensions_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_assets_sha256_format"),
    )
    op.create_index("ix_assets_campaign_created", "assets", ["campaign_id", "created_at"])

    op.create_table(
        "campaign_version_assets",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id", "version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_campaign_version_assets_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "asset_id"],
            ["assets.campaign_id", "assets.id"],
            name="fk_campaign_version_assets_asset",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("version_id", "role", name="uq_campaign_version_asset_role"),
        sa.CheckConstraint("role = 'PRIMARY_GRAPHIC'", name="ck_campaign_version_assets_role"),
    )
    op.create_index("ix_campaign_version_assets_asset", "campaign_version_assets", ["asset_id"])

    op.execute(
        """
        CREATE FUNCTION reject_generated_asset_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'generated assets are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER assets_immutable BEFORE UPDATE OR DELETE ON assets "
        "FOR EACH ROW EXECUTE FUNCTION reject_generated_asset_mutation()"
    )
    op.execute(
        "CREATE TRIGGER campaign_version_assets_immutable "
        "BEFORE UPDATE OR DELETE ON campaign_version_assets "
        "FOR EACH ROW EXECUTE FUNCTION reject_generated_asset_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION validate_campaign_version_asset_manifest() RETURNS trigger AS $$
        DECLARE
            checked_version_id uuid;
            version_row campaign_versions%ROWTYPE;
            manifest_count integer;
            manifest_asset campaign_version_assets%ROWTYPE;
            asset_row assets%ROWTYPE;
            graphic jsonb;
        BEGIN
            IF TG_TABLE_NAME = 'campaign_versions' THEN
                checked_version_id := NEW.id;
            ELSE
                checked_version_id := NEW.version_id;
            END IF;

            SELECT * INTO version_row FROM campaign_versions WHERE id = checked_version_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            SELECT count(*) INTO manifest_count
              FROM campaign_version_assets WHERE version_id = checked_version_id;

            IF version_row.snapshot ? 'primary_graphic' THEN
                graphic := version_row.snapshot->'primary_graphic';
                IF manifest_count <> 1 THEN
                    RAISE EXCEPTION
                        'campaign version graphic manifest must contain one primary graphic';
                END IF;
                SELECT * INTO manifest_asset FROM campaign_version_assets
                 WHERE version_id = checked_version_id AND role = 'PRIMARY_GRAPHIC';
                SELECT * INTO asset_row FROM assets
                 WHERE id = manifest_asset.asset_id AND campaign_id = version_row.campaign_id;
                IF NOT FOUND OR asset_row.asset_type <> 'CAMPAIGN_GRAPHIC' OR
                   graphic->>'asset_id' IS DISTINCT FROM asset_row.id::text OR
                   graphic->>'sha256' IS DISTINCT FROM asset_row.sha256 OR
                   graphic->>'role' IS DISTINCT FROM manifest_asset.role OR
                   graphic->>'composition_version' IS DISTINCT FROM
                       asset_row.generation_metadata->>'composition_version' THEN
                    RAISE EXCEPTION 'campaign version graphic snapshot does not match its asset';
                END IF;
            ELSIF manifest_count <> 0 THEN
                RAISE EXCEPTION 'text-only campaign version cannot acquire a graphic later';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER campaign_version_asset_snapshot_check "
        "AFTER INSERT ON campaign_versions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION validate_campaign_version_asset_manifest()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER campaign_version_asset_manifest_check "
        "AFTER INSERT ON campaign_version_assets DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION validate_campaign_version_asset_manifest()"
    )


def downgrade():
    op.execute("DROP TRIGGER campaign_version_asset_manifest_check ON campaign_version_assets")
    op.execute("DROP TRIGGER campaign_version_asset_snapshot_check ON campaign_versions")
    op.execute("DROP FUNCTION validate_campaign_version_asset_manifest()")
    op.execute("DROP TRIGGER campaign_version_assets_immutable ON campaign_version_assets")
    op.execute("DROP TRIGGER assets_immutable ON assets")
    op.execute("DROP FUNCTION reject_generated_asset_mutation()")
    op.drop_index("ix_campaign_version_assets_asset", table_name="campaign_version_assets")
    op.drop_table("campaign_version_assets")
    op.drop_index("ix_assets_campaign_created", table_name="assets")
    op.drop_table("assets")
