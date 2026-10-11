"""Add exact-version approvals and simulated destination execution history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_approval_execution"
down_revision = "0003_campaigns"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_campaigns_state", "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_state",
        "campaigns",
        "state IN ('DRAFT', 'GENERATING', 'REVIEW', 'APPROVED', 'EXECUTING', "
        "'COMPLETE', 'ABANDONED')",
    )

    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("approved_plan_hash", sa.String(64), nullable=False),
        sa.Column("destination_plan", postgresql.JSONB(), nullable=False),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column("runtime_mode", sa.String(16), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="RESTRICT", name="fk_approvals_campaign"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_approvals_campaign_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"], ["app_users.id"], ondelete="RESTRICT", name="fk_approvals_approver"
        ),
        sa.UniqueConstraint("campaign_version_id", name="uq_approvals_campaign_version"),
        sa.UniqueConstraint("campaign_id", "id", name="uq_approvals_campaign_id"),
        sa.UniqueConstraint(
            "campaign_id", "campaign_version_id", "id", name="uq_approvals_execution_scope"
        ),
        sa.CheckConstraint("event_revision > 0", name="ck_approvals_event_revision_positive"),
        sa.CheckConstraint(
            "runtime_mode IN ('SIMULATED', 'LIVE')", name="ck_approvals_runtime_mode"
        ),
        sa.CheckConstraint(
            "version_hash ~ '^[0-9a-f]{64}$' AND approved_plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approvals_hashes_format",
        ),
    )
    op.create_index("ix_approvals_campaign_time", "approvals", ["campaign_id", "approved_at"])
    op.add_column("campaigns", sa.Column("active_approval_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_campaigns_active_approval",
        "campaigns",
        "approvals",
        ["id", "active_approval_id"],
        ["campaign_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "destination_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_key", sa.String(48), nullable=False),
        sa.Column("target_key", sa.String(160), nullable=False),
        sa.Column("execution_kind", sa.String(12), nullable=False),
        sa.Column("runtime_mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("approved_plan_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("plan_entry", postgresql.JSONB(), nullable=False),
        sa.Column("manual_package", postgresql.JSONB()),
        sa.Column("manual_package_hash", sa.String(64)),
        sa.Column("external_id", sa.String(240)),
        sa.Column("external_url", sa.String(1000)),
        sa.Column("result_summary", sa.String(500)),
        sa.Column("error_category", sa.String(48)),
        sa.Column("human_error", sa.String(500)),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("verification_status", sa.String(24), nullable=False),
        sa.Column("disposition", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id"],
            ["campaign_versions.campaign_id", "campaign_versions.id"],
            name="fk_destination_execution_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "campaign_version_id", "approval_id"],
            ["approvals.campaign_id", "approvals.campaign_version_id", "approvals.id"],
            name="fk_destination_execution_approval",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "campaign_version_id",
            "destination_key",
            "target_key",
            "runtime_mode",
            name="uq_destination_execution_identity",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_destination_execution_idempotency"),
        sa.CheckConstraint("revision > 0", name="ck_destination_execution_revision_positive"),
        sa.CheckConstraint(
            "status IN ('READY', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', "
            "'MANUAL_PENDING', 'MANUAL_DONE', 'BLOCKED', 'SKIPPED')",
            name="ck_destination_execution_status",
        ),
        sa.CheckConstraint(
            "execution_kind IN ('AUTO', 'MANUAL')", name="ck_destination_execution_kind"
        ),
        sa.CheckConstraint(
            "runtime_mode IN ('SIMULATED', 'LIVE')", name="ck_destination_execution_mode"
        ),
        sa.CheckConstraint(
            "approved_plan_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'",
            name="ck_destination_execution_hashes",
        ),
    )
    op.create_index(
        "ix_destination_execution_campaign_status",
        "destination_executions",
        ["campaign_id", "status"],
    )
    op.create_index("ix_destination_execution_approval", "destination_executions", ["approval_id"])

    op.create_table(
        "execution_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("outcome", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("adapter_revision", sa.String(40), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(240)),
        sa.Column("external_url", sa.String(1000)),
        sa.Column("error_category", sa.String(48)),
        sa.Column("human_error", sa.String(500)),
        sa.Column("retryable", sa.Boolean()),
        sa.Column("verification_status", sa.String(24), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["destination_executions.id"],
            ondelete="RESTRICT",
            name="fk_execution_attempt_execution",
        ),
        sa.UniqueConstraint("execution_id", "attempt_number", name="uq_execution_attempt_number"),
        sa.CheckConstraint("attempt_number > 0", name="ck_execution_attempt_number_positive"),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'FINALIZED')", name="ck_execution_attempt_status"
        ),
        sa.CheckConstraint(
            "operation IN ('PUBLISH', 'VERIFY', 'PREPARE_MANUAL', 'ACK_MANUAL', "
            "'RECONCILE', 'DISPOSITION')",
            name="ck_execution_attempt_operation",
        ),
    )
    op.create_index(
        "ix_execution_attempt_history", "execution_attempts", ["execution_id", "attempt_number"]
    )
    op.create_index(
        "uq_execution_attempt_one_running",
        "execution_attempts",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_type", sa.String(12), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True)),
        sa.Column("campaign_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True)),
        sa.Column("destination_execution_id", postgresql.UUID(as_uuid=True)),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True)),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("safe_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["app_users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["campaign_version_id"], ["campaign_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["destination_execution_id"], ["destination_executions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SYSTEM', 'ADAPTER')", name="ck_audit_actor_type"
        ),
    )
    op.create_index("ix_audit_campaign_occurred", "audit_events", ["campaign_id", "occurred_at"])
    op.create_index(
        "ix_audit_execution_occurred",
        "audit_events",
        ["destination_execution_id", "occurred_at"],
    )

    op.execute(
        """
        CREATE FUNCTION reject_immutable_approval_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'approvals are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER approvals_immutable BEFORE UPDATE OR DELETE ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_approval_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION protect_execution_attempt_history() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'execution attempts cannot be deleted';
            END IF;
            IF OLD.status <> 'RUNNING' OR NEW.status <> 'FINALIZED' OR
               NEW.id IS DISTINCT FROM OLD.id OR
               NEW.execution_id IS DISTINCT FROM OLD.execution_id OR
               NEW.attempt_number IS DISTINCT FROM OLD.attempt_number OR
               NEW.operation IS DISTINCT FROM OLD.operation OR
               NEW.started_at IS DISTINCT FROM OLD.started_at OR
               NEW.adapter_revision IS DISTINCT FROM OLD.adapter_revision OR
               NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint THEN
                RAISE EXCEPTION 'finalized execution attempts are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER execution_attempts_append_only "
        "BEFORE UPDATE OR DELETE ON execution_attempts "
        "FOR EACH ROW EXECUTE FUNCTION protect_execution_attempt_history()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit events are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION protect_destination_execution_identity() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'destination executions cannot be deleted';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id OR
               NEW.campaign_id IS DISTINCT FROM OLD.campaign_id OR
               NEW.campaign_version_id IS DISTINCT FROM OLD.campaign_version_id OR
               NEW.approval_id IS DISTINCT FROM OLD.approval_id OR
               NEW.destination_key IS DISTINCT FROM OLD.destination_key OR
               NEW.target_key IS DISTINCT FROM OLD.target_key OR
               NEW.execution_kind IS DISTINCT FROM OLD.execution_kind OR
               NEW.runtime_mode IS DISTINCT FROM OLD.runtime_mode OR
               NEW.approved_plan_hash IS DISTINCT FROM OLD.approved_plan_hash OR
               NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
               NEW.plan_entry IS DISTINCT FROM OLD.plan_entry OR
               NEW.manual_package IS DISTINCT FROM OLD.manual_package OR
               NEW.manual_package_hash IS DISTINCT FROM OLD.manual_package_hash THEN
                RAISE EXCEPTION 'destination execution identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER destination_execution_identity_immutable "
        "BEFORE UPDATE OR DELETE ON destination_executions "
        "FOR EACH ROW EXECUTE FUNCTION protect_destination_execution_identity()"
    )


def downgrade():
    op.execute("DROP TRIGGER destination_execution_identity_immutable ON destination_executions")
    op.execute("DROP FUNCTION protect_destination_execution_identity()")
    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION reject_audit_event_mutation()")
    op.execute("DROP TRIGGER execution_attempts_append_only ON execution_attempts")
    op.execute("DROP FUNCTION protect_execution_attempt_history()")
    op.execute("DROP TRIGGER approvals_immutable ON approvals")
    op.execute("DROP FUNCTION reject_immutable_approval_mutation()")
    op.drop_index("ix_audit_execution_occurred", table_name="audit_events")
    op.drop_index("ix_audit_campaign_occurred", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("uq_execution_attempt_one_running", table_name="execution_attempts")
    op.drop_index("ix_execution_attempt_history", table_name="execution_attempts")
    op.drop_table("execution_attempts")
    op.drop_index("ix_destination_execution_approval", table_name="destination_executions")
    op.drop_index("ix_destination_execution_campaign_status", table_name="destination_executions")
    op.drop_table("destination_executions")
    op.drop_constraint("fk_campaigns_active_approval", "campaigns", type_="foreignkey")
    op.drop_column("campaigns", "active_approval_id")
    op.drop_index("ix_approvals_campaign_time", table_name="approvals")
    op.drop_table("approvals")
    op.drop_constraint("ck_campaigns_state", "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_state",
        "campaigns",
        "state IN ('DRAFT', 'GENERATING', 'REVIEW')",
    )
