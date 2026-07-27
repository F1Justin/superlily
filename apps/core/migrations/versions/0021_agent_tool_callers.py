"""Permit Git-gated agent callers for Phase 5b read-only tool canaries."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0021_agent_tool_callers"
down_revision: str | None = "0020_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_checks(callers: str) -> None:
    with op.batch_alter_table("tool_rollout_plan_items") as batch:
        batch.drop_constraint("ck_rollout_item_caller", type_="check")
        batch.create_check_constraint(
            "ck_rollout_item_caller",
            f"caller IN ({callers})",
        )
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_constraint("ck_tool_invocation_creator_type", type_="check")
        batch.create_check_constraint(
            "ck_tool_invocation_creator_type",
            f"creator_type IN ({callers})",
        )
    transition_callers = (
        "'command', 'agent', 'admin_api', 'provider', 'reaper', 'system'"
        if "'agent'" in callers
        else "'command', 'admin_api', 'provider', 'reaper', 'system'"
    )
    with op.batch_alter_table("tool_invocation_transitions") as batch:
        batch.drop_constraint("ck_tool_invocation_transition_actor", type_="check")
        batch.create_check_constraint(
            "ck_tool_invocation_transition_actor",
            f"actor_type IN ({transition_callers})",
        )


def _restore_sqlite_rollout_guards() -> None:
    op.execute(
        """
        CREATE TRIGGER tool_rollout_plan_items_no_update
        BEFORE UPDATE ON tool_rollout_plan_items
        BEGIN
            SELECT RAISE(ABORT, 'rollout evidence is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_invocation_transitions_no_update
        BEFORE UPDATE ON tool_invocation_transitions
        BEGIN
            SELECT RAISE(ABORT, 'tool invocation transitions are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_invocation_transitions_no_delete
        BEFORE DELETE ON tool_invocation_transitions
        BEGIN
            SELECT RAISE(ABORT, 'tool invocation transitions are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_rollout_plan_items_no_delete
        BEFORE DELETE ON tool_rollout_plan_items
        BEGIN
            SELECT RAISE(ABORT, 'rollout evidence is append-only');
        END
        """
    )


def upgrade() -> None:
    _replace_checks("'command', 'agent', 'admin_api'")
    if op.get_bind().dialect.name == "sqlite":
        _restore_sqlite_rollout_guards()


def downgrade() -> None:
    _replace_checks("'command', 'admin_api'")
    if op.get_bind().dialect.name == "sqlite":
        _restore_sqlite_rollout_guards()
