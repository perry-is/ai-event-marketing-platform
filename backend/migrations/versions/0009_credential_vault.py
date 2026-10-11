"""Encrypted delegated OAuth credentials and single-use authorization state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_credential_vault"
down_revision = "0008_resource_bindings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider_subject", sa.String(240), nullable=False),
        sa.Column("account_id", sa.String(240), nullable=False),
        sa.Column("account_name", sa.String(240)),
        sa.Column("account_type", sa.String(32)),
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True)),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True)),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("reauthorization_required_at", sa.DateTime(timezone=True)),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", name="uq_provider_connection_provider"),
        sa.CheckConstraint("provider IN ('LINKEDIN', 'INSTAGRAM')", name="ck_provider_name"),
        sa.CheckConstraint(
            "status IN ('CONNECTED', 'REAUTH_REQUIRED', 'DISCONNECTED')",
            name="ck_provider_connection_status",
        ),
    )
    op.create_index("ix_provider_connections_status", "provider_connections", ["status"])
    op.create_table(
        "credential_envelopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connection_id", name="uq_credential_envelope_connection"),
        sa.CheckConstraint("payload_schema_version > 0", name="ck_credential_payload_version"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_credential_nonce_length"),
    )
    op.create_index("ix_credential_envelopes_key_id", "credential_envelopes", ["key_id"])
    op.create_table(
        "oauth_authorization_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state_digest", sa.String(64), nullable=False),
        sa.Column("return_destination", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("state_digest", name="uq_oauth_attempt_state_digest"),
        sa.CheckConstraint(
            "provider IN ('LINKEDIN', 'INSTAGRAM')", name="ck_oauth_attempt_provider"
        ),
        sa.CheckConstraint("return_destination = 'SETTINGS'", name="ck_oauth_return_destination"),
    )
    op.create_index("ix_oauth_attempts_expiry", "oauth_authorization_attempts", ["expires_at"])
    op.create_index("ix_oauth_attempts_user", "oauth_authorization_attempts", ["user_id"])


def downgrade():
    op.drop_index("ix_oauth_attempts_user", table_name="oauth_authorization_attempts")
    op.drop_index("ix_oauth_attempts_expiry", table_name="oauth_authorization_attempts")
    op.drop_table("oauth_authorization_attempts")
    op.drop_index("ix_credential_envelopes_key_id", table_name="credential_envelopes")
    op.drop_table("credential_envelopes")
    op.drop_index("ix_provider_connections_status", table_name="provider_connections")
    op.drop_table("provider_connections")
