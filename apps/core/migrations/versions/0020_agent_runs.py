"""Add zero-execution Phase 5a AgentRun and model proposal ledgers."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0020_agent_runs"
down_revision: str | None = "0019_phase4_planning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _append_only_sqlite(table: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, 'agent evidence is append-only');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, 'agent evidence is append-only');
        END
        """
    )


def _append_only_postgres(table: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_mutation
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION reject_agent_evidence_mutation()
        """
    )


def upgrade() -> None:
    op.create_table(
        "agent_model_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("source_commit", sa.String(40), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "provider_id",
            "version",
            name="uq_agent_model_profile_version",
        ),
        sa.UniqueConstraint("profile_hash", name="uq_agent_model_profile_hash"),
    )
    op.create_index(
        "ix_agent_model_profiles_provider",
        "agent_model_profiles",
        ["provider_id", "version"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("creator_type", sa.String(32), nullable=False),
        sa.Column("creator_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "source_event_id",
            sa.String(512),
            sa.ForeignKey("source_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("conversation_key", sa.String(512), nullable=False),
        sa.Column("principal_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("principal_hash", sa.String(64), nullable=False),
        sa.Column("context_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("context_recipe_version", sa.String(128), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("eligible_tools_json", sa.JSON(), nullable=False),
        sa.Column("eligible_tools_hash", sa.String(64), nullable=False),
        sa.Column("budget_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("budget_hash", sa.String(64), nullable=False),
        sa.Column(
            "model_profile_id",
            sa.String(36),
            sa.ForeignKey("agent_model_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("model_profile_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("model_profile_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("tool_invocation_count", sa.Integer(), nullable=False),
        sa.Column("delivery_intent_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "creator_type",
            "creator_id",
            "idempotency_key",
            name="uq_agent_run_idempotency",
        ),
        sa.CheckConstraint(
            "creator_type IN ('admin_api', 'system')",
            name="ck_agent_run_creator_type",
        ),
        sa.CheckConstraint("mode = 'shadow'", name="ck_agent_run_mode"),
        sa.CheckConstraint(
            "state IN ('context_ready', 'model_running', 'shadow_complete', "
            "'rejected', 'failed', 'timed_out', 'budget_exhausted', 'cancelled')",
            name="ck_agent_run_state",
        ),
        sa.CheckConstraint(
            "resource_version >= 1",
            name="ck_agent_run_resource_version",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_agent_run_attempt_count"),
        sa.CheckConstraint(
            "tool_invocation_count = 0",
            name="ck_agent_run_zero_tool_execution",
        ),
        sa.CheckConstraint(
            "delivery_intent_count = 0",
            name="ck_agent_run_zero_delivery",
        ),
        sa.CheckConstraint(
            "((state IN ('shadow_complete', 'rejected', 'failed', 'timed_out', "
            "'budget_exhausted', 'cancelled') AND terminal_at IS NOT NULL) OR "
            "(state IN ('context_ready', 'model_running') AND terminal_at IS NULL))",
            name="ck_agent_run_terminal",
        ),
    )
    op.create_index(
        "ix_agent_runs_source_created",
        "agent_runs",
        ["source_event_id", "created_at"],
    )
    op.create_index(
        "ix_agent_runs_state_deadline",
        "agent_runs",
        ["state", "deadline_at"],
    )
    op.create_index(
        "ix_agent_runs_model_profile",
        "agent_runs",
        ["model_profile_id", "created_at"],
    )

    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_agent_run_event_sequence",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_run_event_sequence"),
        sa.CheckConstraint(
            "event IN ('context_ready', 'model_start', 'model_retry', "
            "'shadow_complete', 'reject', 'fail', 'timeout', "
            "'budget_exhaust', 'cancel')",
            name="ck_agent_run_event_type",
        ),
        sa.CheckConstraint(
            "state IN ('context_ready', 'model_running', 'shadow_complete', "
            "'rejected', 'failed', 'timed_out', 'budget_exhausted', 'cancelled')",
            name="ck_agent_run_event_state",
        ),
        sa.CheckConstraint(
            "actor_type IN ('admin_api', 'model_provider', 'system')",
            name="ck_agent_run_event_actor",
        ),
    )
    op.create_index(
        "ix_agent_run_events_created",
        "agent_run_events",
        ["run_id", "created_at"],
    )

    op.create_table(
        "agent_run_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("model_profile_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("model_request_id", sa.String(256)),
        sa.Column("raw_output_sha256", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.JSON(none_as_null=True)),
        sa.Column("proposal_hash", sa.String(64)),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("usage_hash", sa.String(64), nullable=False),
        sa.Column("safe_error_code", sa.String(128)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "run_id",
            "attempt_number",
            name="uq_agent_run_attempt_number",
        ),
        sa.UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_agent_run_attempt_idempotency",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_agent_run_attempt_number",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'provider_error', 'invalid_output', "
            "'timed_out', 'cancelled')",
            name="ck_agent_run_attempt_outcome",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="ck_agent_run_attempt_time",
        ),
        sa.CheckConstraint(
            "((outcome = 'succeeded' AND proposal_json IS NOT NULL "
            "AND proposal_hash IS NOT NULL AND safe_error_code IS NULL) OR "
            "(outcome <> 'succeeded' AND proposal_json IS NULL "
            "AND proposal_hash IS NULL AND safe_error_code IS NOT NULL))",
            name="ck_agent_run_attempt_result",
        ),
    )
    op.create_index(
        "ix_agent_run_attempts_run_created",
        "agent_run_attempts",
        ["run_id", "created_at"],
    )

    op.create_table(
        "agent_tool_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("agent_run_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("descriptor_version", sa.String(64), nullable=False),
        sa.Column("descriptor_hash", sa.String(64), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("validation", sa.String(32), nullable=False),
        sa.Column("validation_reasons_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "ordinal",
            name="uq_agent_tool_proposal_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_agent_tool_proposal_ordinal",
        ),
        sa.CheckConstraint(
            "validation IN ('valid', 'invalid_arguments', "
            "'forbidden_tool', 'duplicate_loop')",
            name="ck_agent_tool_proposal_validation",
        ),
    )
    op.create_index(
        "ix_agent_tool_proposals_run_created",
        "agent_tool_proposals",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_agent_tool_proposals_tool",
        "agent_tool_proposals",
        ["tool_id", "descriptor_version"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in (
            "agent_model_profiles",
            "agent_run_events",
            "agent_run_attempts",
            "agent_tool_proposals",
        ):
            _append_only_sqlite(table)
        op.execute(
            """
            CREATE TRIGGER agent_runs_authority_no_update
            BEFORE UPDATE OF creator_type, creator_id, idempotency_key, request_hash,
                source_event_id, conversation_key, principal_snapshot_json, principal_hash,
                context_snapshot_json, context_recipe_version, context_hash,
                eligible_tools_json, eligible_tools_hash,
                budget_snapshot_json, budget_hash, model_profile_id,
                model_profile_snapshot_json, model_profile_hash, mode,
                tool_invocation_count, delivery_intent_count, deadline_at, created_at
            ON agent_runs
            BEGIN
                SELECT RAISE(ABORT, 'agent run authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_runs_no_delete
            BEFORE DELETE ON agent_runs
            BEGIN
                SELECT RAISE(ABORT, 'agent run evidence cannot be deleted');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_runs_state_guard
            BEFORE UPDATE OF state, resource_version, attempt_count, reason_code,
                terminal_at, updated_at
            ON agent_runs
            BEGIN
                SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                    THEN RAISE(ABORT, 'agent run resource version must increase by one') END;
                SELECT CASE WHEN NOT (
                    (OLD.state = 'context_ready' AND NEW.state = 'model_running') OR
                    (OLD.state = 'model_running' AND NEW.state IN (
                        'context_ready', 'shadow_complete', 'failed', 'timed_out',
                        'budget_exhausted', 'cancelled'
                    ))
                ) THEN RAISE(ABORT, 'agent run state transition is not allowed') END;
                SELECT CASE WHEN (
                    OLD.state = 'context_ready'
                    AND NEW.state = 'model_running'
                    AND NEW.attempt_count != OLD.attempt_count + 1
                ) OR (
                    OLD.state = 'model_running'
                    AND NEW.attempt_count != OLD.attempt_count
                ) THEN RAISE(ABORT, 'agent run attempt count is invalid') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM agent_run_events
                    WHERE run_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE(ABORT, 'agent run event is required') END;
            END
            """
        )
    else:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_agent_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'agent evidence is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in (
            "agent_model_profiles",
            "agent_run_events",
            "agent_run_attempts",
            "agent_tool_proposals",
        ):
            _append_only_postgres(table)
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_agent_run_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'agent run evidence cannot be deleted';
                END IF;
                IF NEW.creator_type IS DISTINCT FROM OLD.creator_type
                   OR NEW.creator_id IS DISTINCT FROM OLD.creator_id
                   OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
                   OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
                   OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
                   OR NEW.conversation_key IS DISTINCT FROM OLD.conversation_key
                   OR NEW.principal_snapshot_json::text IS DISTINCT FROM OLD.principal_snapshot_json::text
                   OR NEW.principal_hash IS DISTINCT FROM OLD.principal_hash
                   OR NEW.context_snapshot_json::text IS DISTINCT FROM OLD.context_snapshot_json::text
                   OR NEW.context_recipe_version IS DISTINCT FROM OLD.context_recipe_version
                   OR NEW.context_hash IS DISTINCT FROM OLD.context_hash
                   OR NEW.eligible_tools_json::text IS DISTINCT FROM OLD.eligible_tools_json::text
                   OR NEW.eligible_tools_hash IS DISTINCT FROM OLD.eligible_tools_hash
                   OR NEW.budget_snapshot_json::text IS DISTINCT FROM OLD.budget_snapshot_json::text
                   OR NEW.budget_hash IS DISTINCT FROM OLD.budget_hash
                   OR NEW.model_profile_id IS DISTINCT FROM OLD.model_profile_id
                   OR NEW.model_profile_snapshot_json::text IS DISTINCT FROM OLD.model_profile_snapshot_json::text
                   OR NEW.model_profile_hash IS DISTINCT FROM OLD.model_profile_hash
                   OR NEW.mode IS DISTINCT FROM OLD.mode
                   OR NEW.tool_invocation_count IS DISTINCT FROM OLD.tool_invocation_count
                   OR NEW.delivery_intent_count IS DISTINCT FROM OLD.delivery_intent_count
                   OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'agent run authority is immutable';
                END IF;
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'agent run resource version must increase by one';
                END IF;
                IF NOT (
                    (OLD.state = 'context_ready' AND NEW.state = 'model_running') OR
                    (OLD.state = 'model_running' AND NEW.state IN (
                        'context_ready', 'shadow_complete', 'failed', 'timed_out',
                        'budget_exhausted', 'cancelled'
                    ))
                ) THEN
                    RAISE EXCEPTION 'agent run state transition is not allowed';
                END IF;
                IF (OLD.state = 'context_ready'
                    AND NEW.state = 'model_running'
                    AND NEW.attempt_count != OLD.attempt_count + 1)
                   OR (OLD.state = 'model_running'
                       AND NEW.attempt_count != OLD.attempt_count) THEN
                    RAISE EXCEPTION 'agent run attempt count is invalid';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM agent_run_events
                    WHERE run_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN
                    RAISE EXCEPTION 'agent run event is required';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_runs_guard
            BEFORE UPDATE OR DELETE ON agent_runs
            FOR EACH ROW EXECUTE FUNCTION guard_agent_run_mutation()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_agent_run_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS reject_agent_evidence_mutation() CASCADE")

    op.drop_index("ix_agent_tool_proposals_tool", table_name="agent_tool_proposals")
    op.drop_index(
        "ix_agent_tool_proposals_run_created",
        table_name="agent_tool_proposals",
    )
    op.drop_table("agent_tool_proposals")
    op.drop_index(
        "ix_agent_run_attempts_run_created",
        table_name="agent_run_attempts",
    )
    op.drop_table("agent_run_attempts")
    op.drop_index("ix_agent_run_events_created", table_name="agent_run_events")
    op.drop_table("agent_run_events")
    op.drop_index("ix_agent_runs_model_profile", table_name="agent_runs")
    op.drop_index("ix_agent_runs_state_deadline", table_name="agent_runs")
    op.drop_index("ix_agent_runs_source_created", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index(
        "ix_agent_model_profiles_provider",
        table_name="agent_model_profiles",
    )
    op.drop_table("agent_model_profiles")
