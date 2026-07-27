"""Add bounded Phase 5b tool-result continuation evidence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_agent_tool_loops"
down_revision: str | None = "0021_agent_tool_callers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tool_loops",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "proposal_id",
            sa.String(36),
            sa.ForeignKey("agent_tool_proposals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.String(36),
            sa.ForeignKey("tool_invocations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("result_json", sa.JSON(none_as_null=True)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
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
        sa.UniqueConstraint("run_id", name="uq_agent_tool_loop_run"),
        sa.UniqueConstraint("proposal_id", name="uq_agent_tool_loop_proposal"),
        sa.UniqueConstraint("invocation_id", name="uq_agent_tool_loop_invocation"),
        sa.CheckConstraint(
            "state IN ('tool_pending', 'result_ready', 'complete', 'failed', "
            "'budget_exhausted')",
            name="ck_agent_tool_loop_state",
        ),
        sa.CheckConstraint("resource_version >= 1", name="ck_agent_tool_loop_version"),
        sa.CheckConstraint("result_bytes >= 0", name="ck_agent_tool_loop_result_bytes"),
        sa.CheckConstraint(
            "((state = 'result_ready' AND result_json IS NOT NULL "
            "AND result_hash IS NOT NULL AND terminal_at IS NULL) OR "
            "(state IN ('complete', 'failed', 'budget_exhausted') "
            "AND terminal_at IS NOT NULL) OR "
            "(state = 'tool_pending' AND result_json IS NULL "
            "AND result_hash IS NULL AND terminal_at IS NULL))",
            name="ck_agent_tool_loop_result_state",
        ),
    )
    op.create_index(
        "ix_agent_tool_loops_state",
        "agent_tool_loops",
        ["state", "updated_at"],
    )
    op.create_table(
        "agent_tool_loop_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "loop_id",
            sa.String(36),
            sa.ForeignKey("agent_tool_loops.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("loop_id", "sequence", name="uq_agent_tool_loop_event"),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_tool_loop_event_sequence"),
    )
    op.create_table(
        "agent_tool_continuations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "loop_id",
            sa.String(36),
            sa.ForeignKey("agent_tool_loops.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "loop_id",
            "attempt_number",
            name="uq_agent_tool_continuation_attempt",
        ),
        sa.UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_agent_tool_continuation_idempotency",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_agent_tool_continuation_attempt",
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("agent_tool_loop_events", "agent_tool_continuations"):
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'agent loop evidence is append-only'); END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'agent loop evidence is append-only'); END
                """
            )
        op.execute(
            """
            CREATE TRIGGER agent_tool_loops_authority_no_update
            BEFORE UPDATE OF run_id, proposal_id, invocation_id, created_at
            ON agent_tool_loops
            BEGIN
                SELECT RAISE(ABORT, 'agent tool loop authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_tool_loops_no_delete
            BEFORE DELETE ON agent_tool_loops
            BEGIN
                SELECT RAISE(ABORT, 'agent tool loop evidence cannot be deleted');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_tool_loops_state_guard
            BEFORE UPDATE OF state, resource_version, reason_code, result_json,
                result_hash, result_bytes, terminal_at, updated_at
            ON agent_tool_loops
            BEGIN
                SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                    THEN RAISE(ABORT, 'agent tool loop version must increase by one') END;
                SELECT CASE WHEN NOT (
                    (OLD.state = 'tool_pending' AND NEW.state IN (
                        'result_ready', 'failed', 'budget_exhausted'
                    )) OR
                    (OLD.state = 'result_ready' AND NEW.state IN (
                        'result_ready', 'complete', 'failed', 'budget_exhausted'
                    ))
                ) THEN RAISE(ABORT, 'agent tool loop transition is not allowed') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM agent_tool_loop_events
                    WHERE loop_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE(ABORT, 'agent tool loop event is required') END;
            END
            """
        )
    else:
        for table in ("agent_tool_loop_events", "agent_tool_continuations"):
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_mutation
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_agent_evidence_mutation()
                """
            )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_agent_tool_loop_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'agent tool loop evidence cannot be deleted';
                END IF;
                IF NEW.run_id IS DISTINCT FROM OLD.run_id
                   OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
                   OR NEW.invocation_id IS DISTINCT FROM OLD.invocation_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'agent tool loop authority is immutable';
                END IF;
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'agent tool loop version must increase by one';
                END IF;
                IF NOT (
                    (OLD.state = 'tool_pending' AND NEW.state IN (
                        'result_ready', 'failed', 'budget_exhausted'
                    )) OR
                    (OLD.state = 'result_ready' AND NEW.state IN (
                        'result_ready', 'complete', 'failed', 'budget_exhausted'
                    ))
                ) THEN
                    RAISE EXCEPTION 'agent tool loop transition is not allowed';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM agent_tool_loop_events
                    WHERE loop_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN
                    RAISE EXCEPTION 'agent tool loop event is required';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_tool_loops_guard
            BEFORE UPDATE OR DELETE ON agent_tool_loops
            FOR EACH ROW EXECUTE FUNCTION guard_agent_tool_loop_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_agent_tool_loop_mutation() CASCADE")
    op.drop_table("agent_tool_continuations")
    op.drop_table("agent_tool_loop_events")
    op.drop_index("ix_agent_tool_loops_state", table_name="agent_tool_loops")
    op.drop_table("agent_tool_loops")
