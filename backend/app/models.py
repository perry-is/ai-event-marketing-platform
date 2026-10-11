import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppUser(Base):
    __tablename__ = "app_users"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_user_identity"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    # Operator bootstrap entry, not the mutable email subsequently returned by Google.
    allowlist_email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServerSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("kind IN ('LOGIN_FLOW', 'APP_SESSION')", name="ck_session_kind"),
        CheckConstraint(
            "(kind = 'LOGIN_FLOW' AND user_id IS NULL AND authenticated_at IS NULL) OR "
            "(kind = 'APP_SESSION' AND user_id IS NOT NULL AND authenticated_at IS NOT NULL)",
            name="ck_session_identity",
        ),
        Index("ix_sessions_expires_at", "expires_at"),
        Index("ix_sessions_user_id", "user_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT")
    )
    authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    private_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderConnection(Base):
    """Non-secret account and OAuth lifecycle metadata for one supported provider."""

    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("provider", name="uq_provider_connection_provider"),
        CheckConstraint("provider IN ('LINKEDIN', 'INSTAGRAM')", name="ck_provider_name"),
        CheckConstraint(
            "status IN ('CONNECTED', 'REAUTH_REQUIRED', 'DISCONNECTED')",
            name="ck_provider_connection_status",
        ),
        Index("ix_provider_connections_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(240), nullable=False)
    account_id: Mapped[str] = mapped_column(String(240), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(240))
    account_type: Mapped[str | None] = mapped_column(String(32))
    granted_scopes: Mapped[list] = mapped_column(JSONB, nullable=False)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reauthorization_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"ProviderConnection(provider={self.provider!r}, status={self.status!r})"


class CredentialEnvelope(Base):
    """Authenticated ciphertext only; token values never have plaintext DB columns."""

    __tablename__ = "credential_envelopes"
    __table_args__ = (
        UniqueConstraint("connection_id", name="uq_credential_envelope_connection"),
        CheckConstraint("payload_schema_version > 0", name="ck_credential_payload_version"),
        CheckConstraint("octet_length(nonce) = 12", name="ck_credential_nonce_length"),
        Index("ix_credential_envelopes_key_id", "key_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False
    )
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return "CredentialEnvelope(encrypted=True)"


class OAuthAuthorizationAttempt(Base):
    """Single-use, user/session-bound OAuth state; raw state is never persisted."""

    __tablename__ = "oauth_authorization_attempts"
    __table_args__ = (
        CheckConstraint("provider IN ('LINKEDIN', 'INSTAGRAM')", name="ck_oauth_attempt_provider"),
        CheckConstraint("return_destination = 'SETTINGS'", name="ck_oauth_return_destination"),
        UniqueConstraint("state_digest", name="uq_oauth_attempt_state_digest"),
        Index("ix_oauth_attempts_expiry", "expires_at"),
        Index("ix_oauth_attempts_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    return_destination: Mapped[str] = mapped_column(String(16), nullable=False, default="SETTINGS")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_events_revision_positive"),
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_events_end_after_start",
        ),
        Index("ix_events_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str | None] = mapped_column(String(240))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str | None] = mapped_column(String(64))
    delivery: Mapped[str | None] = mapped_column(String(16))
    facts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fact_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    intake_proposals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    internal_notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceMaterial(Base):
    __tablename__ = "source_materials"
    __table_args__ = (
        CheckConstraint("kind IN ('TEXT', 'FILE')", name="ck_sources_kind"),
        CheckConstraint(
            "(original_text IS NOT NULL AND storage_key IS NULL AND kind = 'TEXT') OR "
            "(original_text IS NULL AND storage_key IS NOT NULL AND kind = 'FILE')",
            name="ck_sources_one_original",
        ),
        CheckConstraint("byte_count >= 0", name="ck_sources_nonnegative_size"),
        Index("ix_sources_event_id", "event_id"),
        Index("ix_sources_sha256", "sha256"),
        Index(
            "uq_sources_catalog_sha256",
            "catalog_kind",
            "sha256",
            unique=True,
            postgresql_where=text("catalog_kind IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Null only for operator-imported catalog evidence (catalog_kind), never for Event intake.
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    catalog_kind: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    original_name: Mapped[str | None] = mapped_column(String(240))
    storage_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    original_text: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_type: Mapped[str] = mapped_column(String(100), nullable=False)
    input_method: Mapped[str] = mapped_column(String(16), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    supplied_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_campaigns_revision_positive"),
        CheckConstraint(
            "state IN ('DRAFT', 'GENERATING', 'REVIEW', 'APPROVED', 'EXECUTING', "
            "'COMPLETE', 'ABANDONED')",
            name="ck_campaigns_state",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_campaigns_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "active_approval_id"],
            ["approvals.campaign_id", "approvals.id"],
            name="fk_campaigns_active_approval",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_campaigns_event_state_updated", "event_id", "state", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    active_approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CampaignVersion(Base):
    __tablename__ = "campaign_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_campaign_versions_number_positive"),
        CheckConstraint("event_revision > 0", name="ck_campaign_versions_event_revision_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_campaign_versions_hash_format"),
        UniqueConstraint("campaign_id", "version_number", name="uq_campaign_version_number"),
        UniqueConstraint("campaign_id", "id", name="uq_campaign_version_campaign_id"),
        ForeignKeyConstraint(
            ["campaign_id", "parent_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_campaign_version_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_campaign_versions_campaign_created", "campaign_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    canonical_snapshot_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_id: Mapped[str] = mapped_column(String(80), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(40), nullable=False)
    revision_request: Mapped[str | None] = mapped_column(Text)
    revision_targets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GeneratedAsset(Base):
    """Private immutable campaign artifact; raw visual layers are not review authority."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("campaign_id", "id", name="uq_assets_campaign_id"),
        CheckConstraint(
            "asset_type IN ('VISUAL_LAYER', 'CAMPAIGN_GRAPHIC')",
            name="ck_assets_type",
        ),
        CheckConstraint(
            "generation_mode IN ('GENERATE', 'EDIT', 'COMPOSE')",
            name="ck_assets_generation_mode",
        ),
        CheckConstraint(
            "mime_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="ck_assets_mime_type",
        ),
        CheckConstraint("byte_count > 0", name="ck_assets_byte_count_positive"),
        CheckConstraint("width > 0 AND height > 0", name="ck_assets_dimensions_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_assets_sha256_format"),
        ForeignKeyConstraint(
            ["campaign_id", "source_campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_assets_source_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "source_asset_id"],
            ["assets.campaign_id", "assets.id"],
            name="fk_assets_source_asset",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_assets_campaign_created", "campaign_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    source_campaign_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    generation_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    storage_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    generation_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CampaignVersionAsset(Base):
    """Append-only manifest association between an exact version and its visible graphic."""

    __tablename__ = "campaign_version_assets"
    __table_args__ = (
        CheckConstraint("role = 'PRIMARY_GRAPHIC'", name="ck_campaign_version_assets_role"),
        ForeignKeyConstraint(
            ["campaign_id", "version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_campaign_version_assets_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "asset_id"],
            ["assets.campaign_id", "assets.id"],
            name="fk_campaign_version_assets_asset",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("version_id", "role", name="uq_campaign_version_asset_role"),
        Index("ix_campaign_version_assets_asset", "asset_id"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("campaign_id", "id", name="uq_approvals_campaign_id"),
        UniqueConstraint(
            "campaign_id",
            "campaign_version_id",
            "id",
            name="uq_approvals_execution_scope",
        ),
        Index("ix_approvals_campaign_time", "campaign_id", "approved_at"),
        ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_approvals_campaign_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("event_revision > 0", name="ck_approvals_event_revision_positive"),
        CheckConstraint(
            "runtime_mode IS NULL OR runtime_mode IN ('SIMULATED', 'LIVE')",
            name="ck_approvals_legacy_runtime_mode",
        ),
        CheckConstraint(
            "version_hash ~ '^[0-9a-f]{64}$' AND "
            "destination_plan_hash ~ '^[0-9a-f]{64}$' AND "
            "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[0-9a-f]{64}$')",
            name="ck_approvals_hashes_format",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approver_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_plan_hash: Mapped[str | None] = mapped_column(String(64))
    destination_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Legacy global mode is retained only for historical rows and is not execution authority.
    runtime_mode: Mapped[str | None] = mapped_column(String(16))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DestinationExecution(Base):
    __tablename__ = "destination_executions"
    __table_args__ = (
        UniqueConstraint(
            "approval_id",
            "destination_key",
            "target_key",
            name="uq_destination_execution_identity",
        ),
        UniqueConstraint("idempotency_key", name="uq_destination_execution_idempotency"),
        ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_destination_execution_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id", "approval_id"],
            ["approvals.campaign_id", "approvals.campaign_version_id", "approvals.id"],
            name="fk_destination_execution_approval",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status IN ('READY', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', "
            "'MANUAL_PENDING', 'MANUAL_DONE', 'BLOCKED', 'SKIPPED')",
            name="ck_destination_execution_status",
        ),
        CheckConstraint("revision > 0", name="ck_destination_execution_revision_positive"),
        CheckConstraint(
            "execution_kind IN ('AUTO', 'MANUAL')", name="ck_destination_execution_kind"
        ),
        CheckConstraint(
            "runtime_mode IS NULL OR runtime_mode IN ('SIMULATED', 'LIVE')",
            name="ck_destination_execution_mode",
        ),
        CheckConstraint(
            "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[0-9a-f]{64}$') AND "
            "destination_plan_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_destination_execution_hashes",
        ),
        Index("ix_destination_execution_campaign_status", "campaign_id", "status"),
        Index("ix_destination_execution_approval", "approval_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    destination_key: Mapped[str] = mapped_column(String(48), nullable=False)
    target_key: Mapped[str] = mapped_column(String(160), nullable=False)
    execution_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    runtime_mode: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_plan_hash: Mapped[str | None] = mapped_column(String(64))
    destination_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_entry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manual_package: Mapped[dict | None] = mapped_column(JSONB)
    manual_package_hash: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(240))
    external_url: Mapped[str | None] = mapped_column(String(1000))
    result_summary: Mapped[str | None] = mapped_column(String(500))
    error_category: Mapped[str | None] = mapped_column(String(48))
    human_error: Mapped[str | None] = mapped_column(String(500))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="NOT_REQUIRED"
    )
    disposition: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint("execution_id", "attempt_number", name="uq_execution_attempt_number"),
        CheckConstraint("attempt_number > 0", name="ck_execution_attempt_number_positive"),
        CheckConstraint("status IN ('RUNNING', 'FINALIZED')", name="ck_execution_attempt_status"),
        CheckConstraint(
            "operation IN ('PUBLISH', 'VERIFY', 'PREPARE_MANUAL', 'ACK_MANUAL', "
            "'RECONCILE', 'DISPOSITION')",
            name="ck_execution_attempt_operation",
        ),
        ForeignKeyConstraint(
            ["execution_id"],
            ["destination_executions.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_execution_attempt_history", "execution_id", "attempt_number"),
        Index(
            "uq_execution_attempt_one_running",
            "execution_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index(
            "ix_execution_attempt_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    adapter_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(240))
    external_url: Mapped[str | None] = mapped_column(String(1000))
    error_category: Mapped[str | None] = mapped_column(String(48))
    human_error: Mapped[str | None] = mapped_column(String(500))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('USER', 'SYSTEM', 'ADAPTER')", name="ck_audit_actor_type"),
        Index("ix_audit_campaign_occurred", "campaign_id", "occurred_at"),
        Index("ix_audit_execution_occurred", "destination_execution_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(12), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT")
    )
    campaign_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaign_versions.id", ondelete="RESTRICT")
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT")
    )
    destination_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("destination_executions.id", ondelete="RESTRICT")
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT")
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    safe_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FacebookGroupRecord(Base):
    """One imported Group Master row: immutable evidence plus conservative curated fields."""

    __tablename__ = "facebook_group_records"
    __table_args__ = (
        UniqueConstraint(
            "source_material_id", "source_tab", "source_row", name="uq_group_record_source_row"
        ),
        Index("ix_group_records_review_status", "review_status"),
        Index("ix_group_records_display_name", "display_name"),
        Index("ix_group_records_fingerprint", "row_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_material_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_materials.id", ondelete="RESTRICT"), nullable=False
    )
    source_tab: Mapped[str] = mapped_column(String(80), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_record: Mapped[dict] = mapped_column(JSONB, nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    recorded_urls: Mapped[list] = mapped_column(JSONB, nullable=False)
    chosen_url: Mapped[str | None] = mapped_column(String(500))
    group_key: Mapped[str | None] = mapped_column(String(200))
    identity_status: Mapped[str] = mapped_column(String(32), nullable=False)
    geography: Mapped[dict] = mapped_column(JSONB, nullable=False)
    member_count: Mapped[int | None] = mapped_column(Integer)
    audience: Mapped[dict] = mapped_column(JSONB, nullable=False)
    admin_notes: Mapped[list] = mapped_column(JSONB, nullable=False)
    restrictions: Mapped[list] = mapped_column(JSONB, nullable=False)
    quality_flags: Mapped[list] = mapped_column(JSONB, nullable=False)
    search_tokens: Mapped[list] = mapped_column(JSONB, nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    import_status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GroupRecommendation(Base):
    """Append-only advisory filter/rank result for one Group in one recommendation run."""

    __tablename__ = "group_recommendations"
    __table_args__ = (
        UniqueConstraint("run_id", "group_record_id", name="uq_group_recommendation_run"),
        Index("ix_group_recommendations_campaign_run", "campaign_id", "created_at", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    group_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("facebook_group_records.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    group_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    filter_result: Mapped[str] = mapped_column(String(40), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    tier: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(400))
    signals: Mapped[list] = mapped_column(JSONB, nullable=False)
    cautions: Mapped[list] = mapped_column(JSONB, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    explanation_source: Mapped[str | None] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GroupDecision(Base):
    """Append-only human SELECT/REJECT choice; the highest campaign revision wins."""

    __tablename__ = "group_decisions"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "campaign_revision",
            "group_record_id",
            name="uq_group_decision_revision",
        ),
        Index(
            "ix_group_decisions_campaign_group",
            "campaign_id",
            "group_record_id",
            "campaign_revision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    group_record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("facebook_group_records.id", ondelete="RESTRICT"), nullable=False
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("group_recommendations.id", ondelete="RESTRICT")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    campaign_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    group_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DestinationResourceBinding(Base):
    """Verified remote listing identity reused by successor approvals (never a credential)."""

    __tablename__ = "destination_resource_bindings"
    __table_args__ = (
        UniqueConstraint(
            "destination_key", "account_key", "event_id", name="uq_resource_binding_local"
        ),
        UniqueConstraint(
            "destination_key", "account_key", "external_id", name="uq_resource_binding_remote"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    destination_key: Mapped[str] = mapped_column(String(48), nullable=False)
    account_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    external_url: Mapped[str | None] = mapped_column(String(1000))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    bound_by_execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("destination_executions.id", ondelete="RESTRICT"), nullable=False
    )
    last_execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("destination_executions.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
