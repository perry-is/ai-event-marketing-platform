"""Bind approvals to immutable per-destination plans and their independent hashes."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0007_mixed_mode_approval"
down_revision = "0006_facebook_groups"
branch_labels = None
depends_on = None


_TRANSIENT_PLAN_KEYS = {
    "campaign_version_id",
    "created_at",
    "updated_at",
    "readiness",
    "readiness_message",
    "connector_health",
    "request_id",
    "ui_label",
    "preview_url",
}


def _clean(value):
    if isinstance(value, dict):
        return {
            key: _clean(child) for key, child in value.items() if key not in _TRANSIENT_PLAN_KEYS
        }
    if isinstance(value, list):
        return [_clean(child) for child in value]
    return value


def _plan_hash(plan):
    if not isinstance(plan, dict):
        raise ValueError("Historical approval has no destination plan")
    normalized = {
        key: _clean(value)
        for key, value in plan.items()
        if key not in _TRANSIENT_PLAN_KEYS | {"runtime_mode"}
    }
    targets = []
    for source in plan.get("targets", []):
        entry = _clean(source)
        entry.pop("runtime_mode", None)
        kind = entry.get("execution_kind")
        if kind == "AUTO":
            mode = source.get("runtime_mode", plan.get("runtime_mode"))
            if mode not in {"SIMULATED", "LIVE"}:
                raise ValueError("Historical AUTO plan has no valid runtime mode")
            entry["runtime_mode"] = mode
        elif kind != "MANUAL":
            raise ValueError("Historical plan has an unknown execution treatment")
        groups = entry.get("selected_groups")
        if isinstance(groups, list):
            entry["selected_groups"] = sorted(
                groups,
                key=lambda group: (
                    str(group.get("group_record_id", "")),
                    str(group.get("url", "")),
                ),
            )
        targets.append(entry)
    normalized["targets"] = sorted(
        targets,
        key=lambda entry: (
            str(entry.get("destination_key", "")),
            str(entry.get("target_key", "")),
        ),
    )
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_legacy_plan(plan):
    normalized = dict(plan)
    global_mode = normalized.pop("runtime_mode", None)
    normalized["schema_version"] = 2
    targets = []
    for original in normalized.get("targets", []):
        target = dict(original)
        if target.get("execution_kind") == "AUTO":
            target["runtime_mode"] = target.get("runtime_mode", global_mode)
        elif target.get("execution_kind") == "MANUAL":
            target.pop("runtime_mode", None)
        targets.append(target)
    normalized["targets"] = targets
    return normalized


def upgrade():
    # The old immutable triggers are briefly removed only to backfill derived hashes.
    op.execute("DROP TRIGGER approvals_immutable ON approvals")
    op.execute("DROP FUNCTION reject_immutable_approval_mutation()")
    op.execute("DROP TRIGGER destination_execution_identity_immutable ON destination_executions")
    op.execute("DROP FUNCTION protect_destination_execution_identity()")

    op.add_column("approvals", sa.Column("destination_plan_hash", sa.String(64), nullable=True))
    approvals = (
        op.get_bind()
        .execute(sa.text("SELECT id, destination_plan FROM approvals ORDER BY id"))
        .all()
    )
    for approval_id, plan in approvals:
        normalized_plan = _normalize_legacy_plan(plan)
        op.get_bind().execute(
            sa.text(
                "UPDATE approvals SET destination_plan = CAST(:plan AS jsonb), "
                "destination_plan_hash = :hash WHERE id = :id"
            ),
            {
                "plan": json.dumps(normalized_plan, ensure_ascii=False),
                "hash": _plan_hash(normalized_plan),
                "id": approval_id,
            },
        )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.alter_column("approvals", "destination_plan_hash", nullable=False)
    op.drop_constraint("uq_approvals_campaign_version", "approvals", type_="unique")
    # Re-approval of a previously seen plan is valid after another immutable
    # approval supersedes it, so plan identity must not be unique over history.
    op.alter_column("approvals", "approved_plan_hash", nullable=True)
    op.drop_constraint("ck_approvals_runtime_mode", "approvals", type_="check")
    op.alter_column("approvals", "runtime_mode", nullable=True)
    op.create_check_constraint(
        "ck_approvals_legacy_runtime_mode",
        "approvals",
        "runtime_mode IS NULL OR runtime_mode IN ('SIMULATED', 'LIVE')",
    )
    op.drop_constraint("ck_approvals_hashes_format", "approvals", type_="check")
    op.create_check_constraint(
        "ck_approvals_hashes_format",
        "approvals",
        "version_hash ~ '^[0-9a-f]{64}$' AND "
        "destination_plan_hash ~ '^[0-9a-f]{64}$' AND "
        "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[0-9a-f]{64}$')",
    )

    op.add_column(
        "destination_executions", sa.Column("destination_plan_hash", sa.String(64), nullable=True)
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE destination_executions AS execution "
            "SET destination_plan_hash = approval.destination_plan_hash "
            "FROM approvals AS approval WHERE approval.id = execution.approval_id"
        )
    )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.alter_column("destination_executions", "destination_plan_hash", nullable=False)
    op.drop_constraint(
        "uq_destination_execution_identity", "destination_executions", type_="unique"
    )
    op.create_unique_constraint(
        "uq_destination_execution_identity",
        "destination_executions",
        ["approval_id", "destination_key", "target_key"],
    )
    op.alter_column("destination_executions", "runtime_mode", nullable=True)
    op.alter_column("destination_executions", "approved_plan_hash", nullable=True)
    op.drop_constraint("ck_destination_execution_mode", "destination_executions", type_="check")
    op.create_check_constraint(
        "ck_destination_execution_mode",
        "destination_executions",
        "runtime_mode IS NULL OR runtime_mode IN ('SIMULATED', 'LIVE')",
    )
    op.execute(
        "UPDATE destination_executions SET runtime_mode = NULL, "
        "plan_entry = plan_entry - 'runtime_mode' WHERE execution_kind = 'MANUAL'"
    )
    op.drop_constraint("ck_destination_execution_hashes", "destination_executions", type_="check")
    op.create_check_constraint(
        "ck_destination_execution_hashes",
        "destination_executions",
        "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[0-9a-f]{64}$') AND "
        "destination_plan_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'",
    )

    op.execute(
        "CREATE FUNCTION reject_immutable_approval_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'approvals are immutable'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER approvals_immutable BEFORE UPDATE OR DELETE ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_approval_mutation()"
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
               NEW.destination_plan_hash IS DISTINCT FROM OLD.destination_plan_hash OR
               NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
               NEW.plan_entry IS DISTINCT FROM OLD.plan_entry OR
               NEW.manual_package IS DISTINCT FROM OLD.manual_package OR
               NEW.manual_package_hash IS DISTINCT FROM OLD.manual_package_hash THEN
                RAISE EXCEPTION 'destination execution identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER destination_execution_identity_immutable "
        "BEFORE UPDATE OR DELETE ON destination_executions "
        "FOR EACH ROW EXECUTE FUNCTION protect_destination_execution_identity()"
    )


def downgrade():
    # Restore the old all-SIMULATED representation only when history is losslessly
    # representable. A mixed LIVE plan cannot be collapsed to a global mode.
    bind = op.get_bind()
    plans = bind.execute(sa.text("SELECT destination_plan FROM approvals")).scalars().all()
    if any(
        target.get("execution_kind") == "AUTO" and target.get("runtime_mode") != "SIMULATED"
        for plan in plans
        for target in plan.get("targets", [])
    ):
        raise RuntimeError("Cannot downgrade mixed LIVE approvals to the global-mode schema")
    duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM destination_executions "
            "GROUP BY campaign_version_id, destination_key, target_key HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade repeated approvals with duplicate version targets")
    repeated_approval = bind.execute(
        sa.text("SELECT 1 FROM approvals GROUP BY campaign_version_id HAVING count(*) > 1 LIMIT 1")
    ).first()
    if repeated_approval:
        raise RuntimeError("Cannot downgrade multiple historical approvals for one version")

    op.execute("DROP TRIGGER destination_execution_identity_immutable ON destination_executions")
    op.execute("DROP FUNCTION protect_destination_execution_identity()")
    op.execute("DROP TRIGGER approvals_immutable ON approvals")
    op.execute("DROP FUNCTION reject_immutable_approval_mutation()")
    approvals = bind.execute(
        sa.text("SELECT id, campaign_version_id, version_hash, destination_plan FROM approvals")
    ).all()
    for approval_id, version_id, version_hash, plan in approvals:
        legacy_plan = dict(plan)
        legacy_plan["schema_version"] = 1
        legacy_plan["runtime_mode"] = "SIMULATED"
        legacy_plan["targets"] = [
            {**target, "runtime_mode": "SIMULATED"} for target in plan.get("targets", [])
        ]
        old_hash_payload = {
            "campaign_version_id": str(version_id),
            "version_hash": version_hash,
            "runtime_mode": "SIMULATED",
            "destination_plan": legacy_plan,
        }
        old_hash = hashlib.sha256(
            json.dumps(
                old_hash_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE approvals SET destination_plan = CAST(:plan AS jsonb), "
                "approved_plan_hash = :old_hash, runtime_mode = 'SIMULATED' WHERE id = :id"
            ),
            {
                "plan": json.dumps(legacy_plan, ensure_ascii=False),
                "old_hash": old_hash,
                "id": approval_id,
            },
        )
        bind.execute(
            sa.text(
                "UPDATE destination_executions SET runtime_mode = 'SIMULATED', "
                "approved_plan_hash = :old_hash, "
                "plan_entry = jsonb_set(plan_entry, '{runtime_mode}', "
                "to_jsonb('SIMULATED'::text)) "
                "WHERE approval_id = :approval_id"
            ),
            {"old_hash": old_hash, "approval_id": approval_id},
        )
        executions = bind.execute(
            sa.text(
                "SELECT id, campaign_version_id, destination_key, target_key "
                "FROM destination_executions WHERE approval_id = :approval_id"
            ),
            {"approval_id": approval_id},
        ).all()
        for execution_id, execution_version_id, destination_key, target_key in executions:
            legacy_idempotency_key = hashlib.sha256(
                json.dumps(
                    {
                        "campaign_version_id": str(execution_version_id),
                        "destination_key": destination_key,
                        "target_key": target_key,
                        "runtime_mode": "SIMULATED",
                        "approved_plan_hash": old_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            bind.execute(
                sa.text("UPDATE destination_executions SET idempotency_key = :key WHERE id = :id"),
                {"key": legacy_idempotency_key, "id": execution_id},
            )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_constraint("ck_destination_execution_hashes", "destination_executions", type_="check")
    op.drop_constraint("ck_destination_execution_mode", "destination_executions", type_="check")
    op.drop_constraint(
        "uq_destination_execution_identity", "destination_executions", type_="unique"
    )
    op.alter_column("destination_executions", "runtime_mode", nullable=False)
    op.alter_column("destination_executions", "approved_plan_hash", nullable=False)
    op.drop_column("destination_executions", "destination_plan_hash")
    op.create_unique_constraint(
        "uq_destination_execution_identity",
        "destination_executions",
        ["campaign_version_id", "destination_key", "target_key", "runtime_mode"],
    )
    op.create_check_constraint(
        "ck_destination_execution_mode",
        "destination_executions",
        "runtime_mode IN ('SIMULATED', 'LIVE')",
    )
    op.create_check_constraint(
        "ck_destination_execution_hashes",
        "destination_executions",
        "approved_plan_hash ~ '^[0-9a-f]{64}$' AND idempotency_key ~ '^[0-9a-f]{64}$'",
    )
    op.drop_constraint("ck_approvals_hashes_format", "approvals", type_="check")
    op.create_check_constraint(
        "ck_approvals_hashes_format",
        "approvals",
        "version_hash ~ '^[0-9a-f]{64}$' AND approved_plan_hash ~ '^[0-9a-f]{64}$'",
    )
    op.alter_column("approvals", "runtime_mode", nullable=False)
    op.drop_constraint("ck_approvals_legacy_runtime_mode", "approvals", type_="check")
    op.create_check_constraint(
        "ck_approvals_runtime_mode", "approvals", "runtime_mode IN ('SIMULATED', 'LIVE')"
    )
    op.alter_column("approvals", "approved_plan_hash", nullable=False)
    op.create_unique_constraint(
        "uq_approvals_campaign_version", "approvals", ["campaign_version_id"]
    )
    op.drop_column("approvals", "destination_plan_hash")
    op.execute(
        """
        CREATE FUNCTION reject_immutable_approval_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'approvals are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER approvals_immutable BEFORE UPDATE OR DELETE ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION reject_immutable_approval_mutation()"
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
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER destination_execution_identity_immutable "
        "BEFORE UPDATE OR DELETE ON destination_executions "
        "FOR EACH ROW EXECUTE FUNCTION protect_destination_execution_identity()"
    )
