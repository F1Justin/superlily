"""增加 Provider 拉取 lease、单调 fence 和只追加 attempt 事件。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_tool_attempts"
down_revision: str | None = "0014_tool_invocations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_invocations",
        sa.Column("selected_provider_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_tool_invocations_provider_queue",
        "tool_invocations",
        ["selected_provider_id", "state", "created_at"],
    )
    op.create_table(
        "tool_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("inventory_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_hash", sa.String(length=64), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("budget_hash", sa.String(length=64), nullable=False),
        sa.Column("permissions_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("permissions_hash", sa.String(length=64), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("usage_hash", sa.String(length=64), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("provider_result_id", sa.String(length=512), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_detail", sa.String(length=512), nullable=True),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("attempt_number >= 1", name="ck_tool_attempt_number"),
        sa.CheckConstraint("fencing_token >= 1", name="ck_tool_attempt_fencing_token"),
        sa.CheckConstraint(
            "event_sequence >= 1",
            name="ck_tool_attempt_current_event_sequence",
        ),
        sa.CheckConstraint(
            "state IN ('leased', 'running', 'succeeded', 'failed', 'cancelled', "
            "'lease_expired', 'unknown_completion')",
            name="ck_tool_attempt_state",
        ),
        sa.CheckConstraint(
            "((state IN ('succeeded', 'failed', 'cancelled', 'lease_expired', "
            "'unknown_completion') AND completed_at IS NOT NULL) OR "
            "(state IN ('leased', 'running') AND completed_at IS NULL))",
            name="ck_tool_attempt_terminal_time",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"], ["tool_invocations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id", "attempt_number", name="uq_tool_attempt_number"
        ),
        sa.UniqueConstraint(
            "invocation_id", "fencing_token", name="uq_tool_attempt_fencing_token"
        ),
    )
    op.create_index(
        "uq_tool_attempt_active_invocation",
        "tool_attempts",
        ["invocation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('leased', 'running')"),
        sqlite_where=sa.text("state IN ('leased', 'running')"),
    )
    op.create_index(
        "ix_tool_attempt_provider_state", "tool_attempts", ["provider_id", "state"]
    )
    op.create_index(
        "ix_tool_attempt_lease_expiry", "tool_attempts", ["state", "lease_expires_at"]
    )

    op.create_table(
        "tool_attempt_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_tool_attempt_event_sequence"),
        sa.CheckConstraint(
            "fencing_token >= 1", name="ck_tool_attempt_event_fencing_token"
        ),
        sa.CheckConstraint(
            "event IN ('lease', 'start', 'heartbeat', 'complete', 'fail', 'cancel', "
            "'lease_expire', 'reject')",
            name="ck_tool_attempt_event_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected')", name="ck_tool_attempt_event_outcome"
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["tool_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "sequence", name="uq_tool_attempt_event_sequence"),
    )
    op.create_index(
        "ix_tool_attempt_events_created", "tool_attempt_events", ["attempt_id", "created_at"]
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER tool_attempt_events_no_update
            BEFORE UPDATE ON tool_attempt_events
            BEGIN
                SELECT RAISE(ABORT, 'tool attempt events are append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_attempt_events_no_delete
            BEFORE DELETE ON tool_attempt_events
            BEGIN
                SELECT RAISE(ABORT, 'tool attempt events are append-only');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_tool_attempt_event_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'tool attempt events are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_attempt_events_no_mutation
            BEFORE UPDATE OR DELETE ON tool_attempt_events
            FOR EACH ROW EXECUTE FUNCTION reject_tool_attempt_event_mutation()
            """
        )


def downgrade() -> None:
    op.drop_index("ix_tool_attempt_events_created", table_name="tool_attempt_events")
    op.drop_table("tool_attempt_events")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_tool_attempt_event_mutation()")
    op.drop_index("ix_tool_attempt_lease_expiry", table_name="tool_attempts")
    op.drop_index("ix_tool_attempt_provider_state", table_name="tool_attempts")
    op.drop_index("uq_tool_attempt_active_invocation", table_name="tool_attempts")
    op.drop_table("tool_attempts")
    op.drop_index("ix_tool_invocations_provider_queue", table_name="tool_invocations")
    op.drop_column("tool_invocations", "selected_provider_id")
