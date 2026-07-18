"""增加只记账、不产生 lease 的工具调用账本。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_tool_invocations"
down_revision: str | None = "0013_collection_reliability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("creator_type", sa.String(length=32), nullable=False),
        sa.Column("creator_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("descriptor_id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=128), nullable=False),
        sa.Column("descriptor_version", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("descriptor_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("principal_hash", sa.String(length=64), nullable=False),
        sa.Column("capability_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("capability_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("transition_sequence", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "creator_type IN ('command', 'admin_api')",
            name="ck_tool_invocation_creator_type",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('off', 'ledger_only', 'canary', 'enforce')",
            name="ck_tool_invocation_execution_mode",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'rejected', 'recorded_only', "
            "'awaiting_confirmation', 'queued', 'leased', 'running', 'succeeded', "
            "'failed', 'timed_out', 'cancel_requested', 'cancelled', "
            "'unknown_completion', 'expired', 'lease_expired')",
            name="ck_tool_invocation_state",
        ),
        sa.CheckConstraint(
            "transition_sequence >= 1",
            name="ck_tool_invocation_sequence",
        ),
        sa.CheckConstraint(
            "((state IN ('rejected', 'recorded_only', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'unknown_completion', 'expired') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('rejected', 'recorded_only', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'unknown_completion', 'expired') AND terminal_at IS NULL))",
            name="ck_tool_invocation_terminal_time",
        ),
        sa.ForeignKeyConstraint(
            ["descriptor_id"],
            ["tool_descriptors.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "creator_type",
            "creator_id",
            "idempotency_key",
            name="uq_tool_invocation_idempotency",
        ),
    )
    op.create_index(
        "ix_tool_invocations_tool",
        "tool_invocations",
        ["tool_id", "descriptor_version"],
    )
    op.create_index(
        "ix_tool_invocations_state_deadline",
        "tool_invocations",
        ["state", "deadline_at"],
    )
    op.create_index(
        "ix_tool_invocations_created",
        "tool_invocations",
        ["created_at"],
    )

    op.create_table(
        "tool_invocation_transitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_tool_invocation_transition_sequence",
        ),
        sa.CheckConstraint(
            "event IN ('propose', 'reject', 'record_only', 'require_confirmation', "
            "'confirm', 'confirmation_expire', 'queue', 'lease', 'start', "
            "'complete_success', 'complete_failure', 'request_cancel', 'cancel', "
            "'lease_expire', 'timeout', 'unknown_completion', 'requeue')",
            name="ck_tool_invocation_transition_event",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'rejected', 'recorded_only', "
            "'awaiting_confirmation', 'queued', 'leased', 'running', 'succeeded', "
            "'failed', 'timed_out', 'cancel_requested', 'cancelled', "
            "'unknown_completion', 'expired', 'lease_expired')",
            name="ck_tool_invocation_transition_state",
        ),
        sa.CheckConstraint(
            "actor_type IN ('command', 'admin_api', 'provider', 'reaper', 'system')",
            name="ck_tool_invocation_transition_actor",
        ),
        sa.CheckConstraint(
            "((event = 'propose' AND previous_state IS NULL AND state = 'proposed') OR "
            "(event <> 'propose' AND previous_state IS NOT NULL))",
            name="ck_tool_invocation_transition_initial",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["tool_invocations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id",
            "sequence",
            name="uq_tool_invocation_transition_sequence",
        ),
    )
    op.create_index(
        "ix_tool_invocation_transitions_created",
        "tool_invocation_transitions",
        ["invocation_id", "created_at"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
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
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_tool_invocation_transition_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'tool invocation transitions are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_invocation_transitions_no_mutation
            BEFORE UPDATE OR DELETE ON tool_invocation_transitions
            FOR EACH ROW EXECUTE FUNCTION reject_tool_invocation_transition_mutation()
            """
        )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_invocation_transitions_created",
        table_name="tool_invocation_transitions",
    )
    op.drop_table("tool_invocation_transitions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_tool_invocation_transition_mutation()")
    op.drop_index("ix_tool_invocations_created", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_state_deadline", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_tool", table_name="tool_invocations")
    op.drop_table("tool_invocations")
