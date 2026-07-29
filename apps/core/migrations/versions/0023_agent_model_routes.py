"""Freeze explicit model fallback routes on every AgentRun."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0023_agent_model_routes"
down_revision: str | None = "0022_agent_tool_loops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EMPTY_ROUTE_HASH = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def _postgres_guard(*, routed: bool) -> None:
    route_authority = """
                   OR NEW.fallback_model_profiles_json::text IS DISTINCT FROM OLD.fallback_model_profiles_json::text
                   OR NEW.fallback_model_profiles_hash IS DISTINCT FROM OLD.fallback_model_profiles_hash
                   OR NEW.routing_reason IS DISTINCT FROM OLD.routing_reason
""" if routed else ""
    op.execute(
        f"""
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
{route_authority}               OR NEW.mode IS DISTINCT FROM OLD.mode
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


def _sqlite_authority_trigger(*, routed: bool) -> None:
    route_columns = (
        ", fallback_model_profiles_json, fallback_model_profiles_hash, routing_reason"
        if routed
        else ""
    )
    op.execute("DROP TRIGGER IF EXISTS agent_runs_authority_no_update")
    op.execute(
        f"""
        CREATE TRIGGER agent_runs_authority_no_update
        BEFORE UPDATE OF creator_type, creator_id, idempotency_key, request_hash,
            source_event_id, conversation_key, principal_snapshot_json, principal_hash,
            context_snapshot_json, context_recipe_version, context_hash,
            eligible_tools_json, eligible_tools_hash,
            budget_snapshot_json, budget_hash, model_profile_id,
            model_profile_snapshot_json, model_profile_hash{route_columns}, mode,
            tool_invocation_count, delivery_intent_count, deadline_at, created_at
        ON agent_runs
        BEGIN
            SELECT RAISE(ABORT, 'agent run authority is immutable');
        END
        """
    )


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "fallback_model_profiles_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "fallback_model_profiles_hash",
            sa.String(64),
            nullable=False,
            server_default=sa.text(f"'{_EMPTY_ROUTE_HASH}'"),
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "routing_reason",
            sa.String(128),
            nullable=False,
            server_default=sa.text("'explicit_primary'"),
        ),
    )
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_authority_trigger(routed=True)
    else:
        _postgres_guard(routed=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_authority_trigger(routed=False)
    else:
        _postgres_guard(routed=False)
    op.drop_column("agent_runs", "routing_reason")
    op.drop_column("agent_runs", "fallback_model_profiles_hash")
    op.drop_column("agent_runs", "fallback_model_profiles_json")
