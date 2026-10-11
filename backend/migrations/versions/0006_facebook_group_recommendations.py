"""Facebook Group catalog evidence, advisory recommendations and human decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_facebook_groups"
down_revision = "0005_campaign_graphics"
branch_labels = None
depends_on = None


def upgrade():
    # Reviewed data model: catalog imports are Source Material without an owning Event.
    op.add_column("source_materials", sa.Column("catalog_kind", sa.String(40)))
    op.alter_column("source_materials", "event_id", nullable=True)
    op.alter_column("source_materials", "supplied_by", nullable=True)
    op.create_check_constraint(
        "ck_sources_owner",
        "source_materials",
        "(catalog_kind IS NULL AND event_id IS NOT NULL AND supplied_by IS NOT NULL) OR "
        "(catalog_kind = 'FACEBOOK_GROUP_MASTER' AND event_id IS NULL AND kind = 'FILE')",
    )
    op.create_index(
        "uq_sources_catalog_sha256",
        "source_materials",
        ["catalog_kind", "sha256"],
        unique=True,
        postgresql_where=sa.text("catalog_kind IS NOT NULL"),
    )

    op.create_table(
        "facebook_group_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_materials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_tab", sa.String(80), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("row_fingerprint", sa.String(64), nullable=False),
        sa.Column("raw_record", postgresql.JSONB(), nullable=False),
        sa.Column("display_name", sa.String(300), nullable=False),
        sa.Column("recorded_urls", postgresql.JSONB(), nullable=False),
        sa.Column("chosen_url", sa.String(500)),
        sa.Column("group_key", sa.String(200)),
        sa.Column("identity_status", sa.String(32), nullable=False),
        sa.Column("geography", postgresql.JSONB(), nullable=False),
        sa.Column("member_count", sa.Integer()),
        sa.Column("audience", postgresql.JSONB(), nullable=False),
        sa.Column("admin_notes", postgresql.JSONB(), nullable=False),
        sa.Column("restrictions", postgresql.JSONB(), nullable=False),
        sa.Column("quality_flags", postgresql.JSONB(), nullable=False),
        sa.Column("search_tokens", postgresql.JSONB(), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("import_status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_material_id", "source_tab", "source_row", name="uq_group_record_source_row"
        ),
        sa.CheckConstraint("source_row > 0", name="ck_group_record_row_positive"),
        sa.CheckConstraint("revision > 0", name="ck_group_record_revision_positive"),
        sa.CheckConstraint(
            "review_status IN ('UNREVIEWED', 'USABLE', 'CONFLICT', 'EXCLUDED')",
            name="ck_group_record_review_status",
        ),
        sa.CheckConstraint(
            "import_status IN ('CURRENT', 'SUPERSEDED')", name="ck_group_record_import_status"
        ),
        sa.CheckConstraint(
            "identity_status IN ('GROUP_PATH', 'UNRESOLVED_SHARE_ALIAS', 'UNRECOGNIZED_URL')",
            name="ck_group_record_identity_status",
        ),
        sa.CheckConstraint(
            "member_count IS NULL OR member_count >= 0", name="ck_group_record_members"
        ),
        sa.CheckConstraint("row_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_group_record_fp"),
    )
    op.create_index("ix_group_records_review_status", "facebook_group_records", ["review_status"])
    op.create_index("ix_group_records_display_name", "facebook_group_records", ["display_name"])
    op.create_index("ix_group_records_fingerprint", "facebook_group_records", ["row_fingerprint"])
    op.execute(
        """
        CREATE FUNCTION protect_group_record_source() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'facebook group source records cannot be deleted';
            END IF;
            IF NEW.source_material_id IS DISTINCT FROM OLD.source_material_id OR
               NEW.source_tab IS DISTINCT FROM OLD.source_tab OR
               NEW.source_row IS DISTINCT FROM OLD.source_row OR
               NEW.row_fingerprint IS DISTINCT FROM OLD.row_fingerprint OR
               NEW.raw_record IS DISTINCT FROM OLD.raw_record OR
               NEW.recorded_urls IS DISTINCT FROM OLD.recorded_urls THEN
                RAISE EXCEPTION 'facebook group source evidence is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER facebook_group_source_immutable "
        "BEFORE UPDATE OR DELETE ON facebook_group_records "
        "FOR EACH ROW EXECUTE FUNCTION protect_group_record_source()"
    )

    op.create_table(
        "group_recommendations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "group_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facebook_group_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column("group_revision", sa.Integer(), nullable=False),
        sa.Column("filter_result", sa.String(40), nullable=False),
        sa.Column("rank", sa.Integer()),
        sa.Column("tier", sa.String(16)),
        sa.Column("reason", sa.String(400)),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("cautions", postgresql.JSONB(), nullable=False),
        sa.Column("score", sa.Integer()),
        sa.Column("explanation_source", sa.String(16)),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("model_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "group_record_id", name="uq_group_recommendation_run"),
        sa.CheckConstraint(
            "tier IS NULL OR tier IN ('BEST_FIT', 'GOOD_FIT', 'POSSIBLE_FIT')",
            name="ck_group_recommendation_tier",
        ),
        sa.CheckConstraint(
            "explanation_source IS NULL OR explanation_source IN ('MODEL', 'DETERMINISTIC')",
            name="ck_group_recommendation_explanation_source",
        ),
        sa.CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="ck_group_recommendation_hash"),
    )
    op.create_index(
        "ix_group_recommendations_campaign_run",
        "group_recommendations",
        ["campaign_id", "created_at", "run_id"],
    )

    op.create_table(
        "group_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "group_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("facebook_group_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "recommendation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("group_recommendations.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("campaign_revision", sa.Integer(), nullable=False),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column("group_revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "campaign_id",
            "campaign_revision",
            "group_record_id",
            name="uq_group_decision_revision",
        ),
        sa.CheckConstraint("decision IN ('SELECT', 'REJECT')", name="ck_group_decision_value"),
    )
    op.create_index(
        "ix_group_decisions_campaign_group",
        "group_decisions",
        ["campaign_id", "group_record_id", "campaign_revision"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_group_history_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'group recommendation and decision history is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("group_recommendations", "group_decisions"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_group_history_mutation()"
        )


def downgrade():
    for table in ("group_decisions", "group_recommendations"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_group_history_mutation()")
    op.drop_index("ix_group_decisions_campaign_group", table_name="group_decisions")
    op.drop_table("group_decisions")
    op.drop_index("ix_group_recommendations_campaign_run", table_name="group_recommendations")
    op.drop_table("group_recommendations")
    op.execute("DROP TRIGGER facebook_group_source_immutable ON facebook_group_records")
    op.execute("DROP FUNCTION protect_group_record_source()")
    op.drop_index("ix_group_records_fingerprint", table_name="facebook_group_records")
    op.drop_index("ix_group_records_display_name", table_name="facebook_group_records")
    op.drop_index("ix_group_records_review_status", table_name="facebook_group_records")
    op.drop_table("facebook_group_records")
    # Catalog source rows cannot satisfy the earlier event-owned constraints.
    op.execute("DELETE FROM source_materials WHERE catalog_kind IS NOT NULL")
    op.drop_index("uq_sources_catalog_sha256", table_name="source_materials")
    op.drop_constraint("ck_sources_owner", "source_materials", type_="check")
    op.alter_column("source_materials", "supplied_by", nullable=False)
    op.alter_column("source_materials", "event_id", nullable=False)
    op.drop_column("source_materials", "catalog_kind")
