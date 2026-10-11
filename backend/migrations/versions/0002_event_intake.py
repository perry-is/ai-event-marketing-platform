"""Add canonical Events and immutable intake-source records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_event_intake"
down_revision = "0001_auth_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(240)),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("timezone", sa.String(64)),
        sa.Column("delivery", sa.String(16)),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("fact_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("intake_proposals", postgresql.JSONB(), nullable=False),
        sa.Column("internal_notes", sa.Text()),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_events_revision_positive"),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_events_end_after_start",
        ),
    )
    op.create_index("ix_events_updated_at", "events", ["updated_at"])
    op.create_table(
        "source_materials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("original_name", sa.String(240)),
        sa.Column("storage_key", sa.String(80), unique=True),
        sa.Column("original_text", sa.Text()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("detected_type", sa.String(100), nullable=False),
        sa.Column("input_method", sa.String(16), nullable=False),
        sa.Column("processing_status", sa.String(32), nullable=False),
        sa.Column("extraction", postgresql.JSONB(), nullable=False),
        sa.Column(
            "supplied_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('TEXT', 'FILE')", name="ck_sources_kind"),
        sa.CheckConstraint(
            "(original_text IS NOT NULL AND storage_key IS NULL AND kind = 'TEXT') OR "
            "(original_text IS NULL AND storage_key IS NOT NULL AND kind = 'FILE')",
            name="ck_sources_one_original",
        ),
        sa.CheckConstraint("byte_count >= 0", name="ck_sources_nonnegative_size"),
        sa.CheckConstraint(
            "input_method IN ('TYPED', 'PASTED', 'UPLOAD')", name="ck_sources_input_method"
        ),
    )
    op.create_index("ix_sources_event_id", "source_materials", ["event_id"])
    op.create_index("ix_sources_sha256", "source_materials", ["sha256"])


def downgrade():
    op.drop_table("source_materials")
    op.drop_index("ix_events_updated_at", table_name="events")
    op.drop_table("events")
